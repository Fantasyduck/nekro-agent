"""离线评测框架（M0）。

目的：把「改完 prompt 组装到底变好了没有」变成**可量化、可回归**的判断。

核心设计——**录制-重放**，不重新调用 LLM：

1. core 每次真实调用都会把完整请求/响应写进 `logs/prompts/chat_log_*.json`；
2. 这些日志里的 system prompt 就是「改造前」的真实产物，直接拿来做 fixture；
3. 评测时把 fixture 的请求喂给**当前代码**的 prompt 组装层，得到「改造后」；
4. 用 grader 对比两者的度量指标。

好处：
- **确定性**：同一份代码跑两次结果一致，可进 CI；
- **零成本**：不调用 LLM，30 份 fixture 秒级出结果；
- **可比**：改造前后用同一套度量代码（`services.agent.prompt_metrics`）。
"""

from .case import EvalCase, load_cases
from .grader import GradeResult, compare_cases, grade_case
from .runner import EvalRunner, run_suite
from .suite import render_report, write_report

__all__ = [
    "EvalCase",
    "load_cases",
    "GradeResult",
    "grade_case",
    "compare_cases",
    "EvalRunner",
    "run_suite",
    "render_report",
    "write_report",
]
