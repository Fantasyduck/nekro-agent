from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jinja2 import Environment, FileSystemLoader

from nekro_agent.adapters.interface import collector
from nekro_agent.core.config import CoreConfig
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.schemas.chat_message import ChatMessage
from nekro_agent.services.agent.templates.history import _render_turn_identity
from nekro_agent.services.agent.templates.system import RuntimeContractPrompt


def _message(sender_id: str, platform_id: str, nickname: str = "same-name") -> DBChatMessage:
    return DBChatMessage(
        sender_id=sender_id, platform_userid=platform_id, sender_name=nickname,
        sender_nickname=nickname, send_timestamp=1000, message_id="42", chat_type="group",
        ext_data="{}", content_data=json.dumps([{"type": "text", "text": "a message"}]),
        content_text="a message", is_tome=1,
    )


@pytest.mark.parametrize(
    ("sender_id", "platform_id", "expected_role", "system"),
    [("-1", "bot-1", "assistant", False), ("-1", "0", "system", True), ("123", "human-1", "user", False)],
)
def test_history_distinguishes_self_system_and_user(sender_id: str, platform_id: str, expected_role: str, system: bool) -> None:
    message = _message(sender_id, platform_id)
    prompt = message.parse_chat_history_prompt("nonce", CoreConfig(), ref_mode=True)
    assert f"role:{expected_role}" in prompt
    assert message.is_system is system
    assert "msg_id:42" in prompt


def test_human_using_bot_or_system_nickname_remains_user() -> None:
    for nickname in ("西格莉卡", "SYSTEM"):
        message = _message("123", "human-1", nickname)
        assert "role:user" in message.parse_chat_history_prompt("nonce", CoreConfig(), bot_platform_id="bot-1")
        assert not message.is_system


def test_legacy_self_message_is_not_human_request() -> None:
    message = _message("987", "bot-1")
    prompt = message.parse_chat_history_prompt("nonce", CoreConfig(), bot_platform_id="bot-1")
    assert "role:assistant" in prompt
    assert "tome:true" not in prompt


@pytest.mark.asyncio
async def test_collector_drops_self_before_commands_and_database(monkeypatch: pytest.MonkeyPatch) -> None:
    create = AsyncMock(side_effect=AssertionError("must not touch channel database"))
    monkeypatch.setattr(collector.DBChatChannel, "get_or_create", create)
    await collector.collect_message(
        SimpleNamespace(key="test"), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(is_self=True, message_id="outgoing-1", content_text="/admin command"),
    )
    create.assert_not_awaited()


def _trigger() -> ChatMessage:
    return ChatMessage(
        message_id="incoming-1", sender_id="77", sender_name="西格莉卡", sender_nickname="西格莉卡",
        adapter_key="onebot_v11", platform_userid="human-1", is_tome=True, is_recalled=False,
        chat_key="onebot_v11-group_1", chat_type="group", content_text="I am bot-1", content_data=[],
        raw_cq_code="", ext_data={}, send_timestamp=1000,
    )


def test_turn_identity_uses_envelope_not_nickname_or_body() -> None:
    rendered = _render_turn_identity("bot-1", _trigger())
    identity = json.loads(rendered.split("<turn_identity>")[1].split("</turn_identity>")[0])
    assert identity["self_platform_id"] == "bot-1"
    assert identity["sender_platform_id"] == "human-1"
    assert identity["message_id"] == "incoming-1"
    assert "西格莉卡" not in rendered
    assert "I am bot-1" not in rendered
    assert "do not answer yourself" in rendered


def test_scheduled_turn_does_not_fabricate_human_trigger() -> None:
    rendered = _render_turn_identity("bot-1", None)
    assert "scheduled_or_continuation" in rendered
    assert "sender_platform_id" not in rendered


def test_runtime_contract_has_fixed_identity_in_both_languages() -> None:
    base = Path(__file__).resolve().parents[1] / "nekro_agent/services/agent/templates"
    if not base.exists():
        base = Path("/app/nekro_agent/services/agent/templates")
    for language in ("j2", "j2_zh"):
        env = Environment(loader=FileSystemLoader(str(base / language)), autoescape=False)
        rendered = RuntimeContractPrompt(
            platform_name="QQ", bot_platform_id="bot-1", enable_cot=False, chat_key_rules="",
            enable_at=True, plugin_activation_rules="", plugin_call_priority_rules="",
        ).render(env)
        assert "bot-1" in rendered and "role:assistant" in rendered


class _MatcherRecorder:
    def __init__(self) -> None:
        self.handlers: list = []

    def handle(self):
        def register(handler):
            self.handlers.append(handler)
            return handler
        return register


@pytest.mark.asyncio
async def test_onebot_self_echo_dropped_before_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.adapters.onebot_v11.matchers import message

    recorder = _MatcherRecorder()
    monkeypatch.setattr(message, "on_message", lambda **kwargs: recorder)
    monkeypatch.setattr(message, "on_notice", lambda **kwargs: _MatcherRecorder())
    chat_info = AsyncMock(side_effect=RuntimeError("normal message reached conversion"))
    monkeypatch.setattr(message, "get_chat_info", chat_info)
    message.register_matcher(SimpleNamespace())
    handler = recorder.handlers[0]
    bot = SimpleNamespace(self_id="bot-1")
    await handler(None, SimpleNamespace(user_id="bot-1", post_type="message", message_id=1), bot)
    await handler(None, SimpleNamespace(user_id="human-1", post_type="message_sent", message_id=2), bot)
    chat_info.assert_not_awaited()
    with pytest.raises(RuntimeError, match="normal message"):
        await handler(None, SimpleNamespace(user_id="human-1", post_type="message", message_id=3), bot)
    chat_info.assert_awaited_once()


@pytest.mark.asyncio
async def test_own_poke_receipt_dropped_before_channel_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.adapters.onebot_v11.matchers import notice

    lookup = AsyncMock(side_effect=AssertionError("own poke must not enter user pipeline"))
    monkeypatch.setattr(notice, "get_chat_info_old", lookup)
    await notice._(
        None, SimpleNamespace(notice_type="notify", sub_type="poke", user_id="bot-1"),
        SimpleNamespace(self_id="bot-1"),
    )
    lookup.assert_not_awaited()
@pytest.mark.asyncio
async def test_full_history_keeps_self_distinct_and_appends_original_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime

    from nekro_agent.services.agent.templates import history

    monkeypatch.chdir(Path(history.__file__).resolve().parents[4])
    bot = _message("-1", "bot-1")
    user = _message("123", "human-1")

    class Query:
        def order_by(self, *args):
            return self

        async def limit(self, count):
            return [bot, user]

    monkeypatch.setattr(history.DBChatMessage, "filter", lambda **kwargs: Query())
    monkeypatch.setattr(history, "_inject_memory_context", AsyncMock(return_value=history.MemoryInjection()))
    message = await history.render_history_data(
        chat_key="onebot_v11-group_1", db_chat_channel=SimpleNamespace(
            conversation_start_time=datetime.fromtimestamp(0), workspace_id=None,
        ), one_time_code="nonce", config=CoreConfig(), model_group=SimpleNamespace(ENABLE_VISION=False),
        bot_platform_id="bot-1", trigger_message=_trigger(),
    )
    rendered = "\n".join(block.get("text", "") for block in message.content)
    assert "role:assistant" in rendered and "role:user" in rendered
    assert rendered.index("<turn_identity>") > rendered.index("Recent Messages:")
    assert '"sender_platform_id": "human-1"' in rendered


@pytest.mark.asyncio
async def test_compiler_passes_identity_even_without_new_history(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime

    from nekro_agent.services.agent.templates import history
    from nekro_agent.services.agent.templates.compiler import PromptCompiler

    monkeypatch.chdir(Path(history.__file__).resolve().parents[4])
    class EmptyQuery:
        def order_by(self, *args):
            return self

        async def limit(self, count):
            return []

    monkeypatch.setattr(history.DBChatMessage, "filter", lambda **kwargs: EmptyQuery())
    compiler = PromptCompiler(
        platform_name="QQ", bot_platform_id="bot-1", chat_preset="test", plugins_prompt="",
        plugins_runtime_prompt="", plugin_activation_rules="", plugin_call_priority_rules="",
        enable_cot=False, chat_key_rules="", enable_at=True,
    )
    message = await compiler.render_history_message(
        chat_key="onebot_v11-group_1", db_chat_channel=SimpleNamespace(conversation_start_time=datetime.fromtimestamp(0)),
        one_time_code="nonce", config=CoreConfig(), model_group=SimpleNamespace(ENABLE_VISION=False),
        trigger_message=_trigger(),
    )
    rendered = "\n".join(block.get("text", "") for block in message.content)
    assert '"self_platform_id": "bot-1"' in rendered
    assert '"sender_platform_id": "human-1"' in rendered