"""发送模型请求前的记忆证据复核与降级。

历史模板在装配 prompt 时把记忆块原文与它对应的 EvidenceBundle 登记到这里；
真正发送请求前再取回复核一次。复核失败只移除记忆块后继续发送，不整轮失败。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.agent.memory_context import ContextBundle, MemoryContextBuilder
from nekro_agent.services.agent.trace import emit as trace_emit
from nekro_agent.services.memory.contracts import RequestContext
from nekro_agent.services.memory.service import MemoryService

logger = get_sub_logger("agent_runtime")

STATUS_SKIPPED = "skipped"
STATUS_VERIFIED = "verified"
STATUS_DEGRADED = "degraded"


@dataclass(frozen=True)
class PendingMemoryContext:
    """已装配进 prompt、尚未发送的一次记忆块。"""

    text: str
    bundle: ContextBundle
    context: RequestContext


_PENDING: dict[str, PendingMemoryContext] = {}


def register_pending(*, chat_key: str, text: str, bundle: ContextBundle, context: RequestContext) -> None:
    """登记本次装配的记忆块，供发送前复核使用。"""
    _PENDING[chat_key] = PendingMemoryContext(text=text, bundle=bundle, context=context)


def clear_pending(chat_key: str) -> None:
    """清除登记；未注入记忆块时调用，避免复用到上一轮的旧证据。"""
    _PENDING.pop(chat_key, None)


def take_pending(chat_key: str) -> PendingMemoryContext | None:
    """取出并移除登记，保证每次复核只消费一次。"""
    return _PENDING.pop(chat_key, None)


def remove_memory_block(messages: List[Any], block_text: str) -> bool:
    """从已装配的消息中按原文移除记忆块，返回是否真的移除了内容。"""
    if not block_text:
        return False
    removed = False
    for message in messages:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        changed = False
        kept: list[Any] = []
        for segment in content:
            if isinstance(segment, dict) and isinstance(segment.get("text"), str) and block_text in segment["text"]:
                remainder = segment["text"].replace(block_text, "")
                changed = True
                if remainder:
                    kept.append({**segment, "text": remainder})
                continue
            kept.append(segment)
        if changed:
            removed = True
            message.content = kept
    return removed


async def revalidate_before_send(messages: List[Any], chat_key: str) -> str:
    """发送前复核登记的记忆证据；失败时移除记忆块并返回降级状态。

    Returns:
        skipped / verified / degraded
    """
    if not chat_key:
        return STATUS_SKIPPED
    pending = take_pending(chat_key)
    if pending is None:
        return STATUS_SKIPPED

    try:
        await MemoryContextBuilder(MemoryService()).validate(pending.context, pending.bundle)
    except Exception as exc:  # noqa: BLE001 - 复核失败必须降级而不能打断对话
        removed = remove_memory_block(messages, pending.text)
        logger.info(
            f"[memory_send_guard] {chat_key} | 记忆证据复核失败，已降级"
            f" removed={removed} facts={len(pending.bundle.fact_ids)} error={exc}",
        )
        trace_emit(
            "memory_context_revalidated",
            chat_key,
            status=STATUS_DEGRADED,
            removed=removed,
            fact_count=len(pending.bundle.fact_ids),
            error_summary=str(exc)[:200],
        )
        return STATUS_DEGRADED

    trace_emit(
        "memory_context_revalidated",
        chat_key,
        status=STATUS_VERIFIED,
        fact_count=len(pending.bundle.fact_ids),
    )
    return STATUS_VERIFIED
