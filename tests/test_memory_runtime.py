"""验证真实消息补扫、灰度写入门、抽取边界与运行生命周期。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from test_memory_fact_service import DB_URL, context, isolated_db, proposal

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_fact import DBMemAssertion, DBMemWriteJob
from nekro_agent.services.memory import fact_extractor
from nekro_agent.services.memory.contracts import MemoryError
from nekro_agent.services.memory.ingest_worker import ExtractionInput, ExtractionResult, MemoryIngestQueue
from nekro_agent.services.memory.routing import get_memory_route, legacy_fact_write
from nekro_agent.services.memory.runtime import MemoryFactRuntime
from nekro_agent.services.memory.service import MemoryService

__all__ = ["isolated_db"]
pytestmark = pytest.mark.skipif(not DB_URL, reason="需要显式指定隔离 PostgreSQL")


@pytest.fixture(autouse=True)
def runtime_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_FACT_AGENT_ID", "test-agent")
    monkeypatch.setattr(config, "MEMORY_FACT_WORKER_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_FACT_SHADOW_MODEL_ALLOWED", False)


async def activate(mode: str = "service") -> None:
    await MemoryIngestQueue().set_mode(context(is_admin=True, purpose="admin_review"), mode, expected_writer_revision=1)


async def test_route_uses_explicit_channel_workspace_and_agent() -> None:
    assert await get_memory_route("private_alice") is None
    await activate()
    route = await get_memory_route("private_alice")
    assert route.uses_service and route.workspace_id == 1
    assert await get_memory_route("group_a") is None
    assert await get_memory_route("unknown") is None


async def test_worker_disabled_does_not_restore_legacy_write_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    await activate()
    monkeypatch.setattr(config, "MEMORY_FACT_WORKER_ENABLED", False)
    with pytest.raises(MemoryError, match="LEGACY_WRITER_DISABLED"):
        async with legacy_fact_write("private_alice"):
            raise AssertionError("must not run")
    assert not await MemoryFactRuntime().enqueue_message(1)


async def test_legacy_guard_and_cutover_use_same_commit_lock() -> None:
    queue = MemoryIngestQueue()
    inside = asyncio.Event()
    release = asyncio.Event()

    async def legacy_write() -> None:
        async with legacy_fact_write("private_alice"):
            inside.set()
            await release.wait()

    legacy = asyncio.create_task(legacy_write())
    await inside.wait()
    switch = asyncio.create_task(queue.set_mode(context(is_admin=True, purpose="admin_review"), "service", expected_writer_revision=1))
    await asyncio.sleep(0.05)
    assert not switch.done()
    release.set()
    await legacy
    await switch
    with pytest.raises(MemoryError, match="LEGACY_WRITER_DISABLED"):
        async with legacy_fact_write("private_alice"):
            pass


async def test_shadow_keeps_legacy_primary_but_no_real_model_without_consent() -> None:
    await activate("shadow")
    async with legacy_fact_write("private_alice"):
        pass
    item = await proposal()
    assert not await MemoryFactRuntime().enqueue_message(int(item.source.id))
    assert not await DBMemWriteJob.all().exists()


async def test_message_notification_enqueues_exact_committed_message() -> None:
    from nekro_agent.services.message_service import _notify_memory_scheduler

    await activate()
    item = await proposal()
    await _notify_memory_scheduler("private_alice", 1, 20, message_id=int(item.source.id))
    assert await DBMemWriteJob.all().count() == 1


async def test_rescan_covers_more_than_thirty_and_late_lower_id() -> None:
    await activate()
    runtime = MemoryFactRuntime()
    items = [await proposal(str(i)) for i in range(35)]
    route = await get_memory_route("private_alice")
    cursor, more = await runtime.scan_page(route, page_size=20)
    assert more and await DBMemWriteJob.all().count() == 20
    await runtime.scan_page(route, after_id=cursor, page_size=20)
    assert await DBMemWriteJob.all().count() == 35
    # 模拟先前未提交／未可见的较低 ID：删除任务后重新全扫，不依赖持久高水位。
    job = await DBMemWriteJob.all().order_by("created_at").first()
    await job.delete()
    await runtime.scan_page(route, after_id=0, page_size=100)
    assert await DBMemWriteJob.all().count() == 35
    assert items[0].source.id.isdecimal()


async def test_bot_recalled_and_unbound_sources_are_not_enqueued() -> None:
    await activate()
    runtime = MemoryFactRuntime()
    bot = await proposal()
    await DBChatMessage.filter(id=int(bot.source.id)).update(sender_id="-1")
    assert not await runtime.enqueue_message(int(bot.source.id))
    recalled = await proposal()
    await DBChatMessage.filter(id=int(recalled.source.id)).update(is_recalled=True)
    assert not await runtime.enqueue_message(int(recalled.source.id))
    other = await proposal(chat_key="group_a")
    assert not await runtime.enqueue_message(int(other.source.id))


async def test_restart_recovers_persistent_job_and_no_duplicate_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    await activate()
    item = await proposal()
    runtime = MemoryFactRuntime()
    await runtime.enqueue_message(int(item.source.id))
    extractor = AsyncMock(return_value=ExtractionResult())
    monkeypatch.setattr(fact_extractor, "extract_message_facts", extractor)
    restarted = MemoryFactRuntime()
    await restarted.tick()
    await restarted.tick()
    assert extractor.await_count == 1
    assert await DBMemWriteJob.filter(state="done").count() == 1


async def test_disabled_runtime_does_not_start_background_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MEMORY_FACT_WORKER_ENABLED", False)
    runtime = MemoryFactRuntime()
    await runtime.start()
    assert runtime._task is None
    await runtime.stop()


async def test_relationship_expression_gate_follows_permissions_first() -> None:
    service = MemoryService()
    item = (await proposal()).model_copy(update={"min_favor": 60})
    await service.propose_fact(context(), item, idempotency_key="gated")
    assert not (await service.recall(context())).items
    assert not (await service.recall(context(), expression_ceiling=59)).items
    assert len((await service.recall(context(), expression_ceiling=100)).items) == 1
    assert not (await service.recall(context("group_a"), expression_ceiling=100)).items
    assert len((await service.recall(context(is_admin=True, purpose="admin_review"))).items) == 1


async def test_automatic_correction_cannot_lower_expression_gate() -> None:
    service = MemoryService()
    await service.propose_fact(context(), (await proposal()).model_copy(update={"min_favor": 80}), idempotency_key="first")
    await service.propose_fact(context(), await proposal("coffee"), idempotency_key="correct")
    assert not (await service.recall(context())).items
    assert (await service.recall(context(), expression_ceiling=100)).items[0].min_favor == 80


async def run_fake_extraction(monkeypatch: pytest.MonkeyPatch, facts: list[dict[str, object]]) -> ExtractionResult:
    from nekro_agent.services.agent import openai

    group = SimpleNamespace(CHAT_MODEL="offline", BASE_URL="http://invalid.test", API_KEY="synthetic")
    monkeypatch.setattr(DBChatChannel, "get_effective_config", AsyncMock(return_value=SimpleNamespace(USE_MODEL_GROUP="x", MODEL_GROUPS={"x": group})))
    monkeypatch.setattr(openai, "gen_openai_chat_response", AsyncMock(return_value=SimpleNamespace(response_content=json.dumps({"facts": facts}))))
    item = await proposal()
    source = ExtractionInput(source=item.source, speaker_key="onebot_v11:alice", chat_key="private_alice", text="I prefer tea")
    return await fact_extractor.extract_message_facts(source)


def candidate(**updates: object) -> dict[str, object]:
    return {"predicate": "drink", "value": "tea", "confidence": 0.9, "evidence_quote": "prefer tea", "self_declared": True, "about_agent": False, "manipulation": False, "is_roleplay": False, **updates}


async def test_model_cannot_choose_actor_or_source(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await run_fake_extraction(monkeypatch, [candidate()])
    assert result.facts[0].proposal.subject_key == "onebot_v11:alice"
    with pytest.raises(ValidationError):
        await run_fake_extraction(monkeypatch, [candidate(subject_key="admin")])


@pytest.mark.parametrize("flag", ["about_agent", "manipulation", "is_roleplay"])
async def test_persona_and_manipulation_gate_prevents_publication(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    result = await run_fake_extraction(monkeypatch, [candidate(**{flag: True})])
    assert not result.facts
    assert not await DBMemAssertion.all().exists()


async def test_missing_gate_field_and_false_quote_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    item = candidate()
    item.pop("about_agent")
    with pytest.raises(ValidationError):
        await run_fake_extraction(monkeypatch, [item])
    with pytest.raises(MemoryError, match="SOURCE_INVALID"):
        await run_fake_extraction(monkeypatch, [candidate(evidence_quote="invented quotation")])


async def test_low_confidence_and_third_party_inference_are_not_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not (await run_fake_extraction(monkeypatch, [candidate(confidence=0.2), candidate(self_declared=False)])).facts


async def test_service_history_reads_new_authority_and_never_calls_legacy_retriever(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services.agent.templates import history
    from nekro_agent.services.memory import retriever

    await activate()
    await MemoryService().propose_fact(context(), await proposal("new-authority"), idempotency_key="new")
    old = AsyncMock(side_effect=AssertionError("legacy recall must not run"))
    monkeypatch.setattr(retriever, "retrieve_memories", old)
    result = await history._inject_memory_context(1, [], max_length=4096, chat_key="private_alice")
    assert "new-authority" in result.text and old.await_count == 0


async def test_service_mode_does_not_run_legacy_core_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services.memory.consolidator import EpisodicConsolidator

    await activate()
    old = AsyncMock(side_effect=AssertionError("legacy model call must not run"))
    consolidator = EpisodicConsolidator(1)
    monkeypatch.setattr(consolidator, "load_persona_context", old)
    result = await consolidator.consolidate("private_alice")
    assert result.paragraphs_created == 0 and old.await_count == 0


async def test_source_limit_and_secret_source_do_not_call_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services.agent import openai

    call = AsyncMock(side_effect=AssertionError("no model request"))
    monkeypatch.setattr(openai, "gen_openai_chat_response", call)
    item = await proposal()
    source = ExtractionInput(source=item.source, speaker_key="onebot_v11:alice", chat_key="private_alice", text="password=synthetic-private-value")
    assert not (await fact_extractor.extract_message_facts(source)).facts
    with pytest.raises(MemoryError, match="SOURCE_TOO_LARGE"):
        await fact_extractor.extract_message_facts(source.model_copy(update={"text": "x" * (config.MEMORY_FACT_SOURCE_MAX_CHARS + 1)}))
    assert call.await_count == 0


async def test_source_role_change_after_enqueue_is_rejected_before_model(monkeypatch: pytest.MonkeyPatch) -> None:
    await activate()
    runtime = MemoryFactRuntime()
    item = await proposal()
    await runtime.enqueue_message(int(item.source.id))
    await DBChatMessage.filter(id=int(item.source.id)).update(sender_id="-1")
    call = AsyncMock(side_effect=AssertionError("source identity changed"))
    monkeypatch.setattr(fact_extractor, "extract_message_facts", call)
    await runtime.tick()
    assert call.await_count == 0
    assert await DBMemWriteJob.filter(state="cancelled", error_class="SOURCE_INVALID").count() == 1
