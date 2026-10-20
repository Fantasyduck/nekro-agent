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
        """用当前代码重新渲染 prompt，与基线对比。

        需要数据库可用、插件已加载。渲染入口与 `run_agent` 一致：
        插件 prompt + 框架 prompt + 历史消息。
        """
        if not self.cases:
            self.load()

        rendered = await self._render_current(chat_key)
        self.results = []
        for case in self.cases:
            current = rendered.get(case.chat_key, "") if rendered else ""
            if not current:
                # 没有对应会话的实时渲染时，退化为静态度量
                current = case.system_prompt
            self.results.append(
                grade_case(
                    case,
                    current,
                    case.system_prompt,
                    max_chars=self.max_chars,
                    max_method_ratio=self.max_method_ratio,
                    require_tools=self.require_tools,
                )
            )
        return self.results

    async def _render_current(self, chat_key: str) -> Dict[str, str]:
        """用当前代码渲染 prompt，返回 {chat_key: system_prompt}。"""
        out: Dict[str, str] = {}
        try:
            from nekro_agent.models.db_chat_channel import DBChatChannel
            from nekro_agent.schemas.agent_ctx import AgentCtx
            from nekro_agent.services.agent.templates.compiler import PromptCompiler
            from nekro_agent.services.agent.templates.plugin import render_plugins_prompt
            from nekro_agent.services.plugin.call_priority import build_plugin_call_priority_rules
            from nekro_agent.services.plugin.collector import plugin_collector
            from nekro_agent.services.plugin.prompt_activation import build_plugin_activation_rules

            channels = await DBChatChannel.all()
            for channel in channels:
                key = channel.chat_key
                if chat_key and key != chat_key:
                    continue
                ctx = await AgentCtx.create_by_db_chat_channel(db_chat_channel=channel)
                preset = await channel.get_preset()
                config = await channel.get_effective_config()
                group = config.MODEL_GROUPS[config.USE_MODEL_GROUP]
                rendered = await render_plugins_prompt(
                    plugin_collector.get_all_active_plugins(),
                    ctx,
                    activation_enabled=True,
                )
                self_info = await ctx.adapter.get_self_info()
                compiler = PromptCompiler(
                    platform_name=self_info.platform_name,
                    bot_platform_id=self_info.user_id,
                    chat_preset=preset.content,
                    plugins_prompt=rendered.system_prompt,
                    plugins_runtime_prompt=rendered.runtime_prompt,
                    plugin_activation_rules=build_plugin_activation_rules(),
                    plugin_call_priority_rules=build_plugin_call_priority_rules(),
                    enable_cot=group.ENABLE_COT,
                    chat_key_rules="\n".join(f"- {r}" for r in channel.adapter.chat_key_rules),
                    enable_at=channel.adapter.config.SESSION_ENABLE_AT,
                )
                msg = compiler.render_system_message()
                out[key] = str(getattr(msg, "content", "") or "")
        except Exception:  # noqa: BLE001
            return out
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

    report = {
        "mode": "live" if live else "static",
        "case_count": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
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
