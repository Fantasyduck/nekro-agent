"""验证发送前的记忆证据复核与降级剥离。"""

from __future__ import annotations

import pytest
from test_memory_fact_service import DB_URL, context, isolated_db, proposal

from nekro_agent.services.agent import memory_send_guard
from nekro_agent.services.agent.creator import OpenAIChatMessage
from nekro_agent.services.agent.memory_context import ContextBudget, MemoryContextBuilder
from nekro_agent.services.agent.memory_send_guard import (
    STATUS_DEGRADED,
    STATUS_SKIPPED,
    STATUS_VERIFIED,
    clear_pending,
    register_pending,
    remove_memory_block,
    revalidate_before_send,
    take_pending,
)
from nekro_agent.services.memory.contracts import RequestContext
from nekro_agent.services.memory.service import MemoryService

__all__ = ["isolated_db"]
requires_db = pytest.mark.skipif(not DB_URL, reason="需要显式指定隔离 PostgreSQL")

CHAT_KEY = "private_alice"
BLOCK = '[fact:1; version:1; status:verified] alice / drink = "tea"\n\n'


def fake_context() -> RequestContext:
    return RequestContext(
        workspace_id=1, agent_id="test-agent", actor_key="service:answer",
        chat_key=CHAT_KEY, purpose="answer",
    )


def budget() -> ContextBudget:
    return ContextBudget(
        model_limit=4096, reserved_output=256, tool_reserve=256,
        safety_margin=64, prompt_overhead=64, memory_limit=1000,
    )


def messages_with_block(block: str) -> list[OpenAIChatMessage]:
    first = OpenAIChatMessage.from_text("user", "system prefetch")
    second = OpenAIChatMessage.from_text("user", f"history head\n{block}Recent Messages:\n")
    return [first, second]


def test_register_and_take_is_single_use() -> None:
    memory_send_guard._PENDING.clear()
    register_pending(chat_key=CHAT_KEY, text="block", bundle=None, context=fake_context())  # type: ignore[arg-type]
    first = take_pending(CHAT_KEY)
    assert first is not None and first.text == "block"
    assert take_pending(CHAT_KEY) is None


def test_clear_pending_removes_registration() -> None:
    memory_send_guard._PENDING.clear()
    register_pending(chat_key=CHAT_KEY, text="block", bundle=None, context=fake_context())  # type: ignore[arg-type]
    clear_pending(CHAT_KEY)
    assert take_pending(CHAT_KEY) is None


def test_remove_memory_block_strips_only_the_block() -> None:
    messages = messages_with_block(BLOCK)
    assert remove_memory_block(messages, BLOCK) is True
    text = messages[1].content[0]["text"]
    assert "fact:1" not in text
    assert text.startswith("history head") and "Recent Messages:" in text


def test_remove_memory_block_is_noop_for_unknown_text() -> None:
    messages = messages_with_block("noise")
    assert remove_memory_block(messages, "missing block") is False
    assert messages[1].content[0]["text"] == "history head\nnoiseRecent Messages:\n"


def test_remove_memory_block_ignores_non_text_segments() -> None:
    message = OpenAIChatMessage.from_text("user", "keep")
    message.content.append({"type": "image_url", "image_url": {"url": "http://example.invalid/a.png"}})
    assert remove_memory_block([message], "keep") is True
    assert message.content == [{"type": "image_url", "image_url": {"url": "http://example.invalid/a.png"}}]


async def test_revalidate_is_skipped_without_registration() -> None:
    memory_send_guard._PENDING.clear()
    messages = messages_with_block("noise")
    assert await revalidate_before_send(messages, CHAT_KEY) == STATUS_SKIPPED
    assert await revalidate_before_send(messages, "") == STATUS_SKIPPED


@requires_db
async def test_verified_context_is_kept_for_sending() -> None:
    service = MemoryService()
    await service.propose_fact(context(), await proposal(), idempotency_key="guard-ok")
    bundle = await MemoryContextBuilder(service).build(context(), budget())
    block = bundle.text + "\n\n"
    messages = messages_with_block(block)
    register_pending(chat_key=CHAT_KEY, text=block, bundle=bundle, context=context())

    assert await revalidate_before_send(messages, CHAT_KEY) == STATUS_VERIFIED
    assert "fact:" in messages[1].content[0]["text"]


@requires_db
async def test_revoked_fact_degrades_to_prompt_without_memory() -> None:
    service = MemoryService()
    result = await service.propose_fact(context(), await proposal(), idempotency_key="guard-revoked")
    bundle = await MemoryContextBuilder(service).build(context(), budget())
    block = bundle.text + "\n\n"
    messages = messages_with_block(block)
    register_pending(chat_key=CHAT_KEY, text=block, bundle=bundle, context=context())

    await service.forget(context(), result.assertion_id)

    assert await revalidate_before_send(messages, CHAT_KEY) == STATUS_DEGRADED
    text = messages[1].content[0]["text"]
    assert "fact:" not in text and "Recent Messages:" in text
