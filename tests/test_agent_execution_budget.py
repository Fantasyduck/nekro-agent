from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.services.agent.creator import OpenAIChatMessage

runtime = importlib.import_module("nekro_agent.services.agent.run_agent")


@pytest.fixture
def agent_harness(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    model = SimpleNamespace(CHAT_MODEL="test", ENABLE_COT=False)
    config = SimpleNamespace(
        MODEL_GROUPS={"test": model}, USE_MODEL_GROUP="test",
        AI_SCRIPT_MAX_RETRY_TIMES=3, AI_CHAT_LLM_API_MAX_RETRIES=1,
    )
    adapter = SimpleNamespace(
        set_dialog_example=AsyncMock(return_value=[]), get_jinja_env=AsyncMock(),
        get_self_info=AsyncMock(return_value=SimpleNamespace(platform_name="test", user_id="bot")),
        render_runtime_prompt=AsyncMock(return_value=""), chat_key_rules=[],
        config=SimpleNamespace(SESSION_ENABLE_AT=True),
    )
    channel = SimpleNamespace(
        get_effective_config=AsyncMock(return_value=config),
        get_preset=AsyncMock(return_value=SimpleNamespace(content="persona", id=1, name="test")),
        adapter=adapter, channel_name="test", channel_type="group",
    )
    ctx = SimpleNamespace(db_chat_channel=channel, adapter=adapter)
    compiler = MagicMock()
    compiler.render_system_message.return_value = OpenAIChatMessage.from_text("system", "persona")
    compiler.render_practice_messages.return_value = []
    compiler.render_history_message = AsyncMock(return_value=OpenAIChatMessage.from_text("user", "hello"))
    monkeypatch.setattr(runtime, "PromptCompiler", MagicMock(return_value=compiler))
    monkeypatch.setattr(runtime, "render_plugins_prompt", AsyncMock(return_value=SimpleNamespace(system_prompt="", runtime_prompt="")))
    monkeypatch.setattr(runtime, "build_plugin_call_priority_rules", lambda: "")
    monkeypatch.setattr(runtime, "plugin_collector", SimpleNamespace(get_plugin_by_module_name=lambda _: None, get_all_active_plugins=lambda: []))
    monkeypatch.setattr(runtime, "render_history_data", AsyncMock(return_value=OpenAIChatMessage.from_text("user", "history")))
    monkeypatch.setattr(runtime, "trace_emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime.os, "urandom", lambda _: SimpleNamespace(hex=lambda: "secret-nonce"))
    broadcast = importlib.import_module("nekro_agent.services.system_broadcast")
    states = AsyncMock()
    monkeypatch.setattr(broadcast, "publish_system_event", states)
    requests: list[str] = []

    async def request(**kwargs: object) -> tuple[SimpleNamespace, SimpleNamespace, list[str]]:
        messages = kwargs["messages"]
        requests.append(str(messages[-1].content))
        number = len(requests)
        response = SimpleNamespace(response_content=f"send_msg_text('reply-{number}')", use_model="test")
        return response, model, []

    monkeypatch.setattr(runtime, "send_agent_request", request)
    execute = AsyncMock()
    monkeypatch.setattr(runtime, "limited_run_code", execute)
    return SimpleNamespace(config=config, ctx=ctx, requests=requests, execute=execute, states=states)


@pytest.mark.asyncio
async def test_final_reply_executes_after_two_search_errors_and_tool_result(agent_harness: SimpleNamespace) -> None:
    h = agent_harness
    h.execute.side_effect = [("bad args", "", ExecStopType.ERROR), ("bad language", "", ExecStopType.ERROR),
                             ("search results", "", ExecStopType.AGENT), ("", "", ExecStopType.NORMAL)]
    await runtime.run_agent("test-group", ctx=h.ctx)
    assert h.execute.await_count == len(h.requests) == 4
    assert h.execute.await_args_list[-1].kwargs["code_run_data"].code_content == "send_msg_text('reply-4')"
    assert "This is the last retry" in h.requests[-1]
    assert "4/4" in h.requests[-1]
    assert h.states.await_args_list[-1].args[0].phase == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("retries", [0, 3])
async def test_normal_reply_completes_immediately(agent_harness: SimpleNamespace, retries: int) -> None:
    h = agent_harness
    h.config.AI_SCRIPT_MAX_RETRY_TIMES = retries
    h.execute.return_value = ("", "", ExecStopType.NORMAL)
    await runtime.run_agent("test-group", ctx=h.ctx)
    assert h.execute.await_count == len(h.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_type", [ExecStopType.ERROR, ExecStopType.AGENT, ExecStopType.TIMEOUT])
async def test_exhaustion_fails_without_generating_unexecutable_reply(agent_harness: SimpleNamespace, stop_type: ExecStopType) -> None:
    h = agent_harness
    h.execute.return_value = ("still pending", "", stop_type)
    with pytest.raises(runtime.AgentIterationLimitError, match="4/4"):
        await runtime.run_agent("test-group", ctx=h.ctx)
    assert h.execute.await_count == len(h.requests) == 4
    assert h.states.await_args_list[-1].args[0].phase == "failed"
    assert all("completed" != call.args[0].phase for call in h.states.await_args_list)


@pytest.mark.asyncio
async def test_security_rejection_respects_execution_budget(agent_harness: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    h = agent_harness
    h.config.AI_SCRIPT_MAX_RETRY_TIMES = 0
    monkeypatch.setattr(runtime, "parse_chat_response", lambda _: SimpleNamespace(code_content="secret-nonce"))
    with pytest.raises(runtime.AgentIterationLimitError, match="SECURITY"):
        await runtime.run_agent("test-group", ctx=h.ctx)
    h.execute.assert_not_awaited()
    assert len(h.requests) == 1

@pytest.mark.asyncio
async def test_tool_observation_manual_stop_continues_to_explicit_reply(agent_harness: SimpleNamespace) -> None:
    h = agent_harness
    observation = '{"status":"uncertain","character":null,"candidates":[]}'
    h.execute.side_effect = [(observation, observation, ExecStopType.MANUAL), ("", "", ExecStopType.NORMAL)]
    await runtime.run_agent("test-group", ctx=h.ctx)
    assert h.execute.await_count == len(h.requests) == 2
    assert observation in h.requests[-1]
    assert "send_msg_text" in h.execute.await_args_list[-1].kwargs["code_run_data"].code_content
    assert h.states.await_args_list[-1].args[0].phase == "completed"