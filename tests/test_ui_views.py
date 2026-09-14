"""界面的纯函数部分：图表规格、表格、轨迹与工作流图，不依赖 Streamlit 运行时。"""

from __future__ import annotations

from text2sql.agent.graph import NODE_ORDER
from text2sql.ui.views import (
    NODE_LABELS,
    chart_for,
    csv_bytes,
    kpis_for,
    result_frame,
    steps_frame,
    trace_frame,
    workflow_dot,
)

COLUMNS = ["城市", "企业数量"]
ROWS = [{"城市": "深圳市", "企业数量": 80}, {"城市": "广州市", "企业数量": 120}]


def test_bar_chart_keeps_sql_row_order():
    spec = chart_for({"type": "bar", "x": "城市", "y": ["企业数量"]}, COLUMNS, ROWS).to_dict()

    assert spec["mark"]["type"] == "bar"
    assert spec["encoding"]["x"]["field"] == "城市"
    assert spec["encoding"]["x"]["sort"] is None
    assert spec["encoding"]["y"]["field"] == "企业数量"


def test_horizontal_bar_puts_categories_on_y_axis():
    encoding = chart_for({"type": "barh", "x": "城市", "y": ["企业数量"]}, COLUMNS, ROWS).to_dict()[
        "encoding"
    ]

    assert encoding["y"]["field"] == "城市"
    assert encoding["y"]["sort"] is None
    assert encoding["x"]["field"] == "企业数量"


def test_line_chart_uses_ordinal_time_axis_and_folds_multiple_measures():
    rows = [{"年份": y, "融资事件数": y - 2000, "企业数量": y - 2010} for y in (2024, 2025, 2026)]

    spec = chart_for(
        {"type": "line", "x": "年份", "y": ["融资事件数", "企业数量"]},
        ["年份", "融资事件数", "企业数量"],
        rows,
    ).to_dict()

    assert spec["encoding"]["x"]["type"] == "ordinal"
    assert spec["transform"][0]["fold"] == ["融资事件数", "企业数量"]
    assert spec["encoding"]["color"]["field"] == "指标"


def test_field_names_with_vega_special_characters_are_escaped():
    rows = [{"a.b": "x", "n[1]": 1}]

    encoding = chart_for(
        {"type": "bar", "x": "a.b", "y": ["n[1]"]}, ["a.b", "n[1]"], rows
    ).to_dict()["encoding"]

    assert encoding["x"]["field"] == "a\\.b"
    assert encoding["y"]["field"] == "n\\[1\\]"


def test_kpi_and_missing_specs_do_not_produce_charts():
    assert chart_for({"type": "kpi", "x": None, "y": ["企业数量"]}, COLUMNS, ROWS[:1]) is None
    assert chart_for(None, COLUMNS, ROWS) is None
    assert chart_for({"type": "bar", "x": "不存在", "y": ["企业数量"]}, COLUMNS, ROWS) is None


def test_kpis_format_numbers_for_display():
    spec = {"type": "kpi", "x": None, "y": ["企业数量", "平均注册资本（万元）"]}
    rows = [{"企业数量": 4656, "平均注册资本（万元）": 1016.654}]

    assert kpis_for(spec, rows) == [("企业数量", "4,656"), ("平均注册资本（万元）", "1,016.65")]
    assert kpis_for({"type": "bar", "x": "城市", "y": ["企业数量"]}, ROWS) == []


def test_result_frame_preserves_column_order_and_csv_opens_in_excel():
    frame = result_frame(COLUMNS, ROWS)
    data = csv_bytes(COLUMNS, ROWS)

    assert list(frame.columns) == COLUMNS
    assert data.startswith("﻿".encode())
    assert "深圳市,80" in data.decode("utf-8-sig")


def test_trace_and_steps_frames_are_readable():
    trace = [
        {"node": "understand", "status": "ok", "ms": 1.2, "detail": {"intent": "query"}},
        {
            "node": "link",
            "status": "ok",
            "ms": 0.4,
            "detail": {"tables": ["融资数据", "企业基本信息"]},
        },
    ]
    steps = [
        {
            "index": 1,
            "tool": "preview_sql",
            "arguments": {"sql": "SELECT 1"},
            "ok": True,
            "summary": "试运行返回 1 行",
            "latency_ms": 3.0,
        }
    ]

    frame = trace_frame(trace)
    assert frame.iloc[0]["节点"] == "理解意图"
    assert frame.iloc[1]["说明"] == "tables=融资数据,企业基本信息"
    assert steps_frame(steps).iloc[0]["工具"] == "preview_sql"


def test_workflow_dot_labels_every_node_and_marks_conditional_edges():
    edges = [("__start__", "understand", False), ("understand", "link", True)]

    dot = workflow_dot(edges)

    assert set(NODE_LABELS) == set(NODE_ORDER)
    assert '"understand" -> "link" [style=dashed' in dot
    assert "理解意图" in dot
