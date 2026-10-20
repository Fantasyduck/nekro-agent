"""评测报告的渲染。

输出 Markdown 表格，人可直接阅读；也支持输出 JSON 供 CI 消费。
"""

from typing import Any, Dict, List


def _pct(v: Any) -> str:
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def render_report(report: Dict[str, Any]) -> str:
    """把 run_suite 的结果渲染成 Markdown。"""
    results: List[Dict[str, Any]] = report.get("results") or []
    lines: List[str] = []
    mode = report.get("mode", "static")
    total = report.get("case_count", 0)
    passed = report.get("passed", 0)
    failed = report.get("failed", 0)

    lines.append("# Agent Prompt 评测报告（%s）" % mode)
    lines.append("")
    lines.append("- 用例数：%s　通过：%s　失败：%s" % (total, passed, failed))
    lines.append("")

    if results:
        n = len(results)
        base_avg = sum(r.get("baseline_chars", 0) for r in results) / n
        cur_avg = sum(r.get("current_chars", 0) for r in results) / n
        delta_pct = ((cur_avg - base_avg) / base_avg * 100) if base_avg else 0.0
        avg_methods = sum(r.get("method_count", 0) for r in results) / n
        avg_plugin_ratio = sum(r.get("plugin_ratio", 0) for r in results) / n
        avg_method_ratio = sum(r.get("method_ratio", 0) for r in results) / n
        lines.append("- 平均体积：基线 %s → 当前 %s 字符（%+.1f%%）" % (f"{base_avg:,.0f}", f"{cur_avg:,.0f}", delta_pct))
        lines.append("- 平均工具数：%.1f　插件占比 %s　工具占比 %s" % (avg_methods, _pct(avg_plugin_ratio), _pct(avg_method_ratio)))
        lines.append("")
        lines.append("| 用例 | 结果 | 基线字符 | 当前字符 | 变化 | 工具数 | 插件占比 | 工具占比 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in results:
            mark = "PASS" if r.get("passed") else "FAIL"
            lines.append(
                "| %s | %s | %s | %s | %+.1f%% | %s | %s | %s |" % (
                    str(r.get("case_id", ""))[:34],
                    mark,
                    f"{r.get('baseline_chars', 0):,}",
                    f"{r.get('current_chars', 0):,}",
                    r.get("chars_delta_pct", 0) * 100,
                    r.get("method_count", 0),
                    _pct(r.get("plugin_ratio", 0)),
                    _pct(r.get("method_ratio", 0)),
                )
            )
        for r in results:
            for e in (r.get("errors") or []):
                lines.append("")
                lines.append("- FAIL `%s`: %s" % (r.get("case_id", ""), e))
            for w in (r.get("warnings") or []):
                lines.append("")
                lines.append("- WARN `%s`: %s" % (r.get("case_id", ""), w))
        lines.append("")
    else:
        lines.append("")
        lines.append("没有可用用例（提示词日志目录为空，或未开启 SAVE_PROMPTS_LOG）")
        lines.append("")
    return "\n".join(lines)


def write_report(report: Dict[str, Any], path: str) -> None:
    """把报告写到文件（JSON）。"""
    import json

    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
