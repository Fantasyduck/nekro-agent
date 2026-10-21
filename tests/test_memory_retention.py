"""遗忘回执、正文清理与死信运维的隔离数据库测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from test_memory_fact_service import DB_URL, isolated_db, proposal
from test_memory_fact_service import context as base_context

from nekro_agent.core.config import config
from nekro_agent.models.db_mem_fact import (
    DBMemAssertion,
    DBMemAuditEvent,
    DBMemIndexOutbox,
    DBMemWriteJob,
)
from nekro_agent.services.memory.contracts import MemoryError, RequestContext
from nekro_agent.services.memory.dead_letter import DeadLetterAdmin
from nekro_agent.services.memory.service import MemoryService

__all__ = ["isolated_db"]

pytestmark = pytest.mark.skipif(not DB_URL, reason="需要显式指定隔离 PostgreSQL")


def admin_context(chat_key: str = "private_alice", **kwargs: object):
    return base_context(chat_key=chat_key, is_admin=True, purpose="admin_review", **kwargs)


async def test_forget_receipt_separates_index_and_purge_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MEMORY_FACT_PURGE_AFTER_DAYS", 0)
    service = MemoryService()
    result = await service.propose_fact(base_context(), await proposal("tea"), idempotency_key="first")

    receipt = await service.forget(admin_context(), result.assertion_id)

    assert receipt.logical_block_committed is True
    assert receipt.affected_assertions == 1
    assert receipt.derived_cleanup_pending is True
    # 逻辑遗忘同时排队了向量撤除任务，因此不能宣称索引已清。
    assert receipt.index_cleanup_pending is True
    assert receipt.physical_purge_state == "disabled"
    # 创建与遗忘各排一条待投影任务：索引撤除绝不能因为逻辑删除已提交就算完成。
    assert await DBMemIndexOutbox.filter(state="pending").count() == 2


async def test_purge_waits_for_retention_then_clears_body_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MEMORY_FACT_PURGE_AFTER_DAYS", 7)
    service = MemoryService()
    result = await service.propose_fact(base_context(), await proposal("tea"), idempotency_key="first")
    receipt = await service.forget(admin_context(), result.assertion_id)
    assert receipt.physical_purge_state == "scheduled"

    # 保留期未到：一条都不能清。
    assert await service.purge_forgotten(admin_context()) == 0
    row = await DBMemAssertion.get(id=result.assertion_id)
    assert row.value_json == {"value": "tea"}

    old = datetime.now(timezone.utc) - timedelta(days=30)
    await DBMemAuditEvent.filter(target_id=result.assertion_id, operation="deleted").update(created_at=old)
    assert await service.purge_forgotten(admin_context()) == 1
    row = await DBMemAssertion.get(id=result.assertion_id)
    assert row.value_json == {"value": None, "purged": True}
    # 重复清理幂等：不留第二条审计，也不再计数。
    assert await service.purge_forgotten(admin_context()) == 0
    assert await DBMemAuditEvent.filter(operation="fact.purged").count() == 1

    detail = await service.get_fact(admin_context(), result.assertion_id)
    assert detail.current.value is None
    assert receipt.affected_assertions == 1


async def test_purge_requires_admin_review_and_positive_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MEMORY_FACT_PURGE_AFTER_DAYS", 0)
    service = MemoryService()
    with pytest.raises(MemoryError, match="SCOPE_DENIED"):
        await service.purge_forgotten(base_context())
    monkeypatch.setattr(config, "MEMORY_FACT_PURGE_AFTER_DAYS", 7)
    with pytest.raises(MemoryError, match="LIMIT_INVALID"):
        await service.purge_forgotten(admin_context(), limit=0)


async def test_dead_letter_index_can_be_retried_or_ignored() -> None:
    service = MemoryService()
    result = await service.propose_fact(base_context(), await proposal("tea"), idempotency_key="first")
    admin = DeadLetterAdmin(context=admin_context())

    row = await DBMemIndexOutbox.filter(assertion_id=result.assertion_id).first()
    await DBMemIndexOutbox.filter(id=row.id).update(state="dead_letter", attempts=5)

    page = await admin.list(kind="index")
    assert page.total == 1
    assert page.items[0].state == "dead_letter"
    assert page.items[0].assertion_id == result.assertion_id

    assert await admin.retry(kind="index", item_id=row.id) is True
    retried = await DBMemIndexOutbox.get(id=row.id)
    # attempts 必须归零，否则下一轮领取又会立即判为穷尽。
    assert retried.state == "pending" and retried.attempts == 0

    await DBMemIndexOutbox.filter(id=row.id).update(state="dead_letter")
    assert await admin.ignore(kind="index", item_id=row.id) is True
    assert (await DBMemIndexOutbox.get(id=row.id)).state == "cancelled"
    assert await DBMemAuditEvent.filter(operation__in=["index.retried", "index.ignored"]).count() == 2


async def test_dead_letter_covers_ingest_jobs_without_leaking_payload() -> None:
    service = MemoryService()
    scope = await service.resolve_scope(base_context())
    job = await DBMemWriteJob.create(
        scope_id=scope.id, observation_id=scope.id, pipeline_version="test-v1", writer_revision=1,
        mode="service", state="dead_letter", attempts=5, error_class="EXTRACTOR_FAILED",
        shadow_result={"facts": ["正文不应出现在运维响应里"]},
    )
    admin = DeadLetterAdmin(context=admin_context())

    page = await admin.list(kind="ingest")
    assert page.total == 1
    item = page.items[0]
    assert item.id == job.id and item.error_class == "EXTRACTOR_FAILED"
    assert "正文不应出现在运维响应里" not in page.model_dump_json()

    assert await admin.retry(kind="ingest", item_id=job.id) is True
    retried = await DBMemWriteJob.get(id=job.id)
    assert retried.state == "pending" and retried.attempts == 0 and retried.error_class is None


async def test_dead_letter_rejects_unknown_kind_and_outside_scope() -> None:
    admin = DeadLetterAdmin(context=admin_context())
    with pytest.raises(MemoryError, match="KIND_INVALID"):
        await admin.list(kind="unknown")
    # 频道与工作区不匹配时不能跨 scope 运维别人的死信。
    mismatched = RequestContext(
        workspace_id=2, agent_id="test-agent", actor_key="web:admin",
        chat_key="group_a", purpose="admin_review", is_admin=True,
    )
    with pytest.raises(MemoryError, match="SCOPE_UNRESOLVED"):
        await DeadLetterAdmin(context=mismatched).list(kind="index")
    with pytest.raises(MemoryError, match="SCOPE_DENIED"):
        DeadLetterAdmin(context=base_context())
