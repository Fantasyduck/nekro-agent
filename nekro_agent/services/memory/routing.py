"""服务端按频道路由；停用后台工作器不能重新开放旧事实写入。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from tortoise.transactions import in_transaction

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_mem_fact import DBMemScope
from nekro_agent.services.memory.contracts import MemoryError, RequestContext


@dataclass(frozen=True)
class MemoryRoute:
    scope_id: UUID
    workspace_id: int
    chat_key: str
    agent_id: str
    mode: str
    writer_revision: int

    @property
    def uses_service(self) -> bool:
        return self.mode == "service"

    def context(self, *, actor_key: str = "service:memory", purpose: str = "extract") -> RequestContext:
        return RequestContext(
            workspace_id=self.workspace_id, chat_key=self.chat_key, agent_id=self.agent_id,
            actor_key=actor_key, purpose=purpose,
        )


async def get_memory_route(chat_key: str) -> MemoryRoute | None:
    channel = await DBChatChannel.filter(chat_key=chat_key).first()
    if channel is None or channel.workspace_id is None:
        return None
    scope = await DBMemScope.filter(
        workspace_id=channel.workspace_id, origin_chat_key=chat_key, agent_id=config.MEMORY_FACT_AGENT_ID,
    ).first()
    if scope is None:
        return None
    if scope.pipeline_mode not in {"legacy_guarded", "shadow", "service"}:
        raise MemoryError("PIPELINE_MODE_INVALID")
    return MemoryRoute(
        scope_id=scope.id, workspace_id=scope.workspace_id, chat_key=chat_key, agent_id=scope.agent_id,
        mode=scope.pipeline_mode, writer_revision=scope.writer_revision,
    )


@asynccontextmanager
async def legacy_fact_write(chat_key: str) -> AsyncIterator[None]:
    """与 set_mode 共用频道行锁，保护实际提交点，不能只在抽取前检查一次。"""
    async with in_transaction() as conn:
        channel = await DBChatChannel.filter(chat_key=chat_key).using_db(conn).select_for_update().first()
        if channel is not None and channel.workspace_id is not None:
            scope = await DBMemScope.filter(
                workspace_id=channel.workspace_id, origin_chat_key=chat_key, agent_id=config.MEMORY_FACT_AGENT_ID,
            ).using_db(conn).select_for_update().first()
            if scope is not None and scope.pipeline_mode not in {"legacy_guarded", "shadow"}:
                raise MemoryError("LEGACY_WRITER_DISABLED")
        yield
