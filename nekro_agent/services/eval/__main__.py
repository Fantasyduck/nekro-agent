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
    parser.add_argument("--label", default="", help="本次评测的标签（多期对比时用）")
    parser.add_argument("--history", default="", help="历史 JSON 报告目录，用于多期对比")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出到 stdout")
    args = parser.parse_args(argv)

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
        snap_file.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        print("历史快照已保存: " + str(snap_file))

    if args.html:

        import datetime

        runs = []
        # 历史各期（可选）
        if args.history:
            hist_dir = Path(args.history)
            for hp in sorted(hist_dir.glob("eval_*.json")):
                try:
                    h = json.loads(hp.read_text(encoding="utf-8"))
                except Exception:
                    continue
                # 快照是平铺结构（label/avg_*/tool_freq…），直接作为 run
                if h.get("label"):
                    runs.append(h)
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

    if args.json:

        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_report(report))

    return 0 if report.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
