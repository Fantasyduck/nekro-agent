"""Prompt 组装的可量化指标。

这是 M0 阶段的核心产出：**用同一套代码**度量「改造前」和「改造后」的 prompt，
让「工具目录化能省多少 token」「插件分级能省多少」这类判断有数据支撑，而不是拍脑袋。

度量项：
- `total_chars`      system prompt 总字符数
- `segment_chars`    各段落字符数（policy / persona / runtime / plugins）
- `plugin_ratio`     插件块占比（当前最大头，实测约 51%）
- `method_count`     工具定义数量（实测 77 个）
- `method_chars`     工具定义占用字符数
- `method_ratio`     工具定义占 system prompt 的比例
- `plugin_count`     参与的插件数
- `rule_depth`       关键规则所在深度（0~1，越大越接近尾部 ⇒ 越容易被模型忽略）

**rule_depth** 对应 known research 里反复出现的 lost-in-the-middle 问题：
模型对上下文首尾的信息利用率高于中段。关键规则埋在 50% 深度意味着它很可能被忽略。
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# 工具定义在 prompt 里的形态：`* method_name` 换行 + docstring。
# 注意有两种写法：
#   * angel_remember
#   * angel_recall - **[AGENT METHOD - STOP AFTER CALL]**
# 所以后缀必须允许；同时**只统计 <predefined_methods> 块内**的，
# 否则沙盒已安装包列表（numpy / pandas / matplotlib…）会被误当成工具。
_METHOD_RE = re.compile(r"^\* ([A-Za-z_][A-Za-z0-9_]*)\b", re.M)

# 插件块：<plugin name="..." module_name="..." ...>
_PLUGIN_RE = re.compile(r'<plugin name="([^"]*)" module_name="([^"]*)"', re.M)

# <predefined_methods> ... </predefined_methods> 块
_METHODS_BLOCK_RE = re.compile(r"<predefined_methods>(.*?)</predefined_methods>", re.S)


@dataclass
class PromptMetrics:
    """一次 prompt 组装的度量结果。"""

    total_chars: int = 0
    segment_chars: Dict[str, int] = field(default_factory=dict)
    plugin_chars: int = 0
    plugin_ratio: float = 0.0
    plugin_count: int = 0
    method_count: int = 0
    method_chars: int = 0
    method_ratio: float = 0.0
    injected_prompt_chars: int = 0
    rule_depth: float = 0.0
    method_names: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def measure_prompt(system_prompt: str, *, rule_markers: Optional[List[str]] = None) -> PromptMetrics:
    """度量 system prompt。

`rule_markers` 是「关键规则」的定位串（例如某些必须被模型看到的指令片段）；
命中位置会被换算成相对深度，用于评估 lost-in-the-middle 风险。
"""
    text = system_prompt or ""
    total = len(text)
    m = PromptMetrics(total_chars=total)

    # ---- 段落切分：<plugins> 之前是框架段，之后是插件段
    # 注意：输入也可能**本身就是**插件段（对比时只取这一段来量），
    # 此时没有 <plugins> 包裹标签，要靠 <plugin ...> 块识别，否则会被当成纯框架段。
    plugins_idx = text.find("<plugins>")
    if plugins_idx < 0:
        if "<plugin " in text:
            # 输入本身是插件段：整段都算插件段，框架段为 0
            head, tail = "", text
        else:
            head, tail = text, ""
    else:
        head, tail = text[:plugins_idx], text[plugins_idx:]
    m.segment_chars["framework"] = len(head)
    m.segment_chars["plugins"] = len(tail)
    m.plugin_chars = len(tail)
    m.plugin_ratio = round(len(tail) / total, 4) if total else 0.0

    # ---- 工具定义
    blocks = _METHODS_BLOCK_RE.findall(text)
    method_chars = sum(len(b) for b in blocks)
    names: List[str] = []
    for b in blocks:
        names.extend(_METHOD_RE.findall(b))
    # 不做全文兜底：沙盒「已安装包」列表同样是 `* name` 形态，
    # 全文匹配会把 numpy/pandas 之类算成工具，指标失真。
    m.method_names = names
    m.method_count = len(names)
    m.method_chars = method_chars
    m.method_ratio = round(method_chars / total, 4) if total else 0.0

    # ---- 插件数与注入 prompt 体积
    m.plugin_count = len(_PLUGIN_RE.findall(text))
    injected = 0
    for b in re.findall(r"<injected_prompt>(.*?)</injected_prompt>", text, re.S):
        injected += len(b)
    m.injected_prompt_chars = injected

    # ---- 关键规则深度
    if rule_markers and total:
        positions = []
        for marker in rule_markers:
            idx = text.find(marker)
            if idx >= 0:
                positions.append(idx / total)
        if positions:
            m.rule_depth = round(sum(positions) / len(positions), 4)

    return m


def extract_plugins_segment(system_prompt: str) -> str:
    """从 system prompt 中抽出 <plugins>...</plugins> 段。

    M1 系列的改造全部发生在插件 prompt 组装层（工具定义压缩、插件注入分级），
    框架段与历史段不受影响。对比时两边都只取插件段，才能公平比较。
    """
    text = system_prompt or ""
    i = text.find("<plugins>")
    j = text.rfind("</plugins>")
    if i < 0 or j <= i:
        return ""
    return text[i : j + len("</plugins>")]
