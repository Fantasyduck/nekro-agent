"""评测用例的表示与加载。

用例来源有两种：

1. **日志 fixture**（`logs/prompts/chat_log_*.json`）：core 真实调用留下的完整请求/响应，
   含有「改造前」的真实 system prompt，是主力来源；
2. **手工 fixture**（`fixtures/*.json`）：显式指定 system prompt 与期望断言，用于
   构造「必须保留某段规则」这类针对性用例。
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class EvalCase:
    """一个评测用例。

    `system_prompt` 是被测对象（改造前/后都从这里取）；
    `expect_tools` / `expect_markers` 是「不能丢」的内容断言。
    """

    case_id: str = ""
    source: str = ""
    chat_key: str = ""
    tags: List[str] = field(default_factory=list)
    system_prompt: str = ""
    model: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)
    # 断言：必须仍然存在的工具名
    expect_tools: List[str] = field(default_factory=list)
    # 断言：必须仍然存在的文本片段（关键规则）
    expect_markers: List[str] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return len(self.system_prompt or "")


def _from_chat_log(path: Path) -> Optional[EvalCase]:
    """把一份 chat_log 转成评测用例。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    request = data.get("request") or {}
    messages = request.get("messages") or []
    if not messages:
        return None
    first = messages[0] if messages else {}
    if str(first.get("role")) != "system":
        return None
    content = first.get("content")
    if isinstance(content, list):
        content = "\n".join(
            str(b.get("text") or "") for b in content if isinstance(b, dict)
        )
    system_prompt = str(content or "")
    if not system_prompt:
        return None

    # 工具断言：该日志里出现过的工具名，改动后必须仍然可见
    from nekro_agent.services.agent.prompt_metrics import _METHOD_RE, _METHODS_BLOCK_RE

    tool_names: List[str] = []
    for block in _METHODS_BLOCK_RE.findall(system_prompt):
        tool_names.extend(_METHOD_RE.findall(block))
    tool_names = list(dict.fromkeys(tool_names))

    # 会话标识：core 模板会在最后一条 user 消息写入权威的 chat_key
    chat_key = ""
    for msg in messages:
        if str(msg.get("role")) != "user":
            continue
        text = msg.get("content")
        if isinstance(text, list):
            text = "\n".join(str(b.get("text") or "") for b in text if isinstance(b, dict))
        found = re.search(r"Current Chat Key:\s*([^\s<>]+)", str(text or ""), re.I)
        if found:
            chat_key = found.group(1)
            break

    return EvalCase(
        case_id=path.stem,
        source=str(path),
        chat_key=chat_key,
        tags=["chat_log"],
        system_prompt=system_prompt,
        model=str(data.get("model") or ""),
        metrics=dict(data.get("metrics") or {}),
        expect_tools=tool_names,
    )


def _from_fixture(path: Path) -> Optional[EvalCase]:
    """加载手工构造的 fixture。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return EvalCase(
        case_id=str(data.get("case_id") or path.stem),
        source=str(path),
        tags=list(data.get("tags") or []),
        system_prompt=str(data.get("system_prompt") or ""),
        model=str(data.get("model") or ""),
        expect_tools=list(data.get("expect_tools") or []),
        expect_markers=list(data.get("expect_markers") or []),
    )


def load_cases(
    log_dir: Optional[str] = None,
    fixture_dir: Optional[str] = None,
    limit: int = 30,
    newest_first: bool = True,
) -> List[EvalCase]:
    """加载评测用例。

    `log_dir` 默认取 core 的提示词日志目录（真实调用产物）；
    `fixture_dir` 默认取 `services/eval/fixtures`（手工用例）。
    """
    from nekro_agent.core.os_env import PROMPT_LOG_DIR

    cases: List[EvalCase] = []

    if fixture_dir:
        fdir = Path(fixture_dir)
    else:
        # 允许用配置覆盖；留空则用包内 fixtures
        try:
            from nekro_agent.core.config import config as _cfg

            _conf_dir = str(getattr(_cfg, "EVAL_FIXTURE_DIR", "") or "").strip()
        except Exception:  # noqa: BLE001
            _conf_dir = ""
        fdir = Path(_conf_dir) if _conf_dir else Path(__file__).parent / "fixtures"
    if fdir.exists():
        for p in sorted(fdir.glob("*.json")):
            case = _from_fixture(p)
            if case:
                cases.append(case)

    if log_dir:
        ldir = Path(log_dir)
    else:
        ldir = Path(PROMPT_LOG_DIR)
    if ldir.exists():
        logs = sorted(ldir.glob("chat_log_*.json"), key=lambda p: p.stat().st_mtime)
        if newest_first:
            logs = list(reversed(logs))
        for p in logs:
            if len(cases) >= max(1, int(limit)):
                break
            case = _from_chat_log(p)
            if case:
                cases.append(case)

    return cases
