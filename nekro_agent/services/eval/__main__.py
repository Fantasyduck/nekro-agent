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
import sys

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

    if args.json:
        import json

        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_report(report))

    return 0 if report.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
