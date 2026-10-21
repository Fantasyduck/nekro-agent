"""发送前的统一提示词预算推导。

计量单位与 MemoryContextBuilder 保持一致：UTF-8 字节上界。本地无法精确换算
token，因此按 3 字节/token 的保守经验值折算模型窗口，并把估算方式写进
budget_method，便于之后用供应商计量校验。
"""

from __future__ import annotations

from dataclasses import dataclass

from nekro_agent.core.config import CoreConfig
from nekro_agent.services.agent.memory_context import ContextBudget
from nekro_agent.services.memory.contracts import MemoryError

#: 1 token 折算的 UTF-8 字节数（保守估计，宁小勿大）
BYTES_PER_TOKEN = 3
LEGACY_BUDGET_METHOD = "utf8_bytes_upper_bound:legacy_char_config"
WINDOW_BUDGET_METHOD = "utf8_bytes_upper_bound:window_tokens_x3"


@dataclass(frozen=True)
class PromptBudgetPlan:
    """一次请求的统一预算与它的估算方式。"""

    budget: ContextBudget
    method: str


def _units(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MemoryError("CONTEXT_BUDGET_INVALID")
    return value


def plan_prompt_budget(
    config: CoreConfig,
    *,
    prompt_overhead_units: int = 0,
    memory_limit_units: int | None = None,
) -> PromptBudgetPlan:
    """推导单次发送的记忆预算。

    Args:
        config: 当前配置，读取记忆块上限与（可选的）模型窗口声明。
        prompt_overhead_units: 已确定要发送的固定提示词（系统提示词等）字节数。
        memory_limit_units: 记忆块上限；缺省使用 MEMORY_CONTEXT_MAX_LENGTH。

    Returns:
        预算及其估算方式；未声明模型窗口时保持旧的“只约束记忆块”语义。
    """
    overhead = _units(prompt_overhead_units)
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
            method=LEGACY_BUDGET_METHOD,
        )

    reserved_output = _units(int(getattr(config, "AI_CONTEXT_RESERVED_OUTPUT_TOKENS", 0) or 0)) * BYTES_PER_TOKEN
    tool_reserve = _units(int(getattr(config, "AI_CONTEXT_TOOL_RESERVE_TOKENS", 0) or 0)) * BYTES_PER_TOKEN
    safety_margin = _units(int(getattr(config, "AI_CONTEXT_SAFETY_MARGIN_TOKENS", 0) or 0)) * BYTES_PER_TOKEN
    model_limit = window_tokens * BYTES_PER_TOKEN
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
        method=WINDOW_BUDGET_METHOD,
    )
