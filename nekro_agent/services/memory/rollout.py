"""按频道灰度：切换前预检、在途隔离与所有权交接。

设计依据：`outputs/nekro-memory-detailed-design-v1.md` 的 §17.6（按频道灰度与单一事实所有权）、
§17.7（回滚步骤）。要点：

- 切换必须一次性转移事实读写所有权，不能靠多个独立开关拼出来；
- 切换前要暂停领取旧任务、处理在途提交或使旧租约失效、记录水位，再更新 routing_revision 与 fencing token；
- 回滚不能只切配置，必须追平兼容投影并重新交接所有权。
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from nekro_agent.models.db_mem_fact import (
    DBMemAssertion,
    DBMemAuditEvent,
    DBMemIndexOutbox,
    DBMemWriteJob,
)
from nekro_agent.services.memory.contracts import FactStatus, MemoryError, RequestContext
from nekro_agent.services.memory.ingest_worker import MemoryIngestQueue
from nekro_agent.services.memory.service import MemoryService

MODES = ("legacy_guarded", "shadow", "service")


class RolloutCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    ok: bool
    blocker: bool
    detail: str = ""


class RolloutReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_key: str
    workspace_id: int
    mode: str
    writer_revision: int
    pending_jobs: int
    running_jobs: int
    index_pending: int
    index_dead_letter: int
    active_assertions: int
    ready: bool
    blockers: list[str] = Field(default_factory=list)
    checks: list[RolloutCheck] = Field(default_factory=list)


class RolloutService:
    """灰度协调器：只做预检与所有权交接，事实读写仍由 MemoryService 负责。"""

    def __init__(self, service: MemoryService | None = None) -> None:
        self.service = service or MemoryService()
        self.queue = MemoryIngestQueue(self.service)

    async def report(self, context: RequestContext) -> RolloutReport:
        if not context.is_admin or context.purpose != "admin_review":
            raise MemoryError("SCOPE_DENIED")
        scope = await self.service.resolve_scope(context)
        now = datetime.now(timezone.utc)
        pending = await DBMemWriteJob.filter(scope_id=scope.id, state="pending").count()
        running = await DBMemWriteJob.filter(
            scope_id=scope.id, state="running", lease_until__gt=now,
        ).count()
        assertion_ids = list(
            await DBMemAssertion.filter(scope_id=scope.id).values_list("id", flat=True),
        )
        index_pending = await DBMemIndexOutbox.filter(
            assertion_id__in=assertion_ids, state__in=["pending", "running"],
        ).count()
        index_dead = await DBMemIndexOutbox.filter(
            assertion_id__in=assertion_ids, state="dead_letter",
        ).count()
        active = await DBMemAssertion.filter(scope_id=scope.id, status=FactStatus.ACTIVE).count()

        checks: list[RolloutCheck] = []
        # 在途提交是硬阻断：此时切换所有权会让新旧两侧同时写入同一事实。
        checks.append(
            RolloutCheck(
                code="in_flight_jobs",
                ok=running == 0,
                blocker=True,
                detail=(
                    "没有在途抽取任务"
                    if running == 0
                    else f"仍有 {running} 条抽取任务持有租约；需等待提交或用「隔离旧租约」把它们退回待领取"
                ),
            ),
        )
        # 待领取任务不是阻断项：它们会在切换后按新模式的 writer_revision 重新入队。
        checks.append(
            RolloutCheck(
                code="pending_jobs",
                ok=pending == 0,
                blocker=False,
                detail=f"尚有 {pending} 条待领取任务，切换后需确认能继续处理" if pending else "没有待领取任务",
            ),
        )
        checks.append(
            RolloutCheck(
                code="index_backlog",
                ok=index_pending == 0,
                blocker=False,
                detail=f"索引仍有 {index_pending} 条待处理" if index_pending else "索引已追平",
            ),
        )
        checks.append(
            RolloutCheck(
                code="index_dead_letter",
                ok=index_dead == 0,
                blocker=False,
                detail=f"索引有 {index_dead} 条死信需处置" if index_dead else "索引没有死信",
            ),
        )
        checks.append(
            RolloutCheck(
                code="content_present",
                ok=active > 0,
                blocker=False,
                detail=f"当前有 {active} 条生效事实" if active else "该频道还没有生效事实，切换前建议先确认抽取是否正常",
            ),
        )

        blockers = [check.code for check in checks if check.blocker and not check.ok]
        return RolloutReport(
            chat_key=context.chat_key,
            workspace_id=scope.workspace_id,
            mode=scope.pipeline_mode,
            writer_revision=scope.writer_revision,
            pending_jobs=pending,
            running_jobs=running,
            index_pending=index_pending,
            index_dead_letter=index_dead,
            active_assertions=active,
            ready=not blockers,
            blockers=blockers,
            checks=checks,
        )

    async def switch(
        self,
        context: RequestContext,
        *,
        mode: str,
        expected_writer_revision: int,
        isolate_leases: bool = False,
        reason: str = "",
    ) -> RolloutReport:
        """切换频道的事实所有权；`expected_writer_revision` 不符时拒绝，避免并发切换互相覆盖。"""
        if not context.is_admin or context.purpose != "admin_review":
            raise MemoryError("SCOPE_DENIED")
        if mode not in MODES:
            raise MemoryError("PIPELINE_MODE_INVALID")
        before = await self.report(context)
        if before.mode == mode:
            return before
        if before.running_jobs and not isolate_leases:
            raise MemoryError("ROLLOUT_BUSY")
        if isolate_leases and before.running_jobs:
            # 回滚步骤 4 的「隔离旧租约」：把租约退回待领取，旧所有者下次提交会被 writer_revision 拒绝。
            await DBMemWriteJob.filter(
                scope_id=await self._scope_id(context),
                state="running",
                lease_token__not_isnull=True,
            ).update(
                state="pending", lease_token=None, lease_until=None, error_class="LEASE_ISOLATED",
            )
            await self._audit(context, "pipeline.leases_isolated", reason)
        revision = await self.queue.set_mode(
            context, mode, expected_writer_revision=expected_writer_revision,
        )
        await self._audit(context, f"pipeline.{mode}", reason)
        after = await self.report(context)
        # 让调用方拿到真实的切换后状态，而不是只回一个版本号。
        assert after.writer_revision == revision
        return after

    async def _scope_id(self, context: RequestContext):
        scope = await self.service.resolve_scope(context)
        return scope.id

    async def _audit(self, context: RequestContext, operation: str, reason: str) -> None:
        scope = await self.service.resolve_scope(context)
        await DBMemAuditEvent.create(
            scope_id=scope.id,
            actor_key=context.actor_key[:256],
            operation=operation[:40],
            target_id=scope.id,
            reason_code=(reason or "")[:64],
        )
