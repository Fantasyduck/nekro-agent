# -*- coding: utf-8 -*-
'''评测报表渲染：生成自包含 HTML（内联 CSS/SVG，零外部依赖）。

设计规范（对齐 data-viz-gen 技能）：
- 语义化配色，不用彩虹色
- 数字用 tabular-nums 保证列对齐
- 图表有标题、单位、图例
- 响应式：宽屏双列，窄屏单列
- 中文字体栈，不依赖网络字体
'''

import html
import math
from typing import Any, Dict, List, Optional

PALETTE = {
    "bg": "#f7f8fb",
    "surface": "#ffffff",
    "ink": "#202b42",
    "ink2": "#52617a",
    "muted": "#738198",
    "grid": "#e4e9f2",
    "border": "#dfe5ef",
    "brand": "#6958df",
    "ok": "#2f9e6e",
    "warn": "#d99a12",
    "bad": "#d9534f",
    "s1": "#3b82f6",
    "s2": "#e8834a",
    "s3": "#8b5cf6",
    "s4": "#10b981",
}

CSS = (
    "*{box-sizing:border-box}"
    "body{margin:0;background:%(bg)s;color:%(ink)s;"
    " font:14px/1.6 -apple-system,BlinkMacSystemFont,\"Segoe UI\",\"PingFang SC\",\"Microsoft YaHei\",sans-serif}"
    ".wrap{max-width:1180px;margin:0 auto;padding:28px 22px 60px}"
    "h1{font-size:23px;margin:0 0 4px;letter-spacing:-.3px}"
    ".sub{color:%(muted)s;font-size:12.5px;margin-bottom:22px}"
    "h2{font-size:15px;margin:0 0 12px;font-weight:650}"
    ".card{background:%(surface)s;border:1px solid %(border)s;border-radius:12px;padding:18px 20px;margin-bottom:16px}"
    ".kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px;margin-bottom:16px}"
    ".kpi{background:%(surface)s;border:1px solid %(border)s;border-radius:12px;padding:14px 16px}"
    ".kpi .k{font-size:11.5px;color:%(muted)s;margin-bottom:5px}"
    ".kpi .v{font-size:25px;font-weight:700;letter-spacing:-.6px;font-variant-numeric:tabular-nums;line-height:1.15}"
    ".kpi .d{font-size:11.5px;margin-top:5px;font-variant-numeric:tabular-nums}"
    ".up{color:%(ok)s}.down{color:%(bad)s}.flat{color:%(muted)s}"
    ".chart{width:100%;height:auto;display:block}"
    ".grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}"
    "@media(max-width:900px){.grid2{grid-template-columns:1fr}}"
    "table{width:100%;border-collapse:collapse;font-size:12.5px}"
    "th,td{padding:7px 9px;text-align:left;border-bottom:1px solid %(grid)s}"
    "th{color:%(muted)s;font-weight:600;font-size:11.5px;white-space:nowrap}"
    "td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}"
    ".tag{display:inline-block;padding:1px 7px;border-radius:20px;font-size:11px;font-weight:600}"
    ".tag.pass{background:rgba(47,158,110,.12);color:%(ok)s}"
    ".tag.fail{background:rgba(217,83,79,.12);color:%(bad)s}"
    ".tag.warn{background:rgba(217,154,18,.14);color:#8a6100}"
    ".empty{color:%(muted)s;padding:18px 0;text-align:center;font-size:12.5px}"
    ".note{font-size:12px;color:%(ink2)s;line-height:1.75;margin-top:10px}"
    ".note b{color:%(ink)s}"
)


def _esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def _fmt(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "-"


def _pct(v: Any, digits: int = 1) -> str:
    try:
        return f"{float(v) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "-"


def _signed(v: Any, digits: int = 1) -> str:
    try:
        return f"{float(v) * 100:+.{digits}f}%"
    except (TypeError, ValueError):
        return "-"


def _kpi_delta(value: Any) -> str:
    """prompt 变小是好事，所以负变化映射为绿色。"""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return "flat"
    if x < -0.001:
        return "up"
    if x > 0.001:
        return "down"
    return "flat"


def _kpi(label: str, value: str, detail: str = "", delta: Optional[str] = None,
         delta_cls: str = "flat") -> str:
    d = ""
    if delta:
        d = '<div class="d %s">%s</div>' % (delta_cls, _esc(delta))
    detail_html = ('<div class="d">' + _esc(detail) + "</div>") if detail else ""
    return '<div class="kpi"><div class="k">%s</div><div class="v">%s</div>%s%s</div>' % (
        _esc(label), _esc(value), detail_html, d
    )

def _bar_rows(categories: List[str], series: List[Dict[str, Any]], *,
               unit: str = "", width: int = 560, row_h: int = 26,
               show_values: bool = True) -> str:
    """分组横向条形图：每行一个类别，多系列并列。"""
    if not categories or not series:
        return '<div class="empty">无数据</div>'
    label_w, value_w = 150, 84
    plot_w = max(120, width - label_w - value_w - 12)
    all_vals = [v for s in series for v in (s.get("values") or [])]
    maxv = (max(all_vals) or 1) or 1
    n = len(series)
    bar_h = max(4, (row_h - 8) // max(1, n))
    out: List[str] = []
    out.append('<svg viewBox="0 0 %d %d" class="chart" role="img">' % (width, len(categories) * row_h + 26))
    lx = label_w
    for s in series:
        color = s.get("color") or PALETTE["s1"]
        out.append('<rect x="%d" y="2" width="9" height="9" rx="2" fill="%s"/>' % (lx, color))
        out.append('<text x="%d" y="11" font-size="11" fill="%s">%s</text>' % (lx + 13, PALETTE["ink2"], _esc(s.get("name"))))
        lx += 26 + len(str(s.get("name"))) * 7
    for ci, cat in enumerate(categories):
        y0 = 22 + ci * row_h
        out.append('<text x="%d" y="%.1f" text-anchor="end" font-size="11.5" fill="%s">%s</text>' % (label_w - 8, y0 + row_h / 2 + 4, PALETTE["ink"], _esc(cat)))
        for si, s in enumerate(series):
            vals = s.get("values") or []
            v = vals[ci] if ci < len(vals) else 0
            color = s.get("color") or PALETTE["s1"]
            try:
                w = max(1.0, (float(v) / maxv) * plot_w)
            except (TypeError, ValueError):
                w = 1.0
            y = y0 + 4 + si * (bar_h + 1)
            out.append('<rect x="%d" y="%.1f" width="%.1f" height="%d" rx="3" fill="%s"/>' % (label_w, y, w, bar_h, color))
            if show_values:
                txt = (f"{v:,}" + unit) if isinstance(v, (int, float)) else str(v)
                out.append('<text x="%.1f" y="%.1f" font-size="10.5" fill="%s" style="font-variant-numeric:tabular-nums">%s</text>' % (label_w + w + 6, y + bar_h - 1, PALETTE["ink2"], _esc(txt)))
    out.append("</svg>")
    return "".join(out)


def _donut(parts: List[Dict[str, Any]], *, size: int = 200, thickness: int = 34) -> str:
    """环形占比图。parts = [{"label": str, "value": float, "color": str}]"""
    total = sum(float(p.get("value") or 0) for p in parts) or 1.0
    cx = cy = size / 2
    r = (size - thickness) / 2
    out: List[str] = []
    out.append('<svg viewBox="0 0 %d %d" class="chart" role="img">' % (size, size))
    ang = -math.pi / 2
    for p in parts:
        v = float(p.get("value") or 0)
        if v <= 0:
            continue
        sweep = v / total * 2 * math.pi
        end = ang + sweep
        large = 1 if sweep > math.pi else 0
        x1, y1 = cx + r * math.cos(ang), cy + r * math.sin(ang)
        x2, y2 = cx + r * math.cos(end), cy + r * math.sin(end)
        out.append('<path d="M %.2f %.2f A %.2f %.2f 0 %d 1 %.2f %.2f" fill="none" stroke="%s" stroke-width="%d"/>' % (x1, y1, r, r, large, x2, y2, p.get("color") or PALETTE["s1"], thickness))
        ang = end
    out.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s"/>' % (cx, cy, r - thickness / 2 - 1, PALETTE["surface"]))
    out.append("</svg>")
    return "".join(out)

def build_report(data: Dict[str, Any]) -> str:
    """生成自包含的 HTML 报表。

    data 结构：
      title / subtitle / generated_at
      runs: [ {label, mode, case_count, passed, failed,
             avg_total_chars, avg_method_count, avg_plugin_ratio, avg_method_ratio,
             avg_framework_chars, avg_plugins_chars, avg_method_chars, tool_freq,
             results: [...]} ]
      notes: [str]
    """
    runs = data.get("runs") or []
    if not runs:
        return "<html><body><p>无数据</p></body></html>"
    latest = runs[-1]
    first = runs[0]
    n_runs = len(runs)
    parts: List[str] = []

    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="zh-CN"><head><meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    parts.append("<title>%s</title>" % _esc(data.get("title") or "评测报表"))
    css = CSS
    for _k, _v in PALETTE.items():
        css = css.replace("%(" + _k + ")s", _v)
    parts.append("<style>%s</style>" % css)
    parts.append('</head><body><div class="wrap">')

    parts.append("<h1>%s</h1>" % _esc(data.get("title") or "Agent Prompt 评测报表"))
    sub = data.get("subtitle") or ""
    gen = data.get("generated_at") or ""
    if sub or gen:
        parts.append('<div class="sub">%s%s</div>' % (_esc(sub), ("　·　生成于 " + _esc(gen)) if gen else ""))

    # ---- KPI ----
    avg_chars = latest.get("avg_total_chars") or 0
    parts.append('<div class="kpis">')
    parts.append(_kpi("用例总数", _fmt(latest.get("case_count")), "最新一次评测"))
    parts.append(_kpi("通过 / 失败", "%d / %d" % (latest.get("passed", 0), latest.get("failed", 0)),
                      "内容断言 + 预算断言", None,
                      "up" if latest.get("failed", 0) == 0 else "down"))
    if n_runs > 1:
        base_chars = first.get("avg_total_chars") or 0
        delta = ((avg_chars - base_chars) / base_chars) if base_chars else 0
        parts.append(_kpi("平均 prompt 体积", _fmt(avg_chars), "system prompt 字符",
                          _signed(delta), _kpi_delta(delta)))
    else:
        parts.append(_kpi("平均 prompt 体积", _fmt(avg_chars), "system prompt 字符"))
    parts.append(_kpi("平均工具数", "%.1f" % (latest.get("avg_method_count") or 0), "可调用沙盒方法"))
    parts.append(_kpi("工具定义占比", _pct(latest.get("avg_method_ratio")), "system prompt 内"))
    parts.append(_kpi("插件段占比", _pct(latest.get("avg_plugin_ratio")), "system prompt 内"))
    parts.append("</div>")

    # ---- 改造效果（首期 vs 末期，插件段口径）----
    if n_runs > 1:
        _seen: Dict[str, Dict[str, Any]] = {}
        for _r in runs:
            _seen[str(_r.get("label") or _r.get("mode"))] = _r
        _uniq = list(_seen.values())
        _base = _uniq[0]
        _last = _uniq[-1]
        _b_seg = _base.get("avg_plugins_segment_chars") or 0
        _c_seg = _last.get("avg_plugins_segment_chars") or 0
        _b_tk = _base.get("avg_method_count") or 0
        _c_tk = _last.get("avg_method_count") or 0
        _b_mc = _base.get("avg_method_chars") or 0
        _c_mc = _last.get("avg_method_chars") or 0
        _d = ((_c_seg - _b_seg) / _b_seg) if _b_seg else 0
        parts.append('<div class="card"><h2>改造效果对比：' + _esc(_base.get("label") or "基线") + " -> " + _esc(_last.get("label") or "当前") + "</h2>")
        parts.append('<div class="grid2">')
        parts.append('<div><div class="note" style="margin-top:0"><b>插件段体积</b>（M1 改造的作用面）</div>')
        parts.append(_bar_rows(
            ["基线", "当前"],
            [{"name": "字符", "values": [_b_seg, _c_seg], "color": PALETTE["s2"]}],
            unit=" 字符", width=520, row_h=30))
        parts.append('<div class="note" style="text-align:center;font-size:19px;font-weight:700;letter-spacing:-.4px" class="%s">%s</div>' % (
            "up" if _d < -0.001 else ("down" if _d > 0.001 else "flat"), _signed(_d)))
        parts.append("</div>")
        parts.append('<div><div class="note" style="margin-top:0"><b>工具定义体积</b>（被压缩的部分）</div>')
        parts.append(_bar_rows(
            ["基线", "当前"],
            [{"name": "字符", "values": [_b_mc, _c_mc], "color": PALETTE["s2"]}],
            unit=" 字符", width=520, row_h=30))
        parts.append('<div class="note">工具数 <b>%s -> %s</b>（%s）</div>' % (
            _fmt(_b_tk), _fmt(_c_tk),
            "一致，未裁掉任何工具" if _b_tk == _c_tk else "有变化，需检查"))
        parts.append("</div>")
        parts.append("</div>")
        parts.append("</div>")

    # ---- 多期对比（插件段口径：M1 改的就是这一段）----
    if n_runs > 1:
        # 去重：同一期只保留最后一次（history 里的旧快照会被当前期覆盖）
        seen: Dict[str, Dict[str, Any]] = {}
        for r in runs:
            seen[str(r.get("label") or r.get("mode"))] = r
        uniq = list(seen.values())
        parts.append('<div class="card"><h2>多期对比：插件段体积（M1 改造的作用面）</h2>')
        labels = [r.get("label") or ("run%d" % (i + 1)) for i, r in enumerate(uniq)]
        series_chars = [{"name": "插件段字符",
                         "values": [r.get("avg_plugins_segment_chars") or 0 for r in uniq],
                         "color": PALETTE["brand"]}]
        parts.append(_bar_rows(labels, series_chars, unit=" 字符", width=1080, row_h=30))
        # 逐期降幅
        if len(uniq) >= 2:
            base = uniq[0].get("avg_plugins_segment_chars") or 0
            rows = []
            for r in uniq[1:]:
                cur = r.get("avg_plugins_segment_chars") or 0
                delta = ((cur - base) / base) if base else 0
                rows.append((r.get("label") or "?", base, cur, delta))
            parts.append('<table style="margin-top:12px"><thead><tr><th>期次</th><th>插件段基线</th><th>插件段当前</th><th>变化</th><th>工具数</th></tr></thead><tbody>')
            for lbl, b0, c0, d0 in rows:
                cls = "up" if d0 < -0.001 else ("down" if d0 > 0.001 else "flat")
                parts.append("<tr><td>%s</td><td class=\"num\">%s</td><td class=\"num\">%s</td>"
                             "<td class=\"num %s\">%s</td><td class=\"num\">%s</td></tr>"
                             % (_esc(lbl), _fmt(b0), _fmt(c0), cls, _signed(d0),
                                _fmt((uniq[uniq.index(r)]["avg_method_count"]) if r in uniq else 0)))
            parts.append("</tbody></table>")
            parts.append('<div class="note">口径说明：对比对象是 <b>&lt;plugins&gt; 段</b>（M1 改造的作用面），两期在<b>同一进程、同一插件状态</b>下渲染，只切换 <code>TOOL_PROMPT_MODE</code>，因此差异只来自工具定义的压缩方式。工具数不变即说明<b>没有工具被裁掉</b>。</div>')
        parts.append("</div>")

    # ---- 结构拆解 + 占比 ----
    fw = latest.get("avg_framework_chars") or 0
    pl = latest.get("avg_plugins_chars") or 0
    mc = latest.get("avg_method_chars") or 0
    other = max(0, pl - mc)
    parts.append('<div class="grid2">')
    parts.append('<div class="card"><h2>prompt 结构拆解（平均字符）</h2>')
    parts.append(_bar_rows(
        ["框架段", "插件段-工具定义", "插件段-其它"],
        [{"name": "字符数", "values": [fw, mc, other], "color": PALETTE["s1"]}],
        unit=" 字符", width=520, row_h=32))
    parts.append('<div class="note">工具定义占 system prompt 的 <b>%s</b>，是单一最大项；'
                 '插件的其它内容仅 <b>%s</b>（%s）。</div>' % (
                     _pct(latest.get("avg_method_ratio")), _fmt(other),
                     _pct((other / avg_chars) if avg_chars else 0)))
    parts.append("</div>")

    parts.append('<div class="card"><h2>体积占比</h2>')
    parts.append(_donut([
        {"label": "框架段", "value": fw, "color": PALETTE["s1"]},
        {"label": "工具定义", "value": mc, "color": PALETTE["s2"]},
        {"label": "插件其它", "value": other, "color": PALETTE["s3"]},
    ]))
    parts.append('<div class="note">')
    for lbl, val, col in (("框架段", fw, PALETTE["s1"]), ("工具定义", mc, PALETTE["s2"]), ("插件其它", other, PALETTE["s3"])):
        parts.append('<div><span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:%s;margin-right:6px"></span>%s <b>%s</b>（%s）</div>'
                     % (col, lbl, _fmt(val), _pct((val / avg_chars) if avg_chars else 0)))
    parts.append("</div></div>")
    parts.append("</div>")

    # ---- 工具频次 ----
    tool_freq = latest.get("tool_freq") or {}
    if tool_freq:
        top = sorted(tool_freq.items(), key=lambda kv: -kv[1])[:20]
        parts.append('<div class="card"><h2>工具注入频次（最新一次，全部用例合计）</h2>')
        parts.append(_bar_rows(
            [t for t, _ in top],
            [{"name": "出现次数", "values": [c for _, c in top], "color": PALETTE["s3"]}],
            unit=" 次", width=1080, row_h=22))
        parts.append('<div class="note">工具总种类 <b>%d</b>。频次 = 该工具出现在多少个用例里；'
                     '接近总用例数说明它与当前对话无关，属于常驻注入的固定成本。</div>' % len(tool_freq))
        parts.append("</div>")

    # ---- 明细表 ----
    results = latest.get("results") or []
    if results:
        parts.append('<div class="card"><h2>用例明细</h2>')
        parts.append("<table><thead><tr><th>用例</th><th>结果</th><th>基线字符</th>"
                     "<th>当前字符</th><th>变化</th><th>工具数</th><th>工具占比</th><th>问题</th></tr></thead><tbody>")
        for r in results:
            ok = r.get("passed")
            errs = r.get("errors") or []
            warns = r.get("warnings") or []
            tag = '<span class="tag pass">PASS</span>' if ok else '<span class="tag fail">FAIL</span>'
            issue = ""
            if errs:
                issue = '<span class="tag fail">' + _esc(str(errs[0])[:70]) + "</span>"
            elif warns:
                issue = '<span class="tag warn">' + _esc(str(warns[0])[:70]) + "</span>"
            d = r.get("chars_delta_pct") or 0
            dcls = "up" if d < -0.001 else ("down" if d > 0.001 else "flat")
            parts.append("<tr><td>%s</td><td>%s</td><td class=\"num\">%s</td><td class=\"num\">%s</td>" % (
                _esc(str(r.get("case_id", ""))[:36]), tag,
                _fmt(r.get("baseline_chars")), _fmt(r.get("current_chars"))))
            parts.append("<td class=\"num %s\">%s</td><td class=\"num\">%s</td><td class=\"num\">%s</td><td>%s</td></tr>" % (
                dcls, _signed(d), _fmt(r.get("method_count")),
                _pct(r.get("method_ratio")), issue))
        parts.append("</tbody></table>")
        parts.append("</div>")

    # ---- 结论 ----
    notes = data.get("notes") or []
    if notes:
        parts.append('<div class="card"><h2>结论</h2>')
        for nline in notes:
            parts.append("<div class=\"note\">%s</div>" % nline)
        parts.append("</div>")

    parts.append("</div></body></html>")
    return "".join(parts)


def write_report(data: Dict[str, Any], path: str) -> str:
    """生成 HTML 报表并写入文件，返回路径。"""
    text = build_report(data)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path
