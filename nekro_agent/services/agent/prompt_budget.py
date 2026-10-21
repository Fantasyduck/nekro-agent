"""发送前的统一提示词预算推导。

计量单位由 PromptUnits 决定：优先使用真实分词器（tiktoken），不可用时回退到
UTF-8 字节上界（3 字节/token）。无论走哪条路径，估算方式都写进 budget_method，
并随 ContextBundle 落到 trace，便于之后用供应商计量核对。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from nekro_agent.core.config import CoreConfig
from nekro_agent.services.agent.memory_context import ContextBudget, conservative_units
from nekro_agent.services.memory.contracts import MemoryError

#: 1 token 折算的 UTF-8 字节数（保守估计，宁小勿大）
BYTES_PER_TOKEN = 3
#: 未声明窗口时的估算口径：只约束记忆块。
LEGACY_BUDGET_KIND = "legacy_char_config"
#: 声明窗口后的估算口径：按整窗分配。
WINDOW_BUDGET_KIND = "window_tokens_x3"
#: 真实分词器默认编码；中文与中英混排在 cl100k_base 上偏差可接受。
DEFAULT_TOKENIZER_ENCODING = "cl100k_base"


@dataclass(frozen=True)
class PromptUnits:
    """一次请求使用的计量单位：名字、每 token 单位数与计数器。"""

    name: str
    per_token: int
    counter: Callable[[str], int]

    def method(self, kind: str) -> str:
        return f"{self.name}:{kind}"


UTF8_UNITS = PromptUnits(name="utf8_bytes_upper_bound", per_token=BYTES_PER_TOKEN, counter=conservative_units)
LEGACY_BUDGET_METHOD = UTF8_UNITS.method(LEGACY_BUDGET_KIND)
WINDOW_BUDGET_METHOD = UTF8_UNITS.method(WINDOW_BUDGET_KIND)

_RESOLVED_UNITS: dict[str, PromptUnits] = {}


def tokenizer_method(encoding: str) -> str:
    """真实分词器的计量方式名，写入 budget_method。"""
    return f"tiktoken:{encoding}"


def resolve_prompt_units(encoding: str = DEFAULT_TOKENIZER_ENCODING) -> PromptUnits:
    """优先返回真实分词器；tiktoken 缺失或词表不可用时如实回退到字节上界。

    结果按编码缓存：分词器只在首次使用时解析，避免每次装配重复加载。
    """
    cached = _RESOLVED_UNITS.get(encoding)
    if cached is not None:
        return cached
    units = UTF8_UNITS
    try:
        import tiktoken  # noqa: PLC0415 - 可选依赖，仅在需要精确计量时导入

        tokenizer = tiktoken.get_encoding(encoding)
    except Exception:  # noqa: BLE001 - 词表缺失或离线都属于预期情况
        tokenizer = None
    if tokenizer is not None:

        def counter(text: str) -> int:
            return len(tokenizer.encode(text, disallowed_special=()))

        units = PromptUnits(tokenizer_method(encoding), 1, counter)
    _RESOLVED_UNITS[encoding] = units
    return units


@dataclass(frozen=True)
class PromptBudgetPlan:
    """一次请求的统一预算与它的估算方式。"""

    budget: ContextBudget
    method: str
    units: PromptUnits = field(default=UTF8_UNITS)

    @property
    def counter(self) -> Callable[[str], int]:
        """与本次预算同源的计数器，必须传给 MemoryContextBuilder 才能同尺度比较。"""
        return self.units.counter


def _units(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MemoryError("CONTEXT_BUDGET_INVALID")
    return value


def plan_prompt_budget(
    config: CoreConfig,
    *,
    prompt_overhead_units: int = 0,
    memory_limit_units: int | None = None,
    units: PromptUnits | None = None,
    tool_result_units: int = 0,
) -> PromptBudgetPlan:
    """推导单次发送的记忆预算。

    Args:
        config: 当前配置，读取记忆块上限与（可选的）模型窗口声明。
        prompt_overhead_units: 已确定要发送的固定提示词（系统提示词等）占用量。
        memory_limit_units: 记忆块上限；缺省使用 MEMORY_CONTEXT_MAX_LENGTH。
        units: 计量单位；缺省回退 UTF-8 上界，调用方可传 resolve_prompt_units()。
        tool_result_units: 本轮已装配的工具结果实际占用；与预留取较大者参与同一预算。

    Returns:
        预算及其估算方式；未声明模型窗口时保持旧的“只约束记忆块”语义。
    """
    active = units or UTF8_UNITS
    per_token = _units(active.per_token)
    overhead = _units(prompt_overhead_units)
    tool_actual = _units(tool_result_units)
    memory = _units(
        memory_limit_units if memory_limit_units is not None else getattr(config, "MEMORY_CONTEXT_MAX_LENGTH", 0),
    )
    window_tokens = _units(int(getattr(config, "AI_CONTEXT_WINDOW_TOKENS", 0) or 0))

    if window_tokens <= 0:
        # 未声明模型窗口：沿用旧语义，仅对记忆块做上限约束，不猜测整窗大小。
        return PromptBudgetPlan(
            budget=ContextBudget(
                model_limit=overhead + max(memory, 1),
                reserved_output=0,
                tool_reserve=0,
                safety_margin=0,
                prompt_overhead=overhead,
                memory_limit=memory,
            ),
            method=active.method(LEGACY_BUDGET_KIND),
            units=active,
        )

    reserved_output = _units(int(getattr(config, "AI_CONTEXT_RESERVED_OUTPUT_TOKENS", 0) or 0)) * per_token
    configured_tool_reserve = _units(int(getattr(config, "AI_CONTEXT_TOOL_RESERVE_TOKENS", 0) or 0)) * per_token
    # 工具与记忆在同一窗口里竞争：已装配的工具结果比配置预留更大时按实际扣减。
    tool_reserve = max(configured_tool_reserve, tool_actual)
    safety_margin = _units(int(getattr(config, "AI_CONTEXT_SAFETY_MARGIN_TOKENS", 0) or 0)) * per_token
    model_limit = window_tokens * per_token
    remaining = model_limit - reserved_output - tool_reserve - safety_margin - overhead
    if remaining <= 0:
        raise MemoryError("CONTEXT_BUDGET_INVALID")

    return PromptBudgetPlan(
        budget=ContextBudget(
            model_limit=model_limit,
            reserved_output=reserved_output,
            tool_reserve=tool_reserve,
            safety_margin=safety_margin,
            prompt_overhead=overhead,
            memory_limit=min(memory, remaining),
        ),
        method=active.method(WINDOW_BUDGET_KIND),
        units=active,
    )
