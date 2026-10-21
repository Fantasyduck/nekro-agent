"""隔离 PG / Qdrant 验证索引为可丢弃投影，不能恢复失效事实。"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qm
from test_memory_fact_service import DB_URL, context, isolated_db, proposal

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_fact import DBMemAssertion, DBMemIndexOutbox
from nekro_agent.services.memory import fact_index, fact_retrieval
from nekro_agent.services.memory.contracts import MemoryError
from nekro_agent.services.memory.fact_index import FactIndexKey, FactIndexWorker, QdrantFactIndex
from nekro_agent.services.memory.fact_retrieval import rank_facts
from nekro_agent.services.memory.service import MemoryService

__all__ = ["isolated_db"]
pytestmark = pytest.mark.skipif(not DB_URL, reason="需要显式指定隔离 PostgreSQL")


def fake_index() -> AsyncMock:
    backend = AsyncMock()
    backend.search.return_value = []
    return backend


async def test_projection_stores_current_revision_and_acknowledges() -> None:
    service, backend = MemoryService(), fake_index()
    result = await service.propose_fact(context(), await proposal("绿茶"), idempotency_key="a")
    assert await FactIndexWorker(backend).process_one()
    key, text = backend.upsert.call_args.args
    assert key.assertion_id == result.assertion_id and key.revision == 1 and "绿茶" in text
    assert await DBMemIndexOutbox.filter(state="done").count() == 1


async def test_old_event_after_forget_deletes_instead_of_embedding() -> None:
    service, backend = MemoryService(), fake_index()
    result = await service.propose_fact(context(), await proposal(), idempotency_key="a")
    await service.forget(context(), result.assertion_id)
    worker = FactIndexWorker(backend)
    while await worker.process_one():
        pass
    assert backend.upsert.call_count == 0
    assert backend.delete.call_count == 2
    assert not (await service.recall(context(), query="tea")).items


async def test_out_of_order_correction_never_projects_superseded_text() -> None:
    service, backend = MemoryService(), fake_index()
    await service.propose_fact(context(), await proposal("tea"), idempotency_key="a")
    current = await service.propose_fact(context(), await proposal("coffee"), idempotency_key="b")
    worker = FactIndexWorker(backend)
    while await worker.process_one():
        pass
    assert all(call.args[0].assertion_id == current.assertion_id for call in backend.upsert.call_args_list)
    assert all("tea" not in call.args[1] for call in backend.upsert.call_args_list)


async def test_withdrawn_source_is_not_embedded() -> None:
    service, backend = MemoryService(), fake_index()
    fact = await proposal()
    await service.propose_fact(context(), fact, idempotency_key="a")
    await DBChatMessage.filter(id=int(fact.source.id)).update(is_recalled=True)
    await FactIndexWorker(backend).process_one()
    backend.upsert.assert_not_called()
    backend.delete.assert_awaited_once()


async def test_worker_failure_backoff_and_dead_letter_without_payload() -> None:
    service, backend = MemoryService(), fake_index()
    await service.propose_fact(context(), await proposal(), idempotency_key="a")
    backend.upsert.side_effect = RuntimeError("sensitive upstream response")
    worker = FactIndexWorker(backend)
    for attempt in range(1, 6):
        assert await worker.process_one()
        row = await DBMemIndexOutbox.all().get()
        assert row.attempts == attempt
        assert row.state == ("dead_letter" if attempt == 5 else "pending")
        if attempt < 5:
            assert not await worker.process_one()
            await DBMemIndexOutbox.filter(id=row.id).update(next_retry_at=None)
    assert not await worker.process_one()
    assert "sensitive" not in str(dict(row))


async def test_expired_lease_cannot_acknowledge_new_owner() -> None:
    await MemoryService().propose_fact(context(), await proposal(), idempotency_key="a")
    worker = FactIndexWorker(fake_index())
    old = await worker.claim()
    assert old is not None and await worker.claim() is None
    await DBMemIndexOutbox.filter(id=old.id).update(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    new = await worker.claim()
    assert new is not None and new.token != old.token
    await worker._finish(old, success=True)
    row = await DBMemIndexOutbox.get(id=old.id)
    assert row.state == "running" and row.lease_token == new.token


async def test_concurrent_claim_only_one_owner() -> None:
    await MemoryService().propose_fact(context(), await proposal(), idempotency_key="a")
    worker = FactIndexWorker(fake_index())
    leases = await asyncio.gather(worker.claim(), worker.claim())
    assert sum(lease is not None for lease in leases) == 1


async def test_expired_exhausted_job_does_not_block_new_work() -> None:
    service = MemoryService()
    await service.propose_fact(context(), await proposal(), idempotency_key="a")
    await DBMemIndexOutbox.all().update(
        attempts=5, state="running", lease_until=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    await service.propose_fact(context(), await proposal("coffee"), idempotency_key="b")
    assert await FactIndexWorker(fake_index()).process_one()
    assert await DBMemIndexOutbox.filter(state="dead_letter").count() == 1


async def test_chinese_local_retrieval_reads_before_index_consumption() -> None:
    service = MemoryService()
    await service.propose_fact(context(), await proposal("喜欢茉莉绿茶"), idempotency_key="a")
    bundle = await service.recall(context(), query="茉莉茶")
    assert [item.value for item in bundle.items] == ["喜欢茉莉绿茶"]
    assert await DBMemIndexOutbox.filter(state="pending").count() == 1


async def test_unrelated_query_does_not_return_latest_fact() -> None:
    service = MemoryService()
    await service.propose_fact(context(), await proposal("喜欢绿茶"), idempotency_key="a")
    assert not (await service.recall(context(), query="居住城市")).items


async def test_semantic_fusion_rejects_unknown_id_old_revision_and_duplicates() -> None:
    service, backend = MemoryService(), fake_index()
    await service.propose_fact(context(), await proposal("绿茶"), idempotency_key="a")
    bundle = await service.recall(context())
    key = FactIndexKey(assertion_id=bundle.items[0].assertion_id, revision=1)
    backend.search.return_value = [
        FactIndexKey(assertion_id=uuid4(), revision=1),
        FactIndexKey(assertion_id=key.assertion_id, revision=999), key, key,
    ]
    result = await rank_facts("beverage", bundle.items, limit=20, index=backend)
    assert result == bundle.items
    assert backend.search.call_args.args[1] == [key]


async def test_external_failure_keeps_local_results() -> None:
    service, backend = MemoryService(), fake_index()
    await service.propose_fact(context(), await proposal("绿茶"), idempotency_key="a")
    bundle = await service.recall(context())
    backend.search.side_effect = OSError("index unavailable")
    assert await rank_facts("绿茶", bundle.items, limit=20, index=backend) == bundle.items


async def test_sql_authorization_precedes_vector_search(monkeypatch: pytest.MonkeyPatch) -> None:
    service, backend = MemoryService(), fake_index()
    await service.propose_fact(context(), await proposal("private"), idempotency_key="a")
    await service.propose_fact(context("group_a"), await proposal("public", chat_key="group_a"), idempotency_key="b")
    high = (await proposal("gated", predicate="secret")).model_copy(update={"min_favor": 80})
    await service.propose_fact(context(), high, idempotency_key="c")
    monkeypatch.setattr(config, "MEMORY_FACT_INDEX_ENABLED", True)
    monkeypatch.setattr(fact_retrieval, "QdrantFactIndex", lambda: backend)
    await service.recall(context(), query="private")
    allowed = backend.search.call_args.args[1]
    assert len(allowed) == 1
    row = await DBMemAssertion.get(id=allowed[0].assertion_id)
    assert row.value_json == {"value": "private"}


async def test_deleted_fact_never_reaches_vector_allowed_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    service, backend = MemoryService(), fake_index()
    result = await service.propose_fact(context(), await proposal(), idempotency_key="a")
    await service.forget(context(), result.assertion_id)
    monkeypatch.setattr(config, "MEMORY_FACT_INDEX_ENABLED", True)
    monkeypatch.setattr(fact_retrieval, "QdrantFactIndex", lambda: backend)
    assert not (await service.recall(context(), query="tea")).items
    backend.search.assert_not_called()


async def test_source_recall_during_vector_search_invalidates_result(monkeypatch: pytest.MonkeyPatch) -> None:
    service, backend = MemoryService(), fake_index()
    fact = await proposal("tea")
    result = await service.propose_fact(context(), fact, idempotency_key="a")

    async def revoke(query: str, allowed: list[FactIndexKey], limit: int) -> list[FactIndexKey]:
        await DBChatMessage.filter(id=int(fact.source.id)).update(is_recalled=True)
        return [FactIndexKey(assertion_id=result.assertion_id, revision=1)]

    backend.search.side_effect = revoke
    monkeypatch.setattr(config, "MEMORY_FACT_INDEX_ENABLED", True)
    monkeypatch.setattr(fact_retrieval, "QdrantFactIndex", lambda: backend)
    with pytest.raises(MemoryError, match="SOURCE_INVALID"):
        await service.recall(context(), query="tea")


async def test_bundle_validity_window_rechecked_before_send() -> None:
    service = MemoryService()
    result = await service.propose_fact(context(), await proposal(), idempotency_key="a")
    bundle = await service.recall(context())
    await DBMemAssertion.filter(id=result.assertion_id).update(valid_to=datetime.now(timezone.utc) - timedelta(seconds=1))
    with pytest.raises(MemoryError, match="POLICY_CHANGED"):
        await service.validate_bundle(context(), bundle)


async def test_rebuild_explicitly_enqueues_existing_rows() -> None:
    service = MemoryService()
    await service.propose_fact(context(), await proposal(), idempotency_key="a")
    await DBMemIndexOutbox.all().update(state="done")
    assert await FactIndexWorker(fake_index()).rebuild() == 1
    assert await DBMemIndexOutbox.filter(state="pending", operation="reconcile").count() == 1


@pytest.mark.skipif(not os.environ.get("MEMORY_TEST_QDRANT_URL"), reason="需要显式指定隔离 Qdrant")
async def test_real_qdrant_version_ids_filtered_search_and_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    assert os.environ["MEMORY_TEST_QDRANT_URL"] == "http://127.0.0.1:6333"
    client = AsyncQdrantClient(url=os.environ["MEMORY_TEST_QDRANT_URL"])
    monkeypatch.setattr(fact_index, "get_qdrant_client", AsyncMock(return_value=client))
    monkeypatch.setattr(QdrantFactIndex, "_assert_configuration", lambda self: None)
    backend = object.__new__(QdrantFactIndex)
    backend.collection = f"memory_test_{uuid4().hex}"
    backend.dimension = 3
    backend.embedder = AsyncMock()
    backend.embedder.embed_text.return_value = [1.0, 0.0, 0.0]
    key = FactIndexKey(assertion_id=uuid4(), revision=1)
    other = FactIndexKey(assertion_id=uuid4(), revision=1)
    try:
        await backend.upsert(key, "private text")
        await backend.upsert(other, "other text")
        assert await backend.search("query", [key], 10) == [key]
        points, _ = await client.scroll(backend.collection, with_vectors=False)
        assert len(points) == 2
        assert all(set(point.payload) == {"assertion_id", "revision"} for point in points)
        await backend.delete(key.assertion_id)
        assert not await backend.search("query", [key], 10)
        assert await backend.search("query", [other], 10) == [other]
    finally:
        await client.delete_collection(backend.collection)
        await client.close()


async def test_configuration_changed_during_embedding_cannot_write_wrong_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fact_index, "_configuration_identity", lambda: "config-a")
    backend = QdrantFactIndex()
    client = AsyncMock()
    monkeypatch.setattr(fact_index, "get_qdrant_client", AsyncMock(return_value=client))

    async def changed(text: str) -> list[float]:
        monkeypatch.setattr(fact_index, "_configuration_identity", lambda: "config-b")
        return [1.0, 0.0, 0.0]

    backend.embedder = AsyncMock()
    backend.embedder.embed_text.side_effect = changed
    with pytest.raises(MemoryError, match="INDEX_CONFIG_CHANGED"):
        await backend.upsert(FactIndexKey(assertion_id=uuid4(), revision=1), "text")
    client.upsert.assert_not_called()


@pytest.mark.skipif(not os.environ.get("MEMORY_TEST_QDRANT_URL"), reason="需要显式指定隔离 Qdrant")
async def test_qdrant_dimension_mismatch_is_not_destructively_recreated(monkeypatch: pytest.MonkeyPatch) -> None:
    client = AsyncQdrantClient(url="http://127.0.0.1:6333")
    monkeypatch.setattr(fact_index, "get_qdrant_client", AsyncMock(return_value=client))
    backend = object.__new__(QdrantFactIndex)
    backend.collection = f"memory_test_{uuid4().hex}"
    backend.dimension = 3
    try:
        await client.create_collection(backend.collection, vectors_config=qm.VectorParams(size=2, distance=qm.Distance.COSINE))
        with pytest.raises(MemoryError, match="INDEX_CONFIG_MISMATCH"):
            await backend.ensure()
        assert (await client.get_collection(backend.collection)).config.params.vectors.size == 2
    finally:
        await client.delete_collection(backend.collection)
        await client.close()
