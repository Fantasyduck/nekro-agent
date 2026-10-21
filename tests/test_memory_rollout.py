"""按频道灰度切换的隔离数据库测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from test_memory_fact_service import DB_URL, isolated_db
from test_memory_fact_service import context as base_context

from nekro_agent.core.config import config
from nekro_agent.models.db_mem_fact import DBMemAuditEvent, DBMemScope, DBMemWriteJob
from nekro_agent.services.memory.contracts import MemoryError, RequestContext
from nekro_agent.services.memory.rollout import RolloutService
from nekro_agent.services.memory.service import MemoryService

__all__ = ["isolated_db"]

pytestmark = pytest.mark.skipif(not DB_URL, reason="需要显式指定隔离 PostgreSQL")


def admin_context(chat_key: str = "private_alice", **kwargs: object) -> RequestContext:
    return base_context(chat_key=chat_key, is_admin=True, purpose="admin_review", **kwargs)


async def ensure_scope(chat_key: str = "private_alice") -> DBMemScope:
    return await MemoryService().resolve_scope(base_context(chat_key=chat_key))


async def test_report_blocks_when_a_job_holds_a_lease() -> None:
    scope = await ensure_scope()
    # 造一个仍在租约内的任务：此时切换所有权会让新旧两侧同时写同一事实。
    await DBMemWriteJob.create(
        scope_id=scope.id, observation_id=scope.id, pipeline_version="rollout-v1",
        writer_revision=scope.writer_revision, mode="shadow", state="running",
        lease_token="00000000-0000-0000-0000-000000000001",
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    report = await RolloutService().report(admin_context())
    assert report.running_jobs == 1
    assert report.ready is False
    assert "in_flight_jobs" in report.blockers


async def test_switch_refuses_while_leased_unless_isolating() -> None:
    scope = await ensure_scope()
    await DBMemWriteJob.create(
        scope_id=scope.id, observation_id=scope.id, pipeline_version="rollout-v1",
        writer_revision=scope.writer_revision, mode="shadow", state="running",
        lease_token="00000000-0000-0000-0000-000000000002",
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    service = RolloutService()

    with pytest.raises(MemoryError, match="ROLLOUT_BUSY"):
        await service.switch(
            admin_context(), mode="service",
            expected_writer_revision=scope.writer_revision, reason="test",
        )

    # 显式要求隔离租约时才允许继续，且旧租约必须被退回待领取。
    after = await service.switch(
        admin_context(), mode="service",
        expected_writer_revision=scope.writer_revision, isolate_leases=True, reason="rollback",
    )
    assert after.mode == "service"
    assert after.writer_revision == scope.writer_revision + 1
    isolated = await DBMemWriteJob.get(scope_id=scope.id)
    assert isolated.state == "pending" and isolated.lease_token is None
    assert isolated.error_class == "LEASE_ISOLATED"
    assert await DBMemAuditEvent.filter(operation="pipeline.leases_isolated").exists()
    assert await DBMemAuditEvent.filter(operation="pipeline.service").exists()


async def test_switch_requires_matching_revision_and_is_idempotent() -> None:
    scope = await ensure_scope()
    service = RolloutService()

    with pytest.raises(MemoryError, match="WRITER_FENCED"):
        await service.switch(
            admin_context(), mode="shadow",
            expected_writer_revision=scope.writer_revision + 5, reason="test",
        )

    first = await service.switch(
        admin_context(), mode="shadow",
        expected_writer_revision=scope.writer_revision, reason="test",
    )
    assert first.mode == "shadow" and first.writer_revision == scope.writer_revision + 1

    # 同模式的重复调用不应再抬版本，否则会把并发切换的 fencing 语义搅乱。
    again = await service.switch(
        admin_context(), mode="shadow",
        expected_writer_revision=first.writer_revision, reason="test",
    )
    assert again.writer_revision == first.writer_revision


async def test_switch_rejects_admin_review_only_and_unknown_mode() -> None:
    service = RolloutService()
    with pytest.raises(MemoryError, match="SCOPE_DENIED"):
        await service.report(base_context())
    with pytest.raises(MemoryError, match="PIPELINE_MODE_INVALID"):
        await service.switch(
            admin_context(), mode="published",
            expected_writer_revision=1, reason="test",
        )
    with pytest.raises(MemoryError, match="SCOPE_DENIED"):
        await service.switch(
            base_context(), mode="service", expected_writer_revision=1, reason="test",
        )


async def test_switch_reports_pending_backlog_without_blocking() -> None:
    scope = await ensure_scope()
    await DBMemWriteJob.create(
        scope_id=scope.id, observation_id=scope.id, pipeline_version="rollout-v1",
        writer_revision=scope.writer_revision, mode="shadow", state="pending",
    )
    service = RolloutService()
    report = await service.report(admin_context())
    # 待领取不是阻断项：它们在切换后按新版本重新入队。
    assert report.pending_jobs == 1
    assert report.ready is True
    assert "pending_jobs" not in report.blockers

    after = await service.switch(
        admin_context(), mode="service",
        expected_writer_revision=scope.writer_revision, reason="test",
    )
    assert after.mode == "service"


async def test_mode_change_makes_legacy_writer_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    # 生产里路由与测试共用同一个 agent_id；测试上下文用 test-agent，这里对齐一下。
    monkeypatch.setattr(config, "MEMORY_FACT_AGENT_ID", "test-agent")
    scope = await ensure_scope()
    await RolloutService().switch(
        admin_context(), mode="service",
        expected_writer_revision=scope.writer_revision, reason="test",
    )
    # 切到 service 后旧写入口必须被拒绝，而不是继续静默写旧表。
    from nekro_agent.services.memory.routing import legacy_fact_write

    with pytest.raises(MemoryError, match="LEGACY_WRITER_DISABLED"):
        async with legacy_fact_write("private_alice"):
            pass

    # 切回 shadow 后旧入口重新可用（回滚路径）。
    current = await DBMemScope.get(id=scope.id)
    await RolloutService().switch(
        admin_context(), mode="shadow",
        expected_writer_revision=current.writer_revision, reason="rollback",
    )
    async with legacy_fact_write("private_alice"):
        pass
