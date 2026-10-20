"""评测执行器。

两种模式：

- **static**（默认，无需数据库）：直接度量 fixture 里存下来的 prompt。
  用途：对**已保存的 prompt** 做指标体检（体积/工具数/占比/规则深度），
  也能验证 grader 本身是否工作。
- **live**（需要数据库与插件已加载）：用**当前代码**重新渲染一遍 prompt，
  与 fixture 中的基线对比，产出 A/B 报告。
  这是「改造有没有变好」的直接证据。
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .case import EvalCase, load_cases
from .grader import GradeResult, grade_case


class EvalRunner:
    """评测执行器。"""

    def __init__(
        self,
        *,
        log_dir: Optional[str] = None,
        fixture_dir: Optional[str] = None,
        limit: int = 30,
        max_chars: Optional[int] = None,
        max_method_ratio: Optional[float] = None,
        require_tools: bool = True,
    ) -> None:
        self.log_dir = log_dir
        self.fixture_dir = fixture_dir
        self.limit = limit
        self.max_chars = max_chars
        self.max_method_ratio = max_method_ratio
        self.require_tools = require_tools
        self.cases: List[EvalCase] = []
        self.results: List[GradeResult] = []

    # ---------------------------------------------------------------- 装载

    def load(self) -> List[EvalCase]:
        self.cases = load_cases(
            log_dir=self.log_dir,
            fixture_dir=self.fixture_dir,
            limit=self.limit,
        )
        return self.cases

    # ---------------------------------------------------------------- 静态评测

    def run_static(self) -> List[GradeResult]:
        """对每个用例度量其自带 prompt（无需数据库）。"""
        if not self.cases:
            self.load()
        self.results = []
        for case in self.cases:
            self.results.append(
                grade_case(
                    case,
                    case.system_prompt,
                    case.system_prompt,
                    max_chars=self.max_chars,
                    max_method_ratio=self.max_method_ratio,
                    require_tools=self.require_tools,
                )
            )
        return self.results

    # ---------------------------------------------------------------- 实时评测

    async def run_live(self, chat_key: str = "") -> List[GradeResult]:
        """同口径 A/B：同一进程、同一插件状态下，分别用 full / compact 渲染并对比。

        为什么不用 fixture 里的基线：fixture 是历史快照，其插件激活状态、
        工具集合与当前时刻不同（插件会热插拔、状态会随轮次变化），
        直接对比会把「状态差异」误判成「改造收益」。

        正确做法是**同口径**：同一进程内连续渲染两次，只切换 TOOL_PROMPT_MODE，
        这样插件状态完全一致，差异只来自工具定义的压缩方式。

        baseline 仍取 fixture（用于断言「哪些工具必须还在」），
        体积对比则用同口径的 full 渲染结果。
        """
        from nekro_agent.services.agent.prompt_metrics import extract_plugins_segment

        if not self.cases:
            self.load()

        # 同口径渲染：同一进程内先后跑 full 与 compact
        rendered_full = await self._render_current(chat_key, mode="full")
        rendered_compact = await self._render_current(chat_key, mode="compact")

        self.results = []
        for case in self.cases:
            current = rendered_compact.get(case.chat_key, "")
            baseline = rendered_full.get(case.chat_key, "")
            if not current or not baseline:
                # 没有对应会话的实时渲染时，退化为静态度量
                current = case.system_prompt
                baseline = case.system_prompt
            else:
                # render_plugins_prompt 返回的**就是**插件段（不含 <plugins> 外层标签），
                # 所以当前期不能再 extract（会找不到标签而返回空）；
                # 基线是完整 system prompt，才需要 extract 取出插件段。
                baseline = extract_plugins_segment(baseline) or baseline
            # 同口径的 full 渲染结果就是「应该存在的工具集合」
            from nekro_agent.services.agent.prompt_metrics import measure_prompt

            expected = None
            if baseline:
                expected = measure_prompt(baseline).method_names
            self.results.append(
                grade_case(
                    case,
                    current,
                    baseline,
                    max_chars=self.max_chars,
                    max_method_ratio=self.max_method_ratio,
                    require_tools=self.require_tools,
                    expected_tools=expected,
                )
            )
        return self.results
    def _render_from_trace(self, chat_key: str = "", limit: int = 50) -> Dict[str, str]:
        """从 trace 落盘里取「真实运行时」产出的 system prompt。

        live 评测的正确做法：**不自己渲染**，而是读运行时的 trace 事件。
        trace 的 prompt_built 事件是当前代码在真实对话中渲染出的 prompt，
        既避免了适配器/插件的初始化依赖，也保证测的就是线上真实产物。
        """
        from nekro_agent.core.os_env import OsEnv

        path = Path(OsEnv.DATA_DIR) / "observer" / "agent_trace.jsonl"
        if not path.exists():
            return {}
        out: Dict[str, str] = {}
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or "prompt_built" not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    if ev.get("event") != "prompt_built":
                        continue
                    key = str(ev.get("chat_key") or "")
                    if chat_key and key != chat_key:
                        continue
                    metrics = (ev.get("data") or {}).get("prompt_metrics") or {}
                    # trace 只存了度量，不存 prompt 全文；用度量值还原对比
                    out[key] = json.dumps(metrics, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return out
        return out

    async def _render_current(self, chat_key: str, mode: str = "") -> Dict[str, str]:
        """用当前代码重新渲染「插件 prompt 段」，返回 {chat_key: plugins_prompt}。

        为什么只渲染插件段：M1 系列改造全部发生在插件 prompt 组装层
        （工具定义压缩、插件注入分级），框架段与历史段不受影响，
        因此对比插件段即可精确反映改造效果。

        不自己跑完整 agent 流程：那条路需要适配器与 nonebot 运行时，
        独立进程里不可用。插件段渲染只需要数据库与插件，足够且可复现。
        """
        out: Dict[str, str] = {}
        try:
            from nekro_agent.core.database import init_db
            from nekro_agent.models.db_chat_channel import DBChatChannel
            from nekro_agent.schemas.agent_ctx import AgentCtx
            from nekro_agent.services.agent.templates.plugin import render_plugins_prompt
            from nekro_agent.services.plugin.collector import plugin_collector

            await init_db()
            await plugin_collector.init_plugins()

            # 临时切换渲染模式（同一进程内对比 full / compact，插件状态完全一致）
            from nekro_agent.core.config import config as core_config

            _origin_mode = getattr(core_config, "TOOL_PROMPT_MODE", "full")
            if mode:
                try:
                    core_config.TOOL_PROMPT_MODE = mode
                except Exception:  # noqa: BLE001
                    pass

            try:
                channels = await DBChatChannel.all()
                for channel in channels:
                    key = channel.chat_key
                    if chat_key and key != chat_key:
                        continue
                    ctx = AgentCtx.create_by_db_chat_channel(db_chat_channel=channel)
                    rendered = await render_plugins_prompt(
                        plugin_collector.get_all_active_plugins(),
                        ctx,
                        activation_enabled=True,
                    )
                    out[key] = rendered.system_prompt
            finally:
                # 恢复原模式，避免污染后续渲染
                try:
                    core_config.TOOL_PROMPT_MODE = _origin_mode
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            raise RuntimeError(f"实时渲染失败: {type(e).__name__}: {e}") from e
        return out

def run_suite(
    *,
    log_dir: Optional[str] = None,
    fixture_dir: Optional[str] = None,
    limit: int = 30,
    live: bool = False,
    chat_key: str = "",
    max_chars: Optional[int] = None,
    max_method_ratio: Optional[float] = None,
    require_tools: bool = True,
    out_path: Optional[str] = None,
) -> Dict[str, Any]:
    """跑一次评测，返回报告数据。"""
    runner = EvalRunner(
        log_dir=log_dir,
        fixture_dir=fixture_dir,
        limit=limit,
        max_chars=max_chars,
        max_method_ratio=max_method_ratio,
        require_tools=require_tools,
    )
    if live:
        results = asyncio.run(runner.run_live(chat_key))
    else:
        results = runner.run_static()

    # 补充结构统计（供可视化报表使用）

    fw_sum = pl_sum = mc_sum = 0.0
    method_sum = 0.0
    tool_freq: Dict[str, int] = {}
    for r in results:
        b = r.baseline
        if not b:
            continue
        fw_sum += b.segment_chars.get("framework", 0)
        pl_sum += b.segment_chars.get("plugins", 0)
        mc_sum += b.method_chars
        method_sum += b.method_count
        for name in b.method_names:
            tool_freq[name] = tool_freq.get(name, 0) + 1
    n = max(1, len(results))

    report = {
        "mode": "live" if live else "static",
        "case_count": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
        "avg_total_chars": round(sum(r.current.total_chars for r in results if r.current) / n, 1),
        # 插件段与完整 prompt 分开统计：M1 改的是插件段，对比必须同口径
        "avg_plugins_segment_chars": round(sum(r.current.segment_chars.get("plugins", 0) for r in results if r.current) / n, 1),
        "baseline_plugins_segment_chars": round(sum(r.baseline.segment_chars.get("plugins", 0) for r in results if r.baseline) / n, 1),
        "avg_method_count": round(method_sum / n, 2),
        "avg_framework_chars": round(fw_sum / n, 1),
        "avg_plugins_chars": round(pl_sum / n, 1),
        "avg_method_chars": round(mc_sum / n, 1),
        "avg_plugin_ratio": round(sum((r.current.plugin_ratio if r.current else 0) for r in results) / n, 4),
        "avg_method_ratio": round(sum((r.current.method_ratio if r.current else 0) for r in results) / n, 4),
        "tool_freq": tool_freq,
        "results": [
            {
                "case_id": r.case_id,
                "passed": r.passed,
                "errors": r.errors,
                "warnings": r.warnings,
                "baseline_chars": r.baseline.total_chars if r.baseline else 0,
                "current_chars": r.current.total_chars if r.current else 0,
                "chars_delta": r.chars_delta,
                "chars_delta_pct": r.chars_delta_pct,
                "method_count": r.current.method_count if r.current else 0,
                "method_ratio": r.current.method_ratio if r.current else 0,
                "plugin_ratio": r.current.plugin_ratio if r.current else 0,
                "rule_depth": r.current.rule_depth if r.current else 0,
            }
            for r in results
        ],
    }
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
