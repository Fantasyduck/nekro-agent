import datetime
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from nekro_agent.core.config import CoreConfig, ModelConfigGroup
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import PROMPT_ERROR_LOG_DIR, PROMPT_LOG_DIR
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessage
from nekro_agent.services.plugin.call_priority import build_plugin_call_priority_rules
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.plugin.prompt_activation import build_plugin_activation_rules
from nekro_agent.services.sandbox.runner import limited_run_code

from .creator import OpenAIChatMessage
from .memory_send_guard import revalidate_before_send
from .openai import OpenAIResponse, gen_openai_chat_response
from .prompt_budget import PromptUnits, resolve_prompt_units
from .prompt_metrics import measure_prompt
from .resolver import ParsedCodeRunData, parse_chat_response
from .templates.compiler import PromptCompiler
from .templates.history import render_history_data
from .templates.plugin import render_plugins_prompt
from .trace import emit as trace_emit

# 使用deque保存最近100条错误日志路径

logger = get_sub_logger("agent_runtime")
RECENT_ERR_LOGS = deque(maxlen=100)


def _prompt_text_units(messages: List[OpenAIChatMessage], counter: Optional[Callable[[str], int]] = None) -> int:
    """统计已装配消息的文本占用量，作为记忆预算里的固定开销。

    计量器必须与记忆预算同源（tiktoken 或 UTF-8 字节上界），否则两边不同尺度会算错余量。
    """
    measure = counter or (lambda text: len(text.encode("utf-8")))
    units = 0
    for message in messages:
        content = message.content
        if isinstance(content, str):
            units += measure(content)
            continue
        if not isinstance(content, list):
            continue
        for segment in content:
            if isinstance(segment, dict) and isinstance(segment.get("text"), str):
                units += measure(segment["text"])
    return units


def _summarize_runtime_text(text: str, limit: int = 160) -> str:
    compact = " ".join(text.strip().split())
    if not compact:
        return ""
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


class AgentIterationLimitError(RuntimeError):
    """The execution budget was exhausted before the agent completed its task."""


class AllLLMRequestsFailedError(ValueError):
    """All LLM API retries are exhausted for a single agent request."""


async def run_agent(
    chat_key: str,
    chat_message: Optional[ChatMessage] = None,
    ctx: Optional[AgentCtx] = None,
):
    # 获取当前聊天频道的有效配置
    one_time_code = os.urandom(4).hex()
    db_chat_channel: DBChatChannel
    logger.debug(f"[run_agent] {chat_key} | 开始准备上下文")
    if ctx:
        if ctx.db_chat_channel:
            db_chat_channel = ctx.db_chat_channel
        else:
            db_chat_channel = await DBChatChannel.get(chat_key=chat_key)
            ctx = AgentCtx.create_by_db_chat_channel(db_chat_channel=db_chat_channel)
    else:
        db_chat_channel = await DBChatChannel.get(chat_key=chat_key)
        ctx = AgentCtx.create_by_db_chat_channel(db_chat_channel=db_chat_channel)

    config = await db_chat_channel.get_effective_config()
    preset = await db_chat_channel.get_preset()
    adapter_dialog_examples = await ctx.adapter.set_dialog_example()
    adapter_jinja_env = await ctx.adapter.get_jinja_env()
    self_info = await ctx.adapter.get_self_info()
    logger.debug(f"[run_agent] {chat_key} | 适配器信息就绪，开始渲染插件 prompt")

    # 获取当前使用的模型组
    used_model_group: ModelConfigGroup = config.MODEL_GROUPS[config.USE_MODEL_GROUP]
    activation_plugin = plugin_collector.get_plugin_by_module_name("plugin_activation")
    activation_enabled = bool(activation_plugin and activation_plugin.is_enabled)

    rendered_plugins = await render_plugins_prompt(
        plugin_collector.get_all_active_plugins(),
        ctx,
        activation_enabled=activation_enabled,
    )
    adapter_runtime_prompt = await ctx.adapter.render_runtime_prompt()
    runtime_prompts = [
        prompt for prompt in (adapter_runtime_prompt, rendered_plugins.runtime_prompt) if prompt.strip()
    ]
    runtime_prompt = "\n\n".join(runtime_prompts)
    logger.debug(f"[run_agent] {chat_key} | 插件 prompt 渲染完成，开始构建 system prompt")

    from nekro_agent.services.system_broadcast import AgentRuntimeStatusEvent, publish_system_event

    started_at = int(time.time() * 1000)
    iteration_total = config.AI_SCRIPT_MAX_RETRY_TIMES + 1
    llm_retry_total = config.AI_CHAT_LLM_API_MAX_RETRIES

    async def publish_runtime_state(
        *,
        phase: str,
        iteration_index: int,
        llm_retry_index: int = 1,
        model_name: Optional[str] = None,
        sandbox_stop_type: Optional[int] = None,
        error_summary: Optional[str] = None,
    ) -> None:
        # 轨迹采集：与状态广播同源，零侵入（未启用时只是一次布尔判断）
        try:
            trace_emit(
                "runtime_state",
                chat_key,
                phase=phase,
                iteration_index=iteration_index,
                iteration_total=iteration_total,
                llm_retry_index=llm_retry_index,
                llm_retry_total=llm_retry_total,
                model_name=model_name,
                sandbox_stop_type=sandbox_stop_type,
                error_summary=error_summary,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[run_agent] {chat_key} | 轨迹记录失败: {e}")
        try:
            await publish_system_event(
                AgentRuntimeStatusEvent(
                    chat_key=chat_key,
                    active=True,
                    channel_name=db_chat_channel.channel_name,
                    chat_type=db_chat_channel.channel_type,
                    preset_id=getattr(preset, "id", None),
                    preset_name=getattr(preset, "name", None),
                    started_at=started_at,
                    updated_at=int(time.time() * 1000),
                    phase=phase,
                    iteration_index=iteration_index,
                    iteration_total=iteration_total,
                    llm_retry_index=llm_retry_index,
                    llm_retry_total=llm_retry_total,
                    sandbox_stop_type=sandbox_stop_type,
                    model_name=model_name,
                    error_summary=error_summary,
                )
            )
        except Exception as e:
            logger.debug(f"[run_agent] {chat_key} | 运行阶段广播失败: {e}")

    prompt_compiler = PromptCompiler(
        platform_name=self_info.platform_name,
        bot_platform_id=self_info.user_id,
        chat_preset=preset.content,
        plugins_prompt=rendered_plugins.system_prompt,
        plugins_runtime_prompt=runtime_prompt,
        plugin_activation_rules=build_plugin_activation_rules() if activation_enabled else "",
        plugin_call_priority_rules=build_plugin_call_priority_rules(),
        enable_cot=used_model_group.ENABLE_COT,
        chat_key_rules="\n".join(f"- {r}" for r in db_chat_channel.adapter.chat_key_rules),
        enable_at=db_chat_channel.adapter.config.SESSION_ENABLE_AT,
    )

    # 计量器一次解析、全程复用：预算是同一尺度，不能两头各自估算
    prompt_units: PromptUnits = resolve_prompt_units()
    messages = [prompt_compiler.render_system_message()]
    messages.extend(prompt_compiler.render_practice_messages(adapter_dialog_examples, adapter_jinja_env))
    # 记忆块预算要扣除已经确定要发送的固定提示词，避免整窗超限
    messages.append(
        await prompt_compiler.render_history_message(
            chat_key=chat_key,
            db_chat_channel=db_chat_channel,
            one_time_code=one_time_code,
            config=config,
            model_group=used_model_group,
            trigger_message=chat_message,
            prompt_overhead_units=_prompt_text_units(messages, prompt_units.counter),
            units=prompt_units,
        ),
    )

    # 轨迹：prompt 组装度量（与离线 eval 共用同一套度量代码）
    try:
        _sys_text = messages[0].content if messages else ""
        if isinstance(_sys_text, list):
            _sys_text = "\n".join(
                str(b.get("text") or "") for b in _sys_text if isinstance(b, dict)
            )
        _pm = measure_prompt(str(_sys_text or ""))
        trace_emit(
            "prompt_built",
            chat_key,
            phase="prompt_built",
            iteration_index=1,
            model_name=used_model_group.CHAT_MODEL,
            prompt_metrics=_pm.to_dict(),
            message_count=len(messages),
        )
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[run_agent] {chat_key} | prompt 度量失败: {e}")

    logger.debug(f"[run_agent] {chat_key} | 历史记录渲染完成，发送 LLM 请求 (model={used_model_group.CHAT_MODEL})")
    history_render_until_time = time.time()
    llm_retry_errors: list[str] = []
    try:
        llm_response, used_model_group, llm_retry_errors = await send_agent_request(
            messages=messages,
            config=config,
            chat_key=chat_key,
            on_llm_retry=lambda retry_index, retry_total, model_name, error_summary: publish_runtime_state(
                phase="llm_retrying",
                iteration_index=1,
                llm_retry_index=retry_index,
                model_name=model_name,
                error_summary=error_summary,
            ),
            on_llm_attempt=lambda retry_index, _retry_total, model_name: publish_runtime_state(
                phase="llm_generating",
                iteration_index=1,
                llm_retry_index=retry_index,
                model_name=model_name,
            ),
        )
    except AllLLMRequestsFailedError as e:
        await publish_runtime_state(
            phase="failed",
            iteration_index=1,
            llm_retry_index=llm_retry_total,
            error_summary=_summarize_runtime_text(str(e)),
        )
        raise
    logger.debug(f"[run_agent] {chat_key} | LLM 请求完成，开始解析响应")
    parsed_code_data: ParsedCodeRunData = parse_chat_response(llm_response.response_content)

    for i in range(iteration_total):
        addition_prompt_message: List[OpenAIChatMessage] = []
        sandbox_output = ""
        raw_output = ""
        current_iteration = i + 1
        if one_time_code in parsed_code_data.code_content:
            stop_type = ExecStopType.SECURITY
        else:
            await publish_runtime_state(
                phase="sandbox_running",
                iteration_index=current_iteration,
                model_name=llm_response.use_model,
            )
            sandbox_output, raw_output, stop_type_value = await limited_run_code(
                code_run_data=parsed_code_data,
                from_chat_key=chat_key,
                chat_message=chat_message,
                llm_response=llm_response,
                ctx=ctx,
                llm_retry_errors=llm_retry_errors,
            )
            stop_type = ExecStopType(stop_type_value)

        if stop_type == ExecStopType.NORMAL:
            await publish_runtime_state(
                phase="completed",
                iteration_index=current_iteration,
                model_name=llm_response.use_model,
            )
            return

        await publish_runtime_state(
            phase="sandbox_stopped",
            iteration_index=current_iteration,
            model_name=llm_response.use_model,
            sandbox_stop_type=stop_type.value,
            error_summary=_summarize_runtime_text(sandbox_output),
        )

        # 最后一轮必须执行，但不能再生成一份不会被执行的回复。
        if current_iteration >= iteration_total:
            error_summary = (
                f"Agent 执行次数已耗尽 ({current_iteration}/{iteration_total})，"
                f"最后停止类型: {stop_type.name}。任务未正常完成。"
            )
            logger.error(f"[run_agent] {chat_key} | {error_summary}")
            await publish_runtime_state(
                phase="failed",
                iteration_index=current_iteration,
                model_name=llm_response.use_model,
                sandbox_stop_type=stop_type.value,
                error_summary=error_summary,
            )
            raise AgentIterationLimitError(error_summary)

        # 添加 AI 回复的原始内容到上下文
        addition_prompt_message.append(OpenAIChatMessage.from_text("assistant", llm_response.response_content))

        msg: OpenAIChatMessage = OpenAIChatMessage.create_empty("user")  # 待添加到迭代上下文的用户消息

        # Agent 类型的迭代对话
        if stop_type == ExecStopType.AGENT:
            msg = msg.extend(
                OpenAIChatMessage.from_text(
                    "user",
                    f"[Agent Method Response] {sandbox_output}\nPlease continue based on this agent response. Attention: the code after the agent method is NOT EXECUTED!",
                ),
            )

        # 多模态类型的迭代对话
        elif stop_type == ExecStopType.MULTIMODAL_AGENT:
            multimodal_agent_result = json.loads(raw_output.split("<AGENT_RESULT>")[1].split("</AGENT_RESULT>")[0])
            if isinstance(multimodal_agent_result, list):
                msg = msg.extend(OpenAIChatMessage("user", multimodal_agent_result))
            elif isinstance(multimodal_agent_result, str):
                msg = msg.extend(OpenAIChatMessage.from_text("user", multimodal_agent_result))
            elif isinstance(multimodal_agent_result, dict):
                msg = msg.extend(OpenAIChatMessage(**multimodal_agent_result))
            else:
                raise ValueError(f"Multimodal agent result is not a list or string: {multimodal_agent_result}")
            msg = msg.extend(OpenAIChatMessage.from_text("user", "Attention: the code AFTER THE AGENT METHOD is NOT EXECUTED!"))

        # 异常类型的迭代对话
        exception_reason_map: Dict[ExecStopType, str] = {
            ExecStopType.TIMEOUT: "Sandbox exited due to timeout",
            ExecStopType.ERROR: "Sandbox exited due to error occurred",
            ExecStopType.MANUAL: "Sandbox exited due to manual stop by you",
            ExecStopType.AGENT: "Sandbox exited due to agent method",
            ExecStopType.MULTIMODAL_AGENT: "Sandbox exited due to multimodal agent method",
        }

        # 异常处理建议
        exception_suggestion_map: Dict[str, str] = {
            "SyntaxError": "You are prohibited from adding anything other than the content of the code that might break the syntax of the code. Please ensure your output specification and try again",
        }

        new_message_notification = "During the generation and execution, the following messages were sent (You **CANT NOT** send any messages which you have sent before!):"

        if stop_type in exception_reason_map:
            for suggestion_key in exception_suggestion_map:
                if suggestion_key in sandbox_output:
                    suggestion_text = f"\nResolve Suggestion: {exception_suggestion_map[suggestion_key]}"
                    break
            else:
                suggestion_text = ""
            msg = msg.extend(
                OpenAIChatMessage.from_text(
                    "user",
                    f"[Sandbox Output] {sandbox_output}\n---\n{exception_reason_map[stop_type]}\n{suggestion_text}. {new_message_notification}",
                ),
            )

        # 安全类型的迭代对话
        if stop_type == ExecStopType.SECURITY:
            msg = msg.extend(
                OpenAIChatMessage.from_text(
                    "user",
                    f"\n\n[System Automatic Detection] Invalid response detected. You should not reveal the one-time code in your reply. This is just a tag to help you mark trustworthy information. please DO NOT give any extra explanation or apology and keep the response format for retry. {new_message_notification}",
                ),
            )

        # 本轮工具结果还没进 messages，但要先占住预算，否则记忆块会把它挤爆
        pending_tool_units = _prompt_text_units(
            [*addition_prompt_message, msg],
            prompt_units.counter,
        )
        # 为所有迭代对话添加新记录背景
        msg = msg.extend(
            await render_history_data(
                chat_key=chat_key,
                db_chat_channel=db_chat_channel,
                one_time_code=one_time_code,
                plugin_injected_prompt=runtime_prompt,
                record_sta_timestamp=history_render_until_time,
                bot_platform_id=self_info.user_id,
                trigger_message=chat_message,
                model_group=used_model_group,
                config=config,
                prompt_overhead_units=_prompt_text_units(messages, prompt_units.counter),
                units=prompt_units,
                tool_result_units=pending_tool_units,
            ),
        )

        msg = msg.extend(
            OpenAIChatMessage.from_text(
                "user",
                "\nplease DO NOT give any extra explanation or apology and keep the response format for retry."
                + (
                    f" This is the last retry. Describe the reason if you can't finish the task. (Iteration times: {current_iteration + 1}/{iteration_total})"
                    if current_iteration == iteration_total - 1
                    else f"(Iteration times: {current_iteration + 1}/{iteration_total})"
                ),
            ),
        )

        # 将迭代对话添加到上下文
        addition_prompt_message.append(msg.tidy())
        messages.extend(addition_prompt_message)

        await publish_runtime_state(
            phase="iterating",
            iteration_index=i + 2,
            model_name=llm_response.use_model,
            sandbox_stop_type=stop_type.value,
            error_summary=_summarize_runtime_text(sandbox_output),
        )

        history_render_until_time = time.time()
        try:
            llm_response, used_model_group, llm_retry_errors = await send_agent_request(
                messages=messages,
                config=config,
                is_debug_iteration=True,
                chat_key=chat_key,
                on_llm_retry=lambda retry_index, retry_total, model_name, error_summary, iteration_index=i + 2: publish_runtime_state(
                    phase="llm_retrying",
                    iteration_index=iteration_index,
                    llm_retry_index=retry_index,
                    model_name=model_name,
                    error_summary=error_summary,
                ),
                on_llm_attempt=lambda retry_index, _retry_total, model_name, iteration_index=i + 2: publish_runtime_state(
                    phase="llm_generating",
                    iteration_index=iteration_index,
                    llm_retry_index=retry_index,
                    model_name=model_name,
                ),
            )
        except AllLLMRequestsFailedError as e:
            await publish_runtime_state(
                phase="failed",
                iteration_index=i + 2,
                llm_retry_index=llm_retry_total,
                error_summary=_summarize_runtime_text(str(e)),
            )
            raise
        parsed_code_data = parse_chat_response(llm_response.response_content)


async def send_agent_request(
    messages: List[OpenAIChatMessage],
    config: CoreConfig,
    is_debug_iteration: bool = False,
    chat_key: str = "",
    on_llm_attempt: Optional[Callable[[int, int, str], Awaitable[None]]] = None,
    on_llm_retry: Optional[Callable[[int, int, str, str], Awaitable[None]]] = None,
) -> Tuple[OpenAIResponse, ModelConfigGroup, list[str]]:
    model_group: ModelConfigGroup = (
        config.MODEL_GROUPS[config.DEBUG_MIGRATION_MODEL_GROUP]
        if is_debug_iteration and config.DEBUG_MIGRATION_MODEL_GROUP
        else config.MODEL_GROUPS[config.USE_MODEL_GROUP]
    )
    fallback_model_group: ModelConfigGroup = (
        config.MODEL_GROUPS[config.FALLBACK_MODEL_GROUP] if config.FALLBACK_MODEL_GROUP else model_group
    )

    if config.SAVE_PROMPTS_LOG:
        log_path = f"{PROMPT_LOG_DIR}/chat_log_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    else:
        log_path = None
    err_log_path = (
        f"{PROMPT_ERROR_LOG_DIR}/chat_err_{model_group.CHAT_MODEL}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    )

    used_model_group: ModelConfigGroup = model_group  # 记录实际使用的模型组
    retry_errors: list[str] = []

    # 发送前复核记忆证据：失败只移除记忆块并继续发送，绝不阻断本轮对话
    try:
        guard_status = await revalidate_before_send(messages, chat_key)
        if guard_status != "skipped":
            logger.debug(f"[send_agent_request] {chat_key} | 记忆证据复核结果={guard_status}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[send_agent_request] {chat_key} | 记忆证据复核异常，保留原 prompt 继续发送: {e}")

    for i in range(config.AI_CHAT_LLM_API_MAX_RETRIES):
        use_model_group: ModelConfigGroup = model_group if i < config.AI_CHAT_LLM_API_MAX_RETRIES - 1 else fallback_model_group
        retry_index = i + 1
        if on_llm_attempt is not None:
            await on_llm_attempt(retry_index, config.AI_CHAT_LLM_API_MAX_RETRIES, use_model_group.CHAT_MODEL)

        logger.info(
            f"[send_agent_request] {chat_key} | 发送 LLM 请求 model={use_model_group.CHAT_MODEL} retry={i}/{config.AI_CHAT_LLM_API_MAX_RETRIES}"
        )
        try:
            llm_response: OpenAIResponse = await gen_openai_chat_response(
                model=use_model_group.CHAT_MODEL,
                messages=messages,
                temperature=use_model_group.TEMPERATURE,
                top_p=use_model_group.TOP_P,
                top_k=use_model_group.TOP_K,
                frequency_penalty=use_model_group.FREQUENCY_PENALTY,
                presence_penalty=use_model_group.PRESENCE_PENALTY,
                extra_body=use_model_group.EXTRA_BODY,
                base_url=use_model_group.BASE_URL,
                api_key=use_model_group.API_KEY,
                stream_mode=config.AI_REQUEST_STREAM_MODE,
                proxy_url=use_model_group.CHAT_PROXY,
                max_wait_time=config.AI_GENERATE_TIMEOUT,
                first_token_timeout=config.AI_STREAM_FIRST_TOKEN_TIMEOUT,
                log_path=log_path,
                error_log_path=err_log_path,
            )
        except Exception as e:
            error_summary = _summarize_runtime_text(str(e))
            retry_errors.append(str(e))
            logger.error(
                f"LLM 请求失败: {e} ｜ 使用模型: {use_model_group.CHAT_MODEL} {'(fallback)' if i == config.AI_CHAT_LLM_API_MAX_RETRIES - 1 else ''}",
            )
            if on_llm_retry is not None:
                await on_llm_retry(retry_index, config.AI_CHAT_LLM_API_MAX_RETRIES, use_model_group.CHAT_MODEL, error_summary)
            # 避免重复添加，转换为Path对象并比较绝对路径
            err_log_path_obj = Path(err_log_path)
            if not any(str(log_path.absolute()) == str(err_log_path_obj.absolute()) for log_path in RECENT_ERR_LOGS):
                RECENT_ERR_LOGS.append(err_log_path_obj)
            continue
        else:
            used_model_group = use_model_group  # 记录成功使用的模型组
            break
    else:
        err_log = Path(f"{PROMPT_LOG_DIR}/chat_err_log_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.log")
        err_log.parent.mkdir(parents=True, exist_ok=True)
        err_log.write_text(
            json.dumps([message.to_dict() for message in messages], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        raise AllLLMRequestsFailedError("所有 LLM 请求失败")

    return llm_response, used_model_group, retry_errors
