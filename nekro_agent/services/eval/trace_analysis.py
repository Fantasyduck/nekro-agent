# -*- coding: utf-8 -*-
"""消费 agent trace 做行为级评测（UPGRADE_PLAN 4.2.5）。

与 prompt_metrics 的静态度量互补：
- 静态度量回答「prompt 组装得好不好」（体积/工具数/占比）；
- 本模块回答「agent 跑得对不对」（阶段链路/耗时/token/沙盒行为）。

验收标准（来自规划 4.2）：
- 一次真实对话后，trace 包含 >=5 个 phase 事件，时间戳单调
- trace 能还原「LLM 生成 -> 沙盒执行 -> 工具结果」完整链路
- 关闭 trace 时对 agent 行为零影响
- 落盘开销：单次对话 < 50KB
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class TraceTurn:
    """一次 agent 轮次（一次 run_agent 调用）的链路还原。"""

    chat_key: str = ""
    events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def phases(self) -> List[str]:
        """按顺序的阶段名列表。"""
        return [str(e.get("phase") or e.get("event") or "") for e in self.events]

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def monotonic(self) -> bool:
        """时间戳是否单调不减。"""
        ts = [float(e.get("ts") or 0) for e in self.events]
        return all(ts[i] <= ts[i + 1] for i in range(len(ts) - 1))

    @property
    def duration_ms(self) -> float:
        """本轮总耗时（ms）。"""
        if not self.events:
            return 0.0
        return (float(self.events[-1].get("ts") or 0) - float(self.events[0].get("ts") or 0)) * 1000

    @property
    def has_full_chain(self) -> bool:
        """是否含「LLM 生成 -> 沙盒执行」链路。"""
        ph = self.phases
        return "llm_generating" in ph and "sandbox_running" in ph

    @property
    def has_prompt_metrics(self) -> bool:
        return any((e.get("data") or {}).get("prompt_metrics") for e in self.events)

    def tokens(self) -> Dict[str, int]:
        """汇总本轮 LLM token 用量（若有 llm_response 事件）。"""
        out = {"in": 0, "out": 0, "total": 0}
        for e in self.events:
            d = e.get("data") or {}
            out["in"] += int(d.get("llm_tokens_in") or 0)
            out["out"] += int(d.get("llm_tokens_out") or 0)
            out["total"] += int(d.get("llm_tokens_total") or 0)
        return out

    def sandbox_runs(self) -> List[Dict[str, Any]]:
        """本轮的沙盒执行记录。"""
        return [e.get("data") or {} for e in self.events if e.get("event") == "sandbox_result"]


def load_trace(path: str) -> List[Dict[str, Any]]:
    """读取 JSONL trace 文件。"""
    p = Path(path)
    if not p.exists():
        return []
    out: List[Dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
    return out


def group_turns(events: List[Dict[str, Any]]) -> List[TraceTurn]:
    """把事件流按 chat_key 聚成轮次。

    seq 会随进程重启归零，所以以「prompt_built」作为新一轮的起点，
    同 chat_key 下从上一个起点到下一个起点之间算一轮。
    """
    turns: List[TraceTurn] = []
    cur: Dict[str, TraceTurn] = {}
    last_key = ""
    for ev in events:
        key = str(ev.get("chat_key") or "")
        if not key:
            # 某些埋点（如 openai.py 的 llm_response）没有 chat_key 上下文，
            # 归入最近一个有 chat_key 的轮次，否则会被拆成孤立的「空轮次」
            key = last_key
        else:
            last_key = key
        if ev.get("event") == "prompt_built":
            # 新起点：上一轮收尾
            if key in cur:
                turns.append(cur.pop(key))
            cur[key] = TraceTurn(chat_key=key)
        if key not in cur:
            cur[key] = TraceTurn(chat_key=key)
        cur[key].events.append(ev)
    turns.extend(cur.values())
    return turns


def score_traces(turns: List[TraceTurn]) -> Dict[str, Any]:
    """对还原出的轮次做行为级评分。"""
    if not turns:
        return {"turns": 0, "passed": 0, "failed": 0, "errors": ["没有可分析的 trace 轮次"]}

    errors: List[str] = []
    warnings: List[str] = []
    passed = 0

    for t in turns:
        ok = True
        # 1) >=5 个 phase 事件
        if t.event_count < 5:
            ok = False
            errors.append("%s: 仅 %d 个事件（要求 >=5）" % (t.chat_key[-12:], t.event_count))
        # 2) 时间戳单调
        if not t.monotonic:
            ok = False
            errors.append("%s: 时间戳非单调" % t.chat_key[-12:])
        # 3) 完整链路
        if not t.has_full_chain:
            ok = False
            errors.append("%s: 缺少 LLM->沙盒 链路" % t.chat_key[-12:])
        # 4) prompt 度量在场
        if not t.has_prompt_metrics:
            warnings.append("%s: 无 prompt_metrics 事件" % t.chat_key[-12:])
        if ok:
            passed += 1

    return {
        "turns": len(turns),
        "passed": passed,
        "failed": len(turns) - passed,
        "errors": errors,
        "warnings": warnings,
        "avg_events": round(sum(t.event_count for t in turns) / len(turns), 1),
        "avg_duration_ms": round(sum(t.duration_ms for t in turns) / len(turns), 1),
        "full_chain_ratio": round(sum(1 for t in turns if t.has_full_chain) / len(turns), 3),
        "sandbox_runs": sum(len(t.sandbox_runs()) for t in turns),
    }
