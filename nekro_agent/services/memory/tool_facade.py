"""工具与管理入口的统一事实门面。

Agent 工具（记住事实 / 查看事实 / 删除事实）与天使插件的记忆工具都从这里提交，
不再各自直写 SQLite 事实表。工具本身没有独立证据，因此写入绑定当前会话最近一条
真实消息作为观察来源；主体与说话人不一致时由 MemoryService 自动进入待确认，
不会把待确认伪装成确定成功。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_fact import DBMemLegacyMap
from nekro_agent.services.memory.contracts import (
    FactPage,
    FactProposal,
    ForgetReceipt,
    MemoryError,
    RequestContext,
    ResolutionResult,
    SourceRef,
)
from nekro_agent.services.memory.routing import MemoryRoute, get_memory_route
from nekro_agent.services.memory.service import MemoryService, message_revision

# 自由文本记忆在统一事实里使用多值谓词：同一主体的多条笔记互不覆盖。
NOTE_PREDICATE = "memory.note"
LEGACY_STORE = "nekro_memory_angel"


def digest(value: Any) -> str:
    """与事实服务同构的稳定摘要，用于幂等键与匿名来源 ID。"""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def service() -> MemoryService:
    """返回工具入口使用的事实服务实例（笔记谓词按多值处理）。"""
    return MemoryService(multivalued_predicates=frozenset({NOTE_PREDICATE}))


def normalize_subject(adapter_key: str, user_id: str) -> str:
    """统一主体标识：平台用户写成 adapter:platform_userid，与服务端 speaker 同构。"""
    cleaned = str(user_id or "").strip()
    if not cleaned:
        return f"{adapter_key}:unknown"
    return cleaned if ":" in cleaned else f"{adapter_key}:{cleaned}"


async def require_service_route(chat_key: str) -> MemoryRoute:
    """取得服务模式路由；未启用统一管线时明确拒绝，避免写入无人读取的库。"""
    route = await get_memory_route(chat_key)
    if route is None or not route.uses_service:
        raise MemoryError("PIPELINE_DISABLED")
    return route


async def latest_message_source(chat_key: str) -> SourceRef:
    """取当前会话最近一条真实消息作为工具写入的观察来源。"""
    message = (
        await DBChatMessage.filter(chat_key=chat_key, is_recalled=False)
        .exclude(sender_id="-1")
        .order_by("-id")
        .first()
    )
    if message is None:
        raise MemoryError("SOURCE_INVALID")
    return SourceRef(kind="message", id=str(message.id), revision=message_revision(message))


def request_context(
    route: MemoryRoute, *, actor_key: str, purpose: Literal["answer", "extract", "admin_review"] = "extract",
) -> RequestContext:
    """按路由组装请求上下文，scope 与工作区全部由服务端生成。"""
    return RequestContext(
        workspace_id=route.workspace_id,
        agent_id=route.agent_id,
        actor_key=actor_key or "service:memory",
        chat_key=route.chat_key,
        purpose=purpose,
    )


async def remember_fact(
    *,
    chat_key: str,
    actor_key: str,
    subject_key: str,
    predicate: str,
    value: Any,
    confidence: float = 0.85,
    min_favor: int = 0,
    valid_from: datetime | None = None,
) -> ResolutionResult:
    """提交一条结构化事实（工具入口）。"""
    route = await require_service_route(chat_key)
    source = await latest_message_source(chat_key)
    proposal = FactProposal(
        subject_key=subject_key,
        predicate=predicate,
        value=value,
        confidence=confidence,
        min_favor=min_favor,
        source=source,
        valid_from=valid_from,
    )
    idempotency_key = f"tool:{source.id}:{digest([subject_key, predicate, value])[:32]}"
    return await service().propose_fact(
        request_context(route, actor_key=actor_key), proposal, idempotency_key=idempotency_key,
    )


async def remember_note(
    *,
    chat_key: str,
    actor_key: str,
    subject_key: str,
    text: str,
    tags: list[str] | None = None,
    importance: float = 0.7,
    memory_type: str = "semantic",
    scope: str = "channel",
    min_favor: int = 0,
    confidence: float = 0.9,
) -> ResolutionResult:
    """提交一条自由文本长期记忆（笔记），保留标签与重要度等元数据。"""
    route = await require_service_route(chat_key)
    source = await latest_message_source(chat_key)
    payload = {
        "text": text,
        "tags": list(tags or []),
        "importance": float(importance),
        "memory_type": memory_type,
        "scope": scope,
    }
    proposal = FactProposal(
        subject_key=subject_key,
        predicate=NOTE_PREDICATE,
        value=payload,
        confidence=confidence,
        min_favor=min_favor,
        source=source,
    )
    idempotency_key = f"note:{source.id}:{digest([subject_key, text])[:32]}"
    return await service().propose_fact(
        request_context(route, actor_key=actor_key), proposal, idempotency_key=idempotency_key,
    )


async def list_facts(
    *,
    chat_key: str,
    actor_key: str,
    subject_key: str | None = None,
    predicate: str | None = None,
    query: str = "",
    limit: int = 20,
    expression_ceiling: int = 0,
) -> FactPage:
    """读取当前会话可见的长期事实；有关键词时按检索排序返回。"""
    route = await require_service_route(chat_key)
    context = request_context(route, actor_key=actor_key, purpose="answer")
    size = max(1, min(100, limit))
    service_instance = service()
    text = query.strip()
    if not text:
        return await service_instance.list_facts(context, subject_key=subject_key, predicate=predicate, limit=size)
    bundle = await service_instance.recall(
        context, subject_key=subject_key, predicate=predicate, query=text,
        limit=size, expression_ceiling=expression_ceiling,
    )
    items = [(await service_instance.get_fact(context, item.assertion_id)).current for item in bundle.items]
    return FactPage(items=items, total=len(items), policy_revisions=bundle.policy_revisions)


async def forget_fact(*, chat_key: str, actor_key: str, assertion_id: UUID) -> ForgetReceipt:
    """按断言 ID 遗忘一条事实（普通用户只能遗忘主体是自己的事实）。"""
    route = await require_service_route(chat_key)
    return await service().forget(
        request_context(route, actor_key=actor_key, purpose="answer"), assertion_id,
    )


async def admin_remember_fact(
    *,
    chat_key: str,
    actor_key: str,
    subject_key: str,
    predicate: str,
    value: Any,
    confidence: float = 0.95,
    min_favor: int = 0,
    reason: str = "admin_edit",
) -> ResolutionResult:
    """管理员手工写入：来源记为类型化 admin_edit 证据并审计操作者。"""
    route = await require_service_route(chat_key)
    # is_admin 只能由服务端注入；这条路径只给已完成管理员鉴权的调用方使用。
    context = request_context(route, actor_key=actor_key, purpose="admin_review").model_copy(
        update={"is_admin": True},
    )
    source = SourceRef(
        kind="admin_edit",
        id=f"admin:{digest([actor_key, chat_key, subject_key, predicate])[:32]}",
        revision=digest([value, reason])[:32],
    )
    proposal = FactProposal(
        subject_key=subject_key,
        predicate=predicate,
        value=value,
        confidence=confidence,
        min_favor=min_favor,
        source=source,
    )
    return await service().propose_fact(
        context, proposal, idempotency_key=f"admin:{source.id}:{digest(value)[:16]}",
    )


async def admin_correct_fact(
    *,
    chat_key: str,
    actor_key: str,
    assertion_id: UUID,
    value: Any,
    confidence: float | None = None,
    min_favor: int | None = None,
    expected_row_revision: int | None = None,
    reason: str = "admin_edit",
) -> ResolutionResult:
    """管理员纠正一条事实：生成新版本并保留旧版本，不覆盖历史。"""
    route = await require_service_route(chat_key)
    context = request_context(route, actor_key=actor_key, purpose="admin_review").model_copy(
        update={"is_admin": True},
    )
    return await service().correct_fact(
        context,
        assertion_id,
        value=value,
        confidence=confidence,
        min_favor=min_favor,
        expected_row_revision=expected_row_revision,
        reason=reason,
    )


async def record_legacy_mapping(
    *, legacy_id: str, assertion_id: UUID, legacy_table: str = "facts", checksum: str = "",
) -> None:
    """登记旧 short_id → 新断言 ID 的跳转映射，便于旧面板按旧编号查到权威记录。"""
    await DBMemLegacyMap.get_or_create(
        legacy_store=LEGACY_STORE,
        legacy_table=legacy_table,
        legacy_id=str(legacy_id),
        defaults={
            "assertion_id": assertion_id,
            "checksum": checksum or digest([legacy_table, str(legacy_id)])[:32],
            "import_status": "linked",
        },
    )


async def resolve_legacy_mapping(*, legacy_id: str, legacy_table: str = "facts") -> UUID | None:
    """按旧 short_id 反查新断言 ID；未登记返回 None。"""
    row = await DBMemLegacyMap.filter(
        legacy_store=LEGACY_STORE, legacy_table=legacy_table, legacy_id=str(legacy_id),
    ).first()
    return row.assertion_id if row is not None else None
