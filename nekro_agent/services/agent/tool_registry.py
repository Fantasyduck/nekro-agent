# -*- coding: utf-8 -*-
"""工具目录化（M1 标准档 / 渐进披露）。

把工具定义从 system prompt 移到沙盒内的可浏览目录，模型需要时自己 cat。

背景：实测 39 个工具定义占 system prompt 的 39.4%（约 11.6k 字符），
而其中绝大多数在单轮对话里根本不会被调用。这正是 Anthropic
《Code execution with MCP》提出的渐进披露（progressive disclosure）。

设计要点：
- **索引必须含全部工具名**。踩过的坑：提示词里写了不存在的工具名，模型就会去调。
  索引里列全，模型不会幻觉出不存在的工具。
- 每个工具一个 markdown 文件，含完整签名 + 参数说明 + 返回值，与原 docstring 等价。
- 常驻工具（TOOL_REGISTRY_ALWAYS_LIST）仍全量注入 system prompt，
  避免「发条消息还要先查文档」。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 从完整 docstring 里提取参数说明（name / type / desc）
_PARAM_LINE_RE = re.compile(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(([^)]*)\))?\s*[:：]\s*(.*)$")


@dataclass
class ToolDoc:
    """单个工具的文档。"""

    name: str
    plugin_name: str = ""
    module_name: str = ""
    method_type: str = "tool"
    summary: str = ""
    doc: str = ""
    args: List[Dict[str, str]] = field(default_factory=list)

    def to_markdown(self) -> str:
        """渲染成 markdown 文档（模型 cat 时看到的内容）。"""
        lines = ["# " + self.name, ""]
        if self.summary:
            lines.append(self.summary)
            lines.append("")
        lines.append("- 所属插件：%s (`%s`)" % (self.plugin_name or "-", self.module_name or "-"))
        lines.append("- 方法类型：%s" % self.method_type)
        if self.method_type in ("agent", "multimodal_agent"):
            lines.append("- **注意**：这是 AGENT 方法，调用后本轮停止，系统会把返回值推给你。")
        lines.append("")
        if self.args:
            lines.append("## 参数")
            lines.append("")
            for a in self.args:
                typ = (" (%s)" % a["type"]) if a.get("type") else ""
                desc = (" —— " + a["desc"]) if a.get("desc") else ""
                lines.append("- `%s`%s%s" % (a["name"], typ, desc))
            lines.append("")
        if self.doc:
            lines.append("## 完整说明")
            lines.append("")
            lines.append("```")
            lines.append(self.doc.strip())
            lines.append("```")
            lines.append("")
        lines.append("## 调用")
        lines.append("")
        sig = ", ".join(a["name"] for a in self.args) if self.args else ""
        lines.append("```python")
        lines.append("ret = %s(%s)" % (self.name, sig))
        lines.append("```")
        return "\n".join(lines)


def parse_args(doc: str) -> List[Dict[str, str]]:
    """从 docstring 的 Args 区块提取参数名/类型/说明。"""
    out: List[Dict[str, str]] = []
    if not doc:
        return out
    in_args = False
    for raw in doc.split("\n"):
        stripped = raw.strip()
        if re.match(r"^(Args|Arguments|参数)\s*[:：]?$", stripped):
            in_args = True
            continue
        if not in_args:
            continue
        if re.match(r"^(Returns?|Raises?|Yields?|Examples?|Notes?|Attributes?)\b", stripped):
            break
        # 续行（更深缩进、无 name:）并入上一条描述
        m = _PARAM_LINE_RE.match(raw)
        if m:
            out.append({
                "name": m.group(2),
                "type": (m.group(3) or "").strip(),
                "desc": m.group(4).strip(),
            })
        elif out and stripped:
            out[-1]["desc"] = (out[-1]["desc"] + " " + stripped).strip()
    return out


def _first_line(doc: str) -> str:
    for line in (doc or "").split("\n"):
        t = line.strip()
        if t:
            return t
    return ""


def build_registry(plugins: List[Any], always_list: Optional[List[str]] = None) -> List[ToolDoc]:
    """从插件列表收集工具文档。

    参数：
        plugins: NekroPlugin 列表
        always_list: 需要常驻 system prompt 的工具名（不进目录索引的「按需」部分）
    """
    always = {str(x).strip() for x in (always_list or []) if str(x).strip()}
    docs: List[ToolDoc] = []
    for plugin in plugins:
        for method in getattr(plugin, "sandbox_methods", []) or []:
            name = getattr(method.func, "__name__", "")
            if not name or name in always:
                continue
            doc_text = (method.func.__doc__ or "").strip()
            if not doc_text:
                continue
            mtype = getattr(getattr(method, "method_type", None), "value", "tool")
            docs.append(
                ToolDoc(
                    name=name,
                    plugin_name=str(getattr(plugin, "name", "") or ""),
                    module_name=str(getattr(plugin, "module_name", "") or ""),
                    method_type=str(mtype),
                    summary=_first_line(doc_text),
                    doc=doc_text,
                    args=parse_args(doc_text),
                ),
            )
    docs.sort(key=lambda d: (d.module_name, d.name))
    return docs


def render_index(docs: List[ToolDoc], *, max_summary: int = 28) -> str:
    """渲染索引（放 system prompt 里的那段）。

    体积预算：索引是**常驻成本**，必须足够小；摘要细节放文件里按需读。
    硬要求：**必须含全部工具名**，否则模型会幻觉出不存在的工具。
    """
    lines = [
        "共 %d 个工具，完整文档在沙盒 /app/tools/（index.md + by-plugin/<插件>/<工具>.md）。" % len(docs),
        "看清单：print(open('/app/tools/index.md').read()); exit(9)",
        "看详情：print(open('/app/tools/by-plugin/<插件>/<工具>.md').read()); exit(9)",
        "TOOL 结果或文档需要交回模型分析时，用 print(result) 后接 exit(9)；单独 print 不会触发续答。",
        "续答后用发送工具回应原请求；已发送的内容不要重复，已完成的任务不必 exit(9)。",
        "**只调用下列工具名，不确定参数先看文档，不要猜。**",
        "",
    ]
    by_mod: Dict[str, List[ToolDoc]] = {}
    for d in docs:
        by_mod.setdefault(d.module_name or "_unnamed", []).append(d)
    for mod, items in sorted(by_mod.items()):
        # 名词清单用逗号分隔，最省字符；摘要只在很短时才带上
        names = ", ".join(d.name for d in items)
        lines.append("- %s: %s" % (mod, names))
    return "\n".join(lines)

def build_files(docs: List[ToolDoc]) -> Dict[str, str]:
    """生成要写入沙盒的文件树：{相对路径: 内容}。"""
    files: Dict[str, str] = {}
    index_entries = []
    for d in docs:
        mod = d.module_name or "_unnamed"
        rel = "by-plugin/%s/%s.md" % (mod, d.name)
        files[rel] = d.to_markdown()
        index_entries.append({
            "name": d.name,
            "plugin": d.plugin_name,
            "module": d.module_name,
            "type": d.method_type,
            "summary": d.summary,
            "path": "/app/tools/" + rel,
        })
    files["index.json"] = json.dumps(index_entries, ensure_ascii=False, indent=2)
    # index.md 给模型人读
    lines = ["# 工具索引", "", "共 %d 个工具。" % len(docs), ""]
    by_mod: Dict[str, List[Dict[str, Any]]] = {}
    for e in index_entries:
        by_mod.setdefault(e["module"] or "_unnamed", []).append(e)
    for mod, items in sorted(by_mod.items()):
        lines.append("## " + mod + "（" + (items[0]["plugin"] or "-") + "）")
        for e in items:
            lines.append("- `%s` —— %s" % (e["name"], e["summary"] or ""))
            lines.append("  文档：`%s`" % e["path"])
        lines.append("")
    files["index.md"] = "\n".join(lines)
    return files
