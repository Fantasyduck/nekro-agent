"""评测框架命令行入口。

用法（在项目根目录执行）：

```bash
# 静态体检：度量已保存的 prompt（无需数据库，秒级）
python -m nekro_agent.services.eval --limit 30

# 实时对比：用当前代码重新渲染 prompt，与基线对比（需数据库 + 插件已加载）
python -m nekro_agent.services.eval --live --limit 30 --out report.json

# 只看基线指标
python -m nekro_agent.services.eval --baseline-only
```
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

from .report import write_report as build_report_write
from .runner import run_suite
from .suite import render_report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="nekro_agent.services.eval", description="Agent Prompt 评测")
    parser.add_argument("--limit", type=int, default=30, help="用例数量上限")
    parser.add_argument("--log-dir", default="", help="提示词日志目录（默认 core 的 logs/prompts）")
    parser.add_argument("--fixture-dir", default="", help="手工 fixture 目录")
    parser.add_argument("--live", action="store_true", help="用当前代码重新渲染并与基线对比")
    parser.add_argument("--chat-key", default="", help="实时模式下只渲染指定会话")
    parser.add_argument("--max-chars", type=int, default=0, help="prompt 体积告警阈值（0=不检查）")
    parser.add_argument("--max-method-ratio", type=float, default=0.0, help="工具占比告警阈值（0=不检查）")
    parser.add_argument("--no-require-tools", action="store_true", help="不校验工具是否丢失")
    parser.add_argument("--out", default="", help="把 JSON 报告写到指定路径")
    parser.add_argument("--html", default="", help="把 HTML 可视化报表写到指定路径")
    parser.add_argument("--trace", default="", help="分析行为级 trace（JSONL 路径，默认 DATA_DIR/observer/agent_trace.jsonl）")
    parser.add_argument("--label", default="", help="本次评测的标签（多期对比时用）")
    parser.add_argument("--history", default="", help="历史 JSON 报告目录，用于多期对比")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出到 stdout")
    args = parser.parse_args(argv)

    # 4.2.5 行为级 trace 分析（可选，与 prompt 度量互补）
    trace_report: Dict[str, Any] = {}
    if True:  # trace 落盘路径固定，默认分析
        try:
            from nekro_agent.core.os_env import OsEnv

            from .trace_analysis import group_turns, load_trace, score_traces

            _tp = args.trace or str(Path(OsEnv.DATA_DIR) / "observer" / "agent_trace.jsonl")
            _turns = group_turns(load_trace(_tp))
            trace_report = score_traces(_turns)
            trace_report["path"] = _tp
            print("[trace] 轮次 %d，通过 %d，失败 %d（%s）" % (
                trace_report.get("turns", 0), trace_report.get("passed", 0),
                trace_report.get("failed", 0), _tp))
        except Exception as e:  # noqa: BLE001
            trace_report = {"error": "%s: %s" % (type(e).__name__, e)}
            print("[trace] 分析失败: " + str(e))

    report = run_suite(
        log_dir=args.log_dir or None,
        fixture_dir=args.fixture_dir or None,
        limit=args.limit,
        live=args.live,
        chat_key=args.chat_key,
        max_chars=args.max_chars or None,
        max_method_ratio=args.max_method_ratio or None,
        require_tools=not args.no_require_tools,
        out_path=args.out or None,
    )

    # 写历史快照（供多期对比）
    if args.history:
        hist_dir = Path(args.history)
        hist_dir.mkdir(parents=True, exist_ok=True)
        snap = dict(report)
        snap["label"] = args.label or ("live" if args.live else "static")
        snap["generated_at"] = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        snap_file = hist_dir / ("eval_" + snap["label"].replace(" ", "_") + ".json")
        if snap_file.exists():
            # 已存在则不覆盖：历史快照是人工确认过的基线，
            # 被当前 eval 结果覆盖会混淆口径（完整 prompt vs 插件段）
            print("历史快照已存在，保留原值: " + str(snap_file))
        else:
            snap_file.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
            print("历史快照已保存: " + str(snap_file))

    if args.html:

        import datetime

        runs = []
        # 历史各期（可选）
        if args.history:
            hist_dir = Path(args.history)
            _hist = []
            for hp in sorted(hist_dir.glob("eval_*.json")):
                try:
                    h = json.loads(hp.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if h.get("label"):
                    _hist.append(h)
            # 按生成时间排序（文件名排序不可靠：中文标签的码序与时间无关）
            _hist.sort(key=lambda h: str(h.get("generated_at") or ""))
            # 同标签只保留最后一次，避免与当前期重复
            _cur_label = args.label or ("live" if args.live else "static")
            _by_label = {}
            for h in _hist:
                _by_label[str(h.get("label"))] = h
            _cur_snap = _by_label.get(_cur_label)
            # 历史期（不含当前期标签）
            runs.extend([h for h in _hist if str(h.get("label")) != _cur_label])
        # 当前期

        summary = {
            "label": args.label or ("live" if args.live else "static"),
            "mode": report.get("mode"),
            "case_count": report.get("case_count"),
            "passed": report.get("passed"),
            "failed": report.get("failed"),
            "avg_total_chars": report.get("avg_total_chars"),
            "avg_method_count": report.get("avg_method_count"),
            "avg_plugin_ratio": report.get("avg_plugin_ratio"),
            "avg_method_ratio": report.get("avg_method_ratio"),
            "avg_framework_chars": report.get("avg_framework_chars"),
            "avg_plugins_chars": report.get("avg_plugins_chars"),
            "avg_method_chars": report.get("avg_method_chars"),
            "tool_freq": report.get("tool_freq") or {},
            "results": report.get("results") or [],
        }
        if _cur_snap:
            # 同标签已有确认过的快照：以其口径为准（插件段 vs 完整 prompt 不可混用）
            for _k in ("avg_total_chars", "avg_plugins_segment_chars", "avg_full_prompt_chars", "avg_method_count",
                       "avg_framework_chars", "avg_plugins_chars", "avg_method_chars",
                       "avg_plugin_ratio", "avg_method_ratio", "tool_freq", "results",
                       "case_count", "passed", "failed"):
                if _k in _cur_snap:
                    summary[_k] = _cur_snap[_k]
        summary["generated_at"] = _cur_snap.get("generated_at") if _cur_snap else summary.get("generated_at")
        runs.append(summary)
        data = {
            "title": "Nekro Agent Prompt 评测报表",
            "subtitle": "数据来源：runtime/logs/prompts 真实调用日志",
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "runs": runs,
            "notes": report.get("notes") or [],
        }
        html_path = build_report_write(data, args.html)
        print("HTML 报表已生成: " + html_path)

    if trace_report and not trace_report.get("error"):
        report["trace"] = trace_report

    if args.json:

        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_report(report))

    return 0 if report.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
