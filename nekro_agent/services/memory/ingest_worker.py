"""显式启用的持久抽取任务。消息写入 hook 和周期补扫在灰度接入阶段安装。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from tortoise.expressions import Q
from tortoise.transactions import in_transaction

from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_fact import (
    DBMemObservation,
    DBMemScope,
    DBMemWriteJob,
)
from nekro_agent.services.memory.contracts import FactProposal, MemoryError, RequestContext, SourceRef
from nekro_agent.services.memory.service import MemoryService, message_revision


class ExtractionInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: SourceRef
    speaker_key: str
    chat_key: str
    text: str


class ExtractedFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    proposal: FactProposal
    evidence_quote: str = Field(min_length=1)


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: list[ExtractedFact] = Field(default_factory=list, max_length=6)


class JobLease(BaseModel):
    id: UUID
    token: UUID
    scope_id: UUID
    writer_revision: int
    attempts: int


class MemoryIngestQueue:
    def __init__(self, service: MemoryService | None = None, *, max_attempts: int = 5):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.service = service or MemoryService()
        self.max_attempts = max_attempts

    async def set_mode(self, context: RequestContext, mode: str, *, expected_writer_revision: int) -> int:
        """灰度协调器必须先停止旧领取、追平来源；本方法只转移新管线所有权。"""
        if not context.is_admin or context.purpose != "admin_review":
            raise MemoryError("SCOPE_DENIED")
        if mode not in {"legacy_guarded", "shadow", "service"}:
            raise MemoryError("PIPELINE_MODE_INVALID")
        scope = await self.service.resolve_scope(context)
        async with in_transaction() as conn:
            channel = await DBChatChannel.filter(
                chat_key=context.chat_key, workspace_id=context.workspace_id,
            ).using_db(conn).select_for_update().first()
            if channel is None:
                raise MemoryError("SCOPE_UNRESOLVED")
            scope = await DBMemScope.filter(id=scope.id).using_db(conn).select_for_update().get()
            if scope.writer_revision != expected_writer_revision:
                raise MemoryError("WRITER_FENCED")
            if scope.pipeline_mode == mode:
                return scope.writer_revision
            await DBMemScope.filter(id=scope.id).using_db(conn).update(
                pipeline_mode=mode, writer_revision=scope.writer_revision + 1, revision=scope.revision + 1,
            )
            await self.service._control_event(scope, context, scope.id, "pipeline.changed", conn)
            return scope.writer_revision + 1

    async def enqueue(
        self, context: RequestContext, source: SourceRef, *, pipeline_version: str,
    ) -> UUID:
        if context.purpose != "extract" or source.kind != "message":
            raise MemoryError("SCOPE_DENIED")
        if not pipeline_version or len(pipeline_version) > 64:
            raise MemoryError("PIPELINE_VERSION_INVALID")
        scope = await self.service.resolve_scope(context)
        async with in_transaction() as conn:
            scope = await DBMemScope.filter(id=scope.id).using_db(conn).select_for_update().get()
            if scope.pipeline_mode not in {"shadow", "service"}:
                raise MemoryError("PIPELINE_DISABLED")
            observation = await self.service._observation(context, scope, source, conn)
            job, _ = await DBMemWriteJob.get_or_create(
                scope_id=scope.id, observation_id=observation.id, pipeline_version=pipeline_version,
                writer_revision=scope.writer_revision, defaults={"mode": scope.pipeline_mode}, using_db=conn,
            )
            return job.id

    async def claim(self, *, scope_id: UUID, lease_seconds: int = 60) -> JobLease | None:
        if lease_seconds < 1 or lease_seconds > 3600:
            raise MemoryError("LEASE_INVALID")
        now = datetime.now(timezone.utc)
        due = Q(state="pending") | Q(state="running", lease_until__lte=now)
        async with in_transaction() as conn:
            # 同一频道只允许一个抽取任务在途，避免较早来源晚提交覆盖较新事实。
            scope = await DBMemScope.filter(id=scope_id).using_db(conn).select_for_update().first()
            if scope is None:
                raise MemoryError("SCOPE_UNRESOLVED")
            if await DBMemWriteJob.filter(
                scope_id=scope_id, state="running", lease_until__gt=now,
            ).using_db(conn).exists():
                return None
            await DBMemWriteJob.filter(
                due, scope_id=scope_id, attempts__gte=self.max_attempts,
            ).using_db(conn).update(state="dead_letter", error_class="MAX_ATTEMPTS", lease_token=None, lease_until=None)
            job = await DBMemWriteJob.filter(
                due, Q(next_retry_at=None) | Q(next_retry_at__lte=now), scope_id=scope_id,
                attempts__lt=self.max_attempts,
            ).using_db(conn).select_for_update(skip_locked=True).order_by("created_at").first()
            if job is None:
                return None
            token = uuid4()
            await DBMemWriteJob.filter(id=job.id).using_db(conn).update(
                state="running", lease_token=token, lease_until=now + timedelta(seconds=lease_seconds),
                attempts=job.attempts + 1, next_retry_at=None,
            )
            return JobLease(id=job.id, token=token, scope_id=job.scope_id, writer_revision=job.writer_revision, attempts=job.attempts + 1)

    async def load_source(self, lease: JobLease) -> ExtractionInput:
        job = await DBMemWriteJob.filter(
            id=lease.id, scope_id=lease.scope_id, state="running", lease_token=lease.token,
            lease_until__gt=datetime.now(timezone.utc),
        ).first()
        if job is None:
            raise MemoryError("LEASE_LOST")
        scope = await DBMemScope.get(id=job.scope_id)
        if scope.writer_revision != lease.writer_revision or scope.pipeline_mode != job.mode:
            raise MemoryError("WRITER_FENCED")
        observation = await DBMemObservation.get(id=job.observation_id)
        if observation.status != "valid" or observation.source_kind != "message" or not observation.source_id.isdecimal():
            raise MemoryError("SOURCE_INVALID")
        message = await DBChatMessage.filter(
            id=int(observation.source_id), chat_key=scope.origin_chat_key, is_recalled=False,
        ).first()
        if message is None or str(message.sender_id) == "-1" or message_revision(message) != observation.source_revision:
            raise MemoryError("SOURCE_INVALID")
        context = RequestContext(
            workspace_id=scope.workspace_id, agent_id=scope.agent_id, actor_key="service:memory",
            chat_key=scope.origin_chat_key, purpose="extract",
        )
        await self.service.resolve_scope(context)
        return ExtractionInput(
            source=SourceRef(kind="message", id=observation.source_id, revision=observation.source_revision),
            speaker_key=observation.speaker_key, chat_key=scope.origin_chat_key, text=message.content_text,
        )

    async def renew(self, lease: JobLease, *, lease_seconds: int = 60) -> None:
        if lease_seconds < 1 or lease_seconds > 3600:
            raise MemoryError("LEASE_INVALID")
        now = datetime.now(timezone.utc)
        updated = await DBMemWriteJob.filter(
            id=lease.id, state="running", lease_token=lease.token, lease_until__gt=now,
        ).update(lease_until=now + timedelta(seconds=lease_seconds))
        if not updated:
            raise MemoryError("LEASE_LOST")

    async def complete(self, lease: JobLease, result: ExtractionResult) -> None:
        async with in_transaction() as conn:
            # 所有写路径按 scope -> job 加锁，避免更正与模式切换死锁。
            scope = await DBMemScope.filter(id=lease.scope_id).using_db(conn).select_for_update().get()
            job = await DBMemWriteJob.filter(
                id=lease.id, scope_id=scope.id, state="running", lease_token=lease.token,
                lease_until__gt=datetime.now(timezone.utc),
            ).using_db(conn).select_for_update().first()
            if job is None:
                raise MemoryError("LEASE_LOST")
            if scope.writer_revision != lease.writer_revision or scope.pipeline_mode != job.mode:
                raise MemoryError("WRITER_FENCED")
            source = await self.load_source(lease)
            for item in result.facts:
                if item.proposal.source != source.source or item.evidence_quote not in source.text:
                    raise MemoryError("SOURCE_INVALID")
            if job.mode == "shadow":
                updated = await DBMemWriteJob.filter(
                    id=job.id, lease_until__gt=datetime.now(timezone.utc),
                ).using_db(conn).update(
                    state="shadow_done", shadow_result=result.model_dump(mode="json"), lease_token=None, lease_until=None,
                )
                if not updated:
                    raise MemoryError("LEASE_LOST")
                return
            context = RequestContext(
                workspace_id=scope.workspace_id, agent_id=scope.agent_id, actor_key="service:memory",
                chat_key=scope.origin_chat_key, purpose="extract",
            )
            for i, item in enumerate(result.facts):
                await self.service.propose_fact(
                    context, item.proposal, idempotency_key=f"job:{job.id}:candidate:{i}",
                    expected_writer_revision=lease.writer_revision,
                )
            updated = await DBMemWriteJob.filter(
                id=job.id, lease_until__gt=datetime.now(timezone.utc),
            ).using_db(conn).update(
                state="done", lease_token=None, lease_until=None, error_class=None,
            )
            if not updated:
                raise MemoryError("LEASE_LOST")

    async def fail(self, lease: JobLease, *, error_class: str, permanent: bool = False) -> None:
        # 错误分类是固定代码，禁止持久化含密钥或消息原文的异常字符串。
        if not error_class or len(error_class) > 64 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_" for c in error_class):
            raise MemoryError("ERROR_CLASS_INVALID")
        state = "cancelled" if permanent else "dead_letter" if lease.attempts >= self.max_attempts else "pending"
        now = datetime.now(timezone.utc)
        updated = await DBMemWriteJob.filter(
            id=lease.id, state="running", lease_token=lease.token, lease_until__gt=now,
        ).update(
            state=state, lease_token=None, lease_until=None, error_class=error_class,
            next_retry_at=None if state != "pending" else now + timedelta(seconds=min(300, 2 ** lease.attempts)),
        )
        if not updated:
            raise MemoryError("LEASE_LOST")

    async def process_one(
        self, *, scope_id: UUID, extractor: Callable[[ExtractionInput], Awaitable[ExtractionResult]],
    ) -> bool:
        lease = await self.claim(scope_id=scope_id)
        if lease is None:
            return False
        try:
            source = await self.load_source(lease)
            result = await asyncio.wait_for(extractor(source), timeout=45)
            await self.complete(lease, result)
        except MemoryError as error:
            if error.code == "LEASE_LOST":
                return True
            try:
                await self.fail(
                    lease, error_class=error.code,
                    permanent=error.code in {"SOURCE_INVALID", "WRITER_FENCED", "SCOPE_UNRESOLVED"},
                )
            except MemoryError as failure:
                if failure.code != "LEASE_LOST":
                    raise
            return True
        except Exception:
            # 消费状态必须可恢复；异常原文仍由调用方按既有日志策略处理。
            try:
                await self.fail(lease, error_class="EXTRACTOR_FAILED")
            except MemoryError as failure:
                if failure.code != "LEASE_LOST":
                    raise
            raise
        return True
