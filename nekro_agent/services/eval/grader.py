"""评测断言：判断一次 prompt 组装是否「合格」。

分两类断言：

- **内容断言**（必须过）：该出现的工具、关键规则不能丢。
  这是红线——工具目录化如果让模型看不见某个工具，就是回归。
- **预算断言**（可告警）：prompt 体积、插件占比、工具数量。
  这些不是硬性红线，但是「改造是否有效」的量化依据。
"""

from dataclasses import dataclass, field
from typing import Any, List, Optional

from nekro_agent.services.agent.prompt_metrics import PromptMetrics, measure_prompt

from .case import EvalCase


@dataclass
class GradeResult:
    """单个用例的评测结果。"""

    case_id: str = ""
    passed: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    baseline: Optional[PromptMetrics] = None
    current: Optional[PromptMetrics] = None

    @property
    def chars_delta(self) -> int:
        if not self.baseline or not self.current:
            return 0
        return self.current.total_chars - self.baseline.total_chars

    @property
    def chars_delta_pct(self) -> float:
        if not self.baseline or not self.baseline.total_chars:
            return 0.0
        return round(self.chars_delta / self.baseline.total_chars, 4)


def grade_case(
    case: EvalCase,
    current_prompt: str,
    baseline_prompt: Optional[str] = None,
    *,
    max_chars: Optional[int] = None,
    max_method_ratio: Optional[float] = None,
    require_tools: bool = True,
    expected_tools: Optional[List[str]] = None,
) -> GradeResult:
    """对单个用例打分。

    `current_prompt` 是「改造后」渲染出的 prompt；
    `baseline_prompt` 缺省时用用例自带的（改造前）。
    """
    result = GradeResult(case_id=case.case_id)

    baseline_prompt = baseline_prompt if baseline_prompt is not None else case.system_prompt
    result.baseline = measure_prompt(baseline_prompt)
    result.current = measure_prompt(current_prompt)

    # ---- 内容断言：工具不能丢
    # live 模式下 expected_tools 来自「同口径 full 渲染」，
    # 避免把「插件激活状态变化」误判成「压缩丢了工具」
    want_tools = expected_tools if expected_tools is not None else case.expect_tools
    if require_tools and want_tools:
        current_names = set(result.current.method_names)
        missing = [t for t in want_tools if t not in current_names]
        if missing:
            result.passed = False
            result.errors.append(f"丢失 {len(missing)} 个工具定义: " + ", ".join(missing[:8]))

    # ---- 内容断言：关键规则不能丢
    for marker in case.expect_markers:
        if marker and marker not in current_prompt:
            result.passed = False
            result.errors.append(f"丢失关键规则片段: {marker[:40]!r}")

    # ---- 预算断言：体积
    if max_chars is not None and result.current.total_chars > max_chars:
        result.warnings.append(
            f"prompt 体积 {result.current.total_chars} 超过上限 {max_chars}"
        )

    # ---- 预算断言：工具占比
    if max_method_ratio is not None and result.current.method_ratio > max_method_ratio:
        result.warnings.append(
            f"工具定义占比 {result.current.method_ratio:.1%} 超过上限 {max_method_ratio:.1%}"
        )

    return result


def compare_cases(
    baseline_prompt: str,
    current_prompt: str,
    case: EvalCase,
    **kwargs: Any,
) -> GradeResult:
    """对比基线与当前，产出评分结果。"""
    return grade_case(case, current_prompt, baseline_prompt, **kwargs)
