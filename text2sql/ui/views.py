"""界面用到的纯函数：把智能体结果转换成表格、Altair 图表和工作流图，不依赖 Streamlit 运行时。"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable, Sequence
from typing import Any

import altair as alt
import pandas as pd

NODE_LABELS = {
    "understand": "理解意图",
    "link": "Schema 链接",
    "plan": "规划 SQL",
    "guard": "安全门",
    "execute": "执行",
    "repair": "诊断修复",
    "profile": "结果画像",
    "visualize": "图表推荐",
    "narrate": "生成解读",
    "reflect": "质量检查",
    "finalize": "收尾",
}
STATUS_BADGES = {
    "answered": ("已回答", "green"),
    "declined": ("暂不支持", "orange"),
    "rejected": ("已拒绝", "red"),
    "failed": ("失败", "red"),
}
_VEGA_SPECIAL = re.compile(r"([.\[\]\\])")


def _field(name: str) -> str:
    """Vega-Lite 把字段名里的点和方括号当成嵌套访问，需要转义。"""
    return _VEGA_SPECIAL.sub(r"\\\1", name)


def result_frame(columns: Sequence[str], rows: Sequence[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame([[row.get(c) for c in columns] for row in rows], columns=list(columns))


def csv_bytes(columns: Sequence[str], rows: Sequence[dict[str, Any]]) -> bytes:
    """带 BOM 的 UTF-8，Excel 直接双击打开不乱码。"""
    buffer = io.StringIO()
    result_frame(columns, rows).to_csv(buffer, index=False)
    return buffer.getvalue().encode("utf-8-sig")


def chart_for(
    spec: dict[str, Any] | None, columns: Sequence[str], rows: Sequence[dict[str, Any]]
) -> alt.Chart | None:
    """按智能体推荐的图表规格作图；保留 SQL 给出的行顺序，排序本身是查询语义的一部分。"""
    if not spec or spec.get("type") not in ("bar", "barh", "line") or not rows:
        return None
    x = spec.get("x")
    measures = [m for m in spec.get("y") or [] if m in columns]
    if x not in columns or not measures:
        return None

    frame = result_frame(columns, rows)
    frame[x] = frame[x].astype(str)
    base = alt.Chart(frame)
    title = spec.get("title") or ""
    if title:
        base = base.properties(title=title)

    if spec["type"] == "line":
        axis = alt.X(
            field=_field(x), type="ordinal", sort=None, title=x, axis=alt.Axis(labelAngle=0)
        )
        if len(measures) == 1:
            return base.mark_line(point=True).encode(
                x=axis,
                y=alt.Y(field=_field(measures[0]), type="quantitative", title=measures[0]),
                tooltip=[alt.Tooltip(field=_field(c), title=c) for c in [x, *measures]],
            )
        return (
            base.transform_fold([_field(m) for m in measures], as_=["指标", "数值"])
            .mark_line(point=True)
            .encode(
                x=axis,
                y=alt.Y(field="数值", type="quantitative", title="数值"),
                color=alt.Color(field="指标", type="nominal", title="指标"),
                tooltip=[
                    alt.Tooltip(field=_field(x), title=x),
                    alt.Tooltip(field="指标", type="nominal"),
                    alt.Tooltip(field="数值", type="quantitative"),
                ],
            )
        )

    measure = measures[0]
    category = alt.Tooltip(field=_field(x), title=x)
    value = alt.Tooltip(field=_field(measure), type="quantitative", title=measure)
    if spec["type"] == "barh":
        return base.mark_bar().encode(
            y=alt.Y(
                field=_field(x), type="nominal", sort=None, title=x, axis=alt.Axis(labelLimit=320)
            ),
            x=alt.X(field=_field(measure), type="quantitative", title=measure),
            tooltip=[category, value],
        )
    return base.mark_bar().encode(
        x=alt.X(field=_field(x), type="nominal", sort=None, title=x, axis=alt.Axis(labelAngle=-30)),
        y=alt.Y(field=_field(measure), type="quantitative", title=measure),
        tooltip=[category, value],
    )


def _display_number(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "—" if value is None else str(value)
    if isinstance(value, int) or float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def kpis_for(spec: dict[str, Any] | None, rows: Sequence[dict[str, Any]]) -> list[tuple[str, str]]:
    if not spec or spec.get("type") != "kpi" or not rows:
        return []
    return [(name, _display_number(rows[0].get(name))) for name in spec.get("y") or []]


def detail_text(detail: dict[str, Any] | None) -> str:
    parts = []
    for key, value in (detail or {}).items():
        if isinstance(value, list) and all(isinstance(v, str | int | float) for v in value):
            value = ",".join(str(v) for v in value)
        if isinstance(value, str | int | float) and value != "":
            parts.append(f"{key}={value}")
    return " ".join(parts)


def trace_frame(trace: Iterable[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "节点": NODE_LABELS.get(entry.get("node", ""), entry.get("node", "")),
                "状态": entry.get("status", ""),
                "耗时（ms）": entry.get("ms", 0.0),
                "说明": detail_text(entry.get("detail")),
            }
            for entry in trace
        ],
        columns=["节点", "状态", "耗时（ms）", "说明"],
    )


def steps_frame(steps: Iterable[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "步骤": step.get("index"),
                "工具": step.get("tool"),
                "结果": "成功" if step.get("ok") else "失败",
                "摘要": step.get("summary", ""),
                "耗时（ms）": step.get("latency_ms", 0.0),
            }
            for step in steps
        ],
        columns=["步骤", "工具", "结果", "摘要", "耗时（ms）"],
    )


def workflow_dot(edges: Iterable[tuple[str, str, bool]]) -> str:
    """LangGraph 编译后图的边 → Graphviz DOT；条件边画虚线，和代码里的路由一一对应。"""
    lines = [
        "digraph agent {",
        '  rankdir=LR; bgcolor="transparent";',
        '  node [shape=box, style="rounded,filled", fillcolor="#eef2ff", color="#6366f1", '
        'fontname="sans-serif", fontsize=11];',
        '  edge [color="#94a3b8", arrowsize=0.7];',
        '  "__start__" [label="开始", shape=circle, fillcolor="#e2e8f0", color="#94a3b8"];',
        '  "__end__" [label="结束", shape=doublecircle, fillcolor="#e2e8f0", color="#94a3b8"];',
    ]
    for node, label in NODE_LABELS.items():
        lines.append(f'  "{node}" [label="{label}\\n{node}"];')
    for source, target, conditional in edges:
        style = ' [style=dashed, color="#f59e0b"]' if conditional else ""
        lines.append(f'  "{source}" -> "{target}"{style};')
    lines.append("}")
    return "\n".join(lines)


def graph_edges(compiled_graph: Any) -> list[tuple[str, str, bool]]:
    drawable = compiled_graph.get_graph()
    return [(e.source, e.target, bool(e.conditional)) for e in drawable.edges]
