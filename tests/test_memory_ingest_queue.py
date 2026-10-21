"""以真实数据库验证租约、影子隔离、原子完成和按消费者交接。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from test_memory_fact_service import DB_URL, context, isolated_db, proposal

from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_fact import (
    DBMemAssertion,
    DBMemDomainOutbox,
    DBMemEventDelivery,
    DBMemScope,
    DBMemWriteJob,
)
from nekro_agent.services.memory.contracts import MemoryError
from nekro_agent.services.memory.domain_events import DomainEventBroker
from nekro_agent.services.memory.ingest_worker import (
    ExtractedFact,
    ExtractionResult,
    MemoryIngestQueue,
)

__all__ = ["isolated_db"]
pytestmark = pytest.mark.skipif(not DB_URL, reason="需要显式指定隔离 PostgreSQL")


async def setup_queue(mode: str = "service") -> tuple[MemoryIngestQueue, DBMemScope]:
    queue = MemoryIngestQueue()
    admin = context(is_admin=True, purpose="admin_review")
    await queue.set_mode(admin, mode, expected_writer_revision=1)
    return queue, await queue.service.resolve_scope(admin)


async def test_disabled_pipeline_does_not_enqueue_or_publish() -> None:
    queue = MemoryIngestQueue()
    item = await proposal()
    with pytest.raises(MemoryError, match="PIPELINE_DISABLED"):
        await queue.enqueue(context(purpose="extract"), item.source, pipeline_version="v1")
    assert not await DBMemWriteJob.all().exists()


async def test_enqueue_is_persistent_and_idempotent() -> None:
    queue, scope = await setup_queue()
    item = await proposal()
    ids = await asyncio.gather(*[
        queue.enqueue(context(purpose="extract"), item.source, pipeline_version="v1") for _ in range(4)
    ])
    assert len(set(ids)) == 1 and await DBMemWriteJob.all().count() == 1
    assert (await MemoryIngestQueue().claim(scope_id=scope.id)).id == ids[0]


async def test_backlog_over_thirty_is_fully_processed() -> None:
    queue, scope = await setup_queue()
    for i in range(35):
        item = await proposal(str(i))
        await queue.enqueue(context(purpose="extract"), item.source, pipeline_version="v1")
    processed = 0
    while lease := await queue.claim(scope_id=scope.id):
        await queue.complete(lease, ExtractionResult())
        processed += 1
    assert processed == 35 and await DBMemWriteJob.filter(state="done").count() == 35


async def test_same_scope_has_only_one_inflight_extraction() -> None:
    queue, scope = await setup_queue()
    for i in range(3):
        item = await proposal(str(i))
        await queue.enqueue(context(purpose="extract"), item.source, pipeline_version="v1")
    leases = await asyncio.gather(*[queue.claim(scope_id=scope.id) for _ in range(3)])
    claimed = [lease for lease in leases if lease is not None]
    assert len(claimed) == 1
    await queue.complete(claimed[0], ExtractionResult())
    assert await queue.claim(scope_id=scope.id) is not None


async def test_exhausted_old_job_does_not_block_later_backlog() -> None:
    queue, scope = await setup_queue()
    for i in range(2):
        item = await proposal(str(i))
        await queue.enqueue(context(purpose="extract"), item.source, pipeline_version="v1")
    first = await queue.claim(scope_id=scope.id)
    await DBMemWriteJob.filter(id=first.id).update(
        attempts=5, lease_until=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    second = await queue.claim(scope_id=scope.id)
    assert second is not None and second.id != first.id
    assert (await DBMemWriteJob.get(id=first.id)).state == "dead_letter"


async def test_expired_lease_is_reclaimed_and_old_owner_cannot_commit() -> None:
    queue, scope = await setup_queue()
    item = await proposal()
    await queue.enqueue(context(purpose="extract"), item.source, pipeline_version="v1")
    first = await queue.claim(scope_id=scope.id)
    await DBMemWriteJob.filter(id=first.id).update(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    second = await MemoryIngestQueue().claim(scope_id=scope.id)
    assert second.token != first.token and second.attempts == 2
    with pytest.raises(MemoryError, match="LEASE_LOST"):
        await queue.complete(first, ExtractionResult())
    with pytest.raises(MemoryError, match="LEASE_LOST"):
        await queue.renew(first)
    await queue.complete(second, ExtractionResult())


async def test_shadow_results_cannot_become_formal_facts() -> None:
    queue, scope = await setup_queue("shadow")
    item = await proposal()
    await queue.enqueue(context(purpose="extract"), item.source, pipeline_version="v1")
    lease = await queue.claim(scope_id=scope.id)
    await queue.complete(lease, ExtractionResult(facts=[ExtractedFact(proposal=item, evidence_quote="tea")]))
    assert (await DBMemWriteJob.get(id=lease.id)).state == "shadow_done"
    assert not await DBMemAssertion.all().exists()
    assert not (await queue.service.recall(context())).items


async def test_service_completion_publishes_and_acknowledges_atomically() -> None:
    queue, scope = await setup_queue()
    item = await proposal()
    await queue.enqueue(context(purpose="extract"), item.source, pipeline_version="v1")
    lease = await queue.claim(scope_id=scope.id)
    await queue.complete(lease, ExtractionResult(facts=[ExtractedFact(proposal=item, evidence_quote="tea")]))
    assert (await DBMemWriteJob.get(id=lease.id)).state == "done"
    assert [row.value for row in (await queue.service.recall(context())).items] == ["tea"]


async def test_invalid_candidate_rolls_back_whole_batch() -> None:
    queue, scope = await setup_queue()
    item = await proposal()
    await queue.enqueue(context(purpose="extract"), item.source, pipeline_version="v1")
    lease = await queue.claim(scope_id=scope.id)
    invalid = item.model_copy(update={"predicate": "permissions"})
    with pytest.raises(MemoryError, match="RESERVED_DOMAIN"):
        await queue.complete(lease, ExtractionResult(facts=[
            ExtractedFact(proposal=item, evidence_quote="tea"), ExtractedFact(proposal=invalid, evidence_quote="tea"),
        ]))
    assert not await DBMemAssertion.all().exists()
    assert (await DBMemWriteJob.get(id=lease.id)).state == "running"


async def test_switch_fences_old_extraction_and_old_admin_operation() -> None:
    queue, scope = await setup_queue()
    item = await proposal()
    await queue.enqueue(context(purpose="extract"), item.source, pipeline_version="v1")
    lease = await queue.claim(scope_id=scope.id)
    await queue.set_mode(context(is_admin=True, purpose="admin_review"), "legacy_guarded", expected_writer_revision=scope.writer_revision)
    with pytest.raises(MemoryError, match="WRITER_FENCED"):
        await queue.complete(lease, ExtractionResult())
    with pytest.raises(MemoryError, match="WRITER_FENCED"):
        await queue.set_mode(context(is_admin=True, purpose="admin_review"), "service", expected_writer_revision=scope.writer_revision)
    assert not await DBMemAssertion.all().exists()


async def test_deleted_source_is_not_given_to_extractor() -> None:
    queue, scope = await setup_queue()
    item = await proposal()
    await queue.enqueue(context(purpose="extract"), item.source, pipeline_version="v1")
    await DBChatMessage.filter(id=int(item.source.id)).update(is_recalled=True)
    called = False

    async def extract(source: object) -> ExtractionResult:
        nonlocal called
        called = True
        return ExtractionResult()

    await queue.process_one(scope_id=scope.id, extractor=extract)
    assert not called
    assert await DBMemWriteJob.filter(state="cancelled", error_class="SOURCE_INVALID").count() == 1


async def test_false_quote_or_other_source_is_rejected() -> None:
    queue, scope = await setup_queue()
    item = await proposal()
    await queue.enqueue(context(purpose="extract"), item.source, pipeline_version="v1")
    lease = await queue.claim(scope_id=scope.id)
    with pytest.raises(MemoryError, match="SOURCE_INVALID"):
        await queue.complete(lease, ExtractionResult(facts=[ExtractedFact(proposal=item, evidence_quote="not in source")]))
    assert not await DBMemAssertion.all().exists()


async def test_failure_is_recoverable_and_retries_are_bounded() -> None:
    queue, scope = await setup_queue()
    item = await proposal()
    await queue.enqueue(context(purpose="extract"), item.source, pipeline_version="v1")
    lease = await queue.claim(scope_id=scope.id)
    await queue.fail(lease, error_class="MODEL_TIMEOUT")
    assert await queue.claim(scope_id=scope.id) is None
    await DBMemWriteJob.filter(id=lease.id).update(next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=1), attempts=4)
    lease = await queue.claim(scope_id=scope.id)
    await queue.fail(lease, error_class="MODEL_TIMEOUT")
    assert (await DBMemWriteJob.get(id=lease.id)).state == "dead_letter"


async def test_unexpected_model_error_keeps_retry_state() -> None:
    queue, scope = await setup_queue()
    item = await proposal()
    await queue.enqueue(context(purpose="extract"), item.source, pipeline_version="v1")

    async def fail(source: object) -> ExtractionResult:
        raise RuntimeError("synthetic timeout")

    with pytest.raises(RuntimeError):
        await queue.process_one(scope_id=scope.id, extractor=fail)
    assert await DBMemWriteJob.filter(state="pending", error_class="EXTRACTOR_FAILED").count() == 1


async def test_multiple_consumers_acknowledge_independently() -> None:
    queue, scope = await setup_queue()
    await queue.service.propose_fact(context(), await proposal(), idempotency_key="fact")
    broker = DomainEventBroker()
    event_count = await DBMemDomainOutbox.filter(scope_id=scope.id).count()
    assert await broker.prepare(consumer_id="angel", scope_id=scope.id) == event_count
    assert await broker.prepare(consumer_id="angel", scope_id=scope.id) == 0
    assert await broker.prepare(consumer_id="audit", scope_id=scope.id) == event_count
    lease = await broker.claim(consumer_id="angel")
    await broker.acknowledge(lease)
    assert await DBMemEventDelivery.filter(consumer_id="audit", state="done").count() == 0
    assert await DBMemEventDelivery.filter(consumer_id="angel", state="done").count() == 1
    with pytest.raises(MemoryError, match="LEASE_LOST"):
        await broker.acknowledge(lease.model_copy(update={"token": uuid4()}))


async def test_expired_event_lease_is_reclaimed_and_old_ack_denied() -> None:
    queue, scope = await setup_queue()
    broker = DomainEventBroker()
    await broker.prepare(consumer_id="angel", scope_id=scope.id)
    first = await broker.claim(consumer_id="angel")
    await DBMemEventDelivery.filter(id=first.id).update(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    second = await broker.claim(consumer_id="angel")
    assert second.event_id == first.event_id and second.token != first.token
    with pytest.raises(MemoryError, match="LEASE_LOST"):
        await broker.acknowledge(first)
    await broker.acknowledge(second)
