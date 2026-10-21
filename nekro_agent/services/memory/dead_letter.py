"""死信（dead letter）运维：查询、重试与确认忽略。

设计依据：任务处于 dead_letter 时必须由管理动作重试或确认忽略，不能默默推进为成功
（detailed-design :346）。这里只暴露分类与编号，不返回消息正文或事实取值。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from tortoise.expressions import Q
from tortoise.transactions import in_transaction

from nekro_agent.models.db_mem_fact import (
    DBMemAssertion,
    DBMemAuditEvent,
    DBMemIndexOutbox,
    DBMemScope,
    DBMemWriteJob,
)
from nekro_agent.services.memory.contracts import MemoryError, RequestContext

DeadLetterKind = Literal["index", "ingest"]
KINDS: tuple[str, str] = ("index", "ingest")


class DeadLetterItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    kind: str
    state: str
    attempts: int
    created_at: datetime
    next_retry_at: datetime | None = None
    assertion_id: UUID | None = None
    operation: str = ""
    error_class: str = ""
    scope_id: UUID | None = None


class DeadLetterPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[DeadLetterItem] = Field(default_factory=list)
    total: int = 0


class DeadLetterAdmin:
    """两个队列（索引投影、抽取任务）共用的死信管理入口。"""

    def __init__(self, *, context: RequestContext) -> None:
        if not context.is_admin or context.purpose != "admin_review":
            raise MemoryError("SCOPE_DENIED")
        self.context = context

    @staticmethod
    def _check_kind(kind: str) -> str:
        if kind not in KINDS:
            raise MemoryError("KIND_INVALID")
        return kind

    async def _scope_ids(self) -> list[UUID]:
        scope = await DBMemScope.filter(
            workspace_id=self.context.workspace_id, agent_id=self.context.agent_id,
            origin_chat_key=self.context.chat_key,
        ).first()
        if scope is None:
            raise MemoryError("SCOPE_UNRESOLVED")
        return [scope.id]

    async def list(
        self, *, kind: str, state: str = "dead_letter", limit: int = 50, offset: int = 0,
    ) -> DeadLetterPage:
        if not 1 <= limit <= 200 or offset < 0:
            raise MemoryError("LIMIT_INVALID")
        self._check_kind(kind)
        if state not in {"dead_letter", "pending", "running", "cancelled", "done", "shadow_done", "all"}:
            raise MemoryError("LIMIT_INVALID")
        scope_ids = await self._scope_ids()
        items: list[DeadLetterItem] = []
        if kind == "index":
            assertion_ids = list(await DBMemAssertion.filter(scope_id__in=scope_ids).values_list("id", flat=True))
            query = DBMemIndexOutbox.filter(assertion_id__in=assertion_ids)
            if state != "all":
                query = query.filter(state=state)
            total = await query.count()
            async for row in query.order_by("created_at").offset(offset).limit(limit):
                items.append(DeadLetterItem(
                    id=row.id, kind=kind, state=row.state, attempts=row.attempts, created_at=row.created_at,
                    next_retry_at=row.next_retry_at, assertion_id=row.assertion_id, operation=row.operation,
                ))
            return DeadLetterPage(items=items, total=total)
        query = DBMemWriteJob.filter(scope_id__in=scope_ids)
        if state != "all":
            query = query.filter(state=state)
        total = await query.count()
        async for row in query.order_by("created_at").offset(offset).limit(limit):
            # 不带 shadow_result：影子结果里可能有消息正文。
            items.append(DeadLetterItem(
                id=row.id, kind=kind, state=row.state, attempts=row.attempts, created_at=row.created_at,
                next_retry_at=row.next_retry_at, error_class=row.error_class or "", scope_id=row.scope_id,
            ))
        return DeadLetterPage(items=items, total=total)

    async def retry(self, *, kind: str, item_id: UUID) -> bool:
        """把死信放回待办队列；attempts 归零，否则下一轮领取就会再次判为穷尽。"""
        self._check_kind(kind)
        scope_ids = await self._scope_ids()
        if kind == "index":
            assertion_ids = list(await DBMemAssertion.filter(scope_id__in=scope_ids).values_list("id", flat=True))
            async with in_transaction() as conn:
                row = await DBMemIndexOutbox.filter(
                    id=item_id, assertion_id__in=assertion_ids, state="dead_letter",
                ).using_db(conn).select_for_update().first()
                if row is None:
                    return False
                await DBMemIndexOutbox.filter(id=row.id).using_db(conn).update(
                    state="pending", attempts=0, next_retry_at=None, lease_token=None, lease_until=None,
                )
                await self._audit(row.id, "index.retried", scope_ids[0], conn)
            return True
        async with in_transaction() as conn:
            row = await DBMemWriteJob.filter(
                id=item_id, scope_id__in=scope_ids, state="dead_letter",
            ).using_db(conn).select_for_update().first()
            if row is None:
                return False
            await DBMemWriteJob.filter(id=row.id).using_db(conn).update(
                state="pending", attempts=0, next_retry_at=None, lease_token=None, lease_until=None, error_class=None,
            )
            await self._audit(row.id, "ingest.retried", row.scope_id, conn)
        return True

    async def ignore(self, *, kind: str, item_id: UUID) -> bool:
        """确认忽略：置 cancelled，保留记录与原因供对账。"""
        self._check_kind(kind)
        scope_ids = await self._scope_ids()
        if kind == "index":
            assertion_ids = list(await DBMemAssertion.filter(scope_id__in=scope_ids).values_list("id", flat=True))
            async with in_transaction() as conn:
                row = await DBMemIndexOutbox.filter(
                    Q(state="dead_letter") | Q(state="pending"), id=item_id, assertion_id__in=assertion_ids,
                ).using_db(conn).select_for_update().first()
                if row is None:
                    return False
                await DBMemIndexOutbox.filter(id=row.id).using_db(conn).update(
                    state="cancelled", lease_token=None, lease_until=None, next_retry_at=None,
                )
                await self._audit(row.id, "index.ignored", scope_ids[0], conn)
            return True
        async with in_transaction() as conn:
            row = await DBMemWriteJob.filter(
                Q(state="dead_letter") | Q(state="pending"), id=item_id, scope_id__in=scope_ids,
            ).using_db(conn).select_for_update().first()
            if row is None:
                return False
            await DBMemWriteJob.filter(id=row.id).using_db(conn).update(
                state="cancelled", lease_token=None, lease_until=None, next_retry_at=None,
            )
            await self._audit(row.id, "ingest.ignored", row.scope_id, conn)
        return True

    async def _audit(self, target_id: UUID, operation: str, scope_id: UUID, conn) -> None:
        await DBMemAuditEvent.create(
            scope_id=scope_id, actor_key=self.context.actor_key, operation=operation,
            target_id=target_id, reason_code="admin_dead_letter", using_db=conn,
        )
