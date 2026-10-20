# -*- coding: utf-8 -*-
'''沙盒方法 docstring 的压缩摘要。

M1 保守档的核心：把注入 prompt 的**完整 docstring**压缩成「首行摘要 + 参数签名」，
完整文档移到沙盒内按需查阅。

为什么保守档要保留参数名：模型必须知道参数叫什么才能正确调用，
只留首行摘要会让它猜参数名。丢的是「参数解释全文 / Returns 描述 / Examples」，
这些是文档性的，不是调用契约。
'''

import re
from typing import List, Tuple

# Args: / Arguments: / 参数: 区块起始
_ARGS_RE = re.compile(r"^\s*(?:Args|Arguments|参数|Arguments?)\s*[:：]\s*$", re.M)
# 下一个大区块起始（Returns / Raises / Examples / Note / Yields ...）
_SECTION_RE = re.compile(r"^\s*(?:Returns?|Raises?|Yields?|Examples?|Notes?|Attributes?|See Also|Warning|Warnings)\b.*$", re.M)


def _first_line(doc: str) -> str:
    """docstring 的首行摘要（去掉空行与前导空白）。"""
    for line in (doc or "").split("\n"):
        t = line.strip()
        if t:
            return t
    return ""


def _param_names(args_block: str) -> List[str]:
    """从 Args 区块里提取参数名与类型。

    支持两种写法：
      name (type): 说明        -> 取 name (type)
      name: 说明              -> 取 name
    """
    out: List[str] = []
    for raw in (args_block or "").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 缩进层级变化通常表示新的大区块，停止
        if not raw.startswith((" ", "\t")):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(([^)]*)\))?\s*[:：]", line)
        if m:
            name = m.group(1)
            typ = (m.group(2) or "").strip()
            out.append(f"{name}({typ})" if typ else name)
            continue
        # 续行（参数说明折行）跳过
    return out


def _args_block(doc: str) -> str:
    """截取 Args 区块正文（到下一个大区块为止）。"""
    m = _ARGS_RE.search(doc or "")
    if not m:
        return ""
    rest = (doc or "")[m.end():]
    nxt = _SECTION_RE.search(rest)
    return rest[: nxt.start()] if nxt else rest


def summarize_doc(doc: str, *, max_summary: int = 120, max_params: int = 12) -> Tuple[str, List[str]]:
    """把 docstring 压缩成 (首行摘要, 参数签名列表)。"""
    d = (doc or "").strip()
    if not d:
        return "", []
    summary = _first_line(d)
    if len(summary) > max_summary:
        summary = summary[: max_summary - 1].rstrip() + "…"
    params = _param_names(_args_block(d))[:max_params]
    return summary, params


def render_compact_method_prompt(name: str, doc: str, *, is_agent: bool = False,
                                     max_summary: int = 120) -> str:
    """渲染压缩后的方法说明（保守档）。

    格式：
      * method_name(arg1, arg2)
        摘要文字
    """
    summary, params = summarize_doc(doc, max_summary=max_summary)
    sig = name + ("(" + ", ".join(params) + ")" if params else "()")
    lines = [f"* {sig}"]
    if summary:
        lines.append(f"  {summary}")
    if is_agent:
        lines.append("  **[AGENT METHOD - STOP AFTER CALL]**")
    return "\n".join(lines)
