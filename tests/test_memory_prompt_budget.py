"""验证发送前的统一预算推导：未声明窗口保持旧语义，声明窗口才做整窗分配。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nekro_agent.services.agent.prompt_budget import (
    BYTES_PER_TOKEN,
    LEGACY_BUDGET_METHOD,
    UTF8_UNITS,
    WINDOW_BUDGET_METHOD,
    PromptUnits,
    plan_prompt_budget,
    resolve_prompt_units,
)
from nekro_agent.services.memory.contracts import MemoryError

__all__ = ["pytest"]


def fake_config(**overrides: object) -> SimpleNamespace:
    base = {
        "MEMORY_CONTEXT_MAX_LENGTH": 1200,
        "AI_CONTEXT_WINDOW_TOKENS": 0,
        "AI_CONTEXT_RESERVED_OUTPUT_TOKENS": 1024,
        "AI_CONTEXT_TOOL_RESERVE_TOKENS": 0,
        "AI_CONTEXT_SAFETY_MARGIN_TOKENS": 256,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_undeclared_window_keeps_legacy_memory_only_semantics() -> None:
    plan = plan_prompt_budget(fake_config(), prompt_overhead_units=500)
    assert plan.method == LEGACY_BUDGET_METHOD
    assert plan.budget.memory_limit == 1200
    assert plan.budget.prompt_overhead == 500
    assert plan.budget.model_limit == 500 + 1200
    assert plan.budget.available == 1200


def test_declared_window_subtracts_output_tools_and_margin() -> None:
    plan = plan_prompt_budget(
        fake_config(AI_CONTEXT_WINDOW_TOKENS=8000, AI_CONTEXT_TOOL_RESERVE_TOKENS=100),
        prompt_overhead_units=3000,
    )
    assert plan.method == WINDOW_BUDGET_METHOD
    assert plan.budget.model_limit == 8000 * BYTES_PER_TOKEN
    assert plan.budget.reserved_output == 1024 * BYTES_PER_TOKEN
    assert plan.budget.tool_reserve == 100 * BYTES_PER_TOKEN
    assert plan.budget.safety_margin == 256 * BYTES_PER_TOKEN
    assert plan.budget.available == 8000 * BYTES_PER_TOKEN - (1024 + 100 + 256) * BYTES_PER_TOKEN - 3000


def test_memory_limit_never_exceeds_remaining_window() -> None:
    plan = plan_prompt_budget(
        fake_config(MEMORY_CONTEXT_MAX_LENGTH=100000, AI_CONTEXT_WINDOW_TOKENS=4000),
        prompt_overhead_units=6000,
    )
    assert plan.budget.memory_limit == plan.budget.available
    assert plan.budget.memory_limit < 100000


def test_memory_limit_is_capped_by_config_value() -> None:
    plan = plan_prompt_budget(
        fake_config(MEMORY_CONTEXT_MAX_LENGTH=1200, AI_CONTEXT_WINDOW_TOKENS=100000),
    )
    assert plan.budget.memory_limit == 1200


def test_explicit_memory_limit_overrides_config() -> None:
    plan = plan_prompt_budget(
        fake_config(MEMORY_CONTEXT_MAX_LENGTH=1200, AI_CONTEXT_WINDOW_TOKENS=100000),
        memory_limit_units=4096,
    )
    assert plan.budget.memory_limit == 4096


def test_negative_or_boolean_units_are_rejected() -> None:
    for bad in (-1, True, "1200", 12.5):
        with pytest.raises(MemoryError, match="CONTEXT_BUDGET_INVALID"):
            plan_prompt_budget(fake_config(), prompt_overhead_units=bad)  # type: ignore[arg-type]


def test_tool_result_units_compete_with_the_reserve() -> None:
    config = fake_config(AI_CONTEXT_WINDOW_TOKENS=8000, AI_CONTEXT_TOOL_RESERVE_TOKENS=100)
    reserved = plan_prompt_budget(config)
    assert reserved.budget.tool_reserve == 100 * BYTES_PER_TOKEN
    # 实际工具结果超过预留时按实际值占位，否则记忆块会把窗口挤爆
    actual = plan_prompt_budget(config, tool_result_units=5000)
    assert actual.budget.tool_reserve == 5000
    assert actual.budget.available < reserved.budget.available
    # 实际用量小于预留时仍按预留保留
    smaller = plan_prompt_budget(config, tool_result_units=10)
    assert smaller.budget.tool_reserve == reserved.budget.tool_reserve


def test_explicit_units_change_the_scale_and_the_method_name() -> None:
    units = PromptUnits(name="test_counter", per_token=1, counter=len)
    plan = plan_prompt_budget(
        fake_config(AI_CONTEXT_WINDOW_TOKENS=4000),
        prompt_overhead_units=100,
        units=units,
    )
    assert plan.method == "test_counter:window_tokens_x3"
    assert plan.budget.model_limit == 4000
    assert plan.budget.available == 4000 - (1024 + 0 + 256) - 100
    assert plan.counter is len


def test_units_are_used_in_legacy_mode_too() -> None:
    plan = plan_prompt_budget(fake_config(), prompt_overhead_units=10, units=UTF8_UNITS)
    assert plan.method == LEGACY_BUDGET_METHOD
    assert plan.counter("中") == len("中".encode("utf-8"))


def test_prompt_units_fall_back_to_utf8_when_tokenizer_is_unavailable() -> None:
    units = resolve_prompt_units()
    assert units.counter("hello") > 0
    # 真实分词器可用时应得到比字节上界更小的计数；不可用则退化为 3 字节/token 上界
    assert units.name == "utf8_bytes_upper_bound" or units.name.startswith("tiktoken:")


def test_overhead_larger_than_window_is_rejected() -> None:
    with pytest.raises(MemoryError, match="CONTEXT_BUDGET_INVALID"):
        plan_prompt_budget(
            fake_config(AI_CONTEXT_WINDOW_TOKENS=1000, AI_CONTEXT_RESERVED_OUTPUT_TOKENS=0,
                        AI_CONTEXT_SAFETY_MARGIN_TOKENS=0),
            prompt_overhead_units=100000,
        )
