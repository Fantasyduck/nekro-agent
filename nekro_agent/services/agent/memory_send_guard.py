"""发送模型请求前的记忆证据复核、有限重新解析与降级。

历史模板在装配 prompt 时把记忆块原文与它对应的 EvidenceBundle 登记到这里；
真正发送请求前再取回复核一次。按设计 §11.1，POLICY_CHANGED 之类可恢复的失败先
有限重新解析一次，成功则原地替换记忆块；仍然失败才剥离记忆块继续发送，整轮不失败。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
STATUS_REPARSED = "reparsed"
STATUS_DEGRADED = "degraded"

#: 发送前复核允许的重新解析次数上限；超过即降级，避免与外部状态反复拉扯。
MAX_REPARSE_ATTEMPTS = 1


@dataclass(frozen=True)
class PendingMemoryContext:
    """已装配进 prompt、尚未发送的一次记忆块。

    rebuild 是同一口径下的重新装配回调（复用同一 builder 与预算），用于
    复核失败后的有限重新解析；为 None 时只能降级剥离。
    """

    text: str
    bundle: ContextBundle
    context: RequestContext
    rebuild: Callable[[], Awaitable[tuple[str, ContextBundle]]] | None = None


_PENDING: dict[str, PendingMemoryContext] = {}


def register_pending(
    *, chat_key: str, text: str, bundle: ContextBundle, context: RequestContext,
    rebuild: Callable[[], Awaitable[tuple[str, ContextBundle]]] | None = None,
) -> None:
    """登记本次装配的记忆块，供发送前复核使用。"""
    _PENDING[chat_key] = PendingMemoryContext(text=text, bundle=bundle, context=context, rebuild=rebuild)


def clear_pending(chat_key: str) -> None:
    """清除登记；未注入记忆块时调用，避免复用到上一轮的旧证据。"""
    _PENDING.pop(chat_key, None)


def take_pending(chat_key: str) -> PendingMemoryContext | None:
    """取出并移除登记，保证每次复核只消费一次。"""
    return _PENDING.pop(chat_key, None)


def _rewrite_text_segments(messages: List[Any], old_text: str, new_text: str) -> bool:
    """把已装配消息中的记忆块原文替换为新文本，返回是否真的替换了内容。"""
    if not old_text:
        return False
    rewritten = False
    for message in messages:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        changed = False
        kept: list[Any] = []
        for segment in content:
            if isinstance(segment, dict) and isinstance(segment.get("text"), str) and old_text in segment["text"]:
                remainder = segment["text"].replace(old_text, new_text)
                changed = True
                if remainder:
                    kept.append({**segment, "text": remainder})
                continue
            kept.append(segment)
        if changed:
            rewritten = True
            message.content = kept
    return rewritten


def remove_memory_block(messages: List[Any], block_text: str) -> bool:
    """从已装配的消息中按原文移除记忆块，返回是否真的移除了内容。"""
    return _rewrite_text_segments(messages, block_text, "")


def replace_memory_block(messages: List[Any], old_text: str, new_text: str) -> bool:
    """用重新解析得到的新记忆块替换旧块，返回是否真的替换了内容。"""
    if not new_text:
        return remove_memory_block(messages, old_text)
    return _rewrite_text_segments(messages, old_text, new_text)


async def _reparse(
    pending: PendingMemoryContext,
) -> tuple[str, ContextBundle] | None:
    """有限重新解析：成功返回新的（块文本, bundle），失败返回 None。"""
    if pending.rebuild is None:
        return None
    for attempt in range(MAX_REPARSE_ATTEMPTS):
        try:
            text, bundle = await pending.rebuild()
        except Exception as exc:  # noqa: BLE001 - 重新解析失败只能继续降级
            logger.info(
                f"[memory_send_guard] 记忆上下文重新解析失败 attempt={attempt + 1}"
                f"/{MAX_REPARSE_ATTEMPTS} error={exc}",
            )
            continue
        return text, bundle
    return None


async def revalidate_before_send(messages: List[Any], chat_key: str) -> str:
    """发送前复核登记的记忆证据；可恢复的失败先重新解析一次，再失败才降级。

    Returns:
        skipped / verified / reparsed / degraded
    """
    if not chat_key:
        return STATUS_SKIPPED
    pending = take_pending(chat_key)
    if pending is None:
        return STATUS_SKIPPED

    try:
        await MemoryContextBuilder(MemoryService()).validate(pending.context, pending.bundle)
    except Exception as exc:  # noqa: BLE001 - 复核失败必须降级而不能打断对话
        error_summary = str(exc)[:200]
        reparsed = await _reparse(pending)
        if reparsed is not None:
            restored_text, restored_bundle = reparsed
            if replace_memory_block(messages, pending.text, restored_text):
                logger.info(
                    f"[memory_send_guard] {chat_key} | 记忆证据复核失败，已重新解析并替换记忆块"
                    f" facts={len(restored_bundle.fact_ids)} error={exc}",
                )
                trace_emit(
                    "memory_context_revalidated",
                    chat_key,
                    status=STATUS_REPARSED,
                    removed=False,
                    fact_count=len(restored_bundle.fact_ids),
                    previous_fact_count=len(pending.bundle.fact_ids),
                    error_summary=error_summary,
                )
                return STATUS_REPARSED
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
            error_summary=error_summary,
        )
        return STATUS_DEGRADED

    trace_emit(
        "memory_context_revalidated",
        chat_key,
        status=STATUS_VERIFIED,
        fact_count=len(pending.bundle.fact_ids),
    )
    return STATUS_VERIFIED
