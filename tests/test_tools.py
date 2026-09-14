from __future__ import annotations

import json

import pytest

from text2sql.agent.linking import SchemaLinker
from text2sql.agent.tools import TOOL_SPECS, AgentToolbox
from text2sql.agent.values import build_value_index
from text2sql.db.schema import load_znjz_schema
from text2sql.semantic.catalog import load_znjz_catalog
from text2sql.sql.guard import SQLGuard

CATALOG = load_znjz_catalog()
SCHEMA = load_znjz_schema()


@pytest.fixture(scope="module")
def toolbox(demo_backend):
    return AgentToolbox(
        catalog=CATALOG,
        schema=SCHEMA,
        linker=SchemaLinker(CATALOG, SCHEMA),
        guard=SQLGuard.from_catalog(CATALOG, SCHEMA),
        backend=demo_backend,
        values=build_value_index(demo_backend, CATALOG),
        cost_limit=50_000_000,
    )


def test_tool_specs_are_valid_function_schemas(toolbox):
    names = [spec["function"]["name"] for spec in TOOL_SPECS]

    assert names == [*AgentToolbox.names, "submit_sql"]
    for spec in TOOL_SPECS:
        parameters = spec["function"]["parameters"]
        assert spec["type"] == "function"
        assert parameters["type"] == "object"
        assert set(parameters["required"]) <= set(parameters["properties"])
        json.dumps(spec, ensure_ascii=False)


def test_search_schema_returns_relevant_tables(toolbox):
    result = toolbox.execute("search_schema", {"keywords": "融资轮次"})

    assert result.ok
    assert result.payload["tables"][0]["name"] == "融资数据"


def test_describe_table_known_and_unknown(toolbox):
    known = toolbox.execute("describe_table", {"table": "`招投标`"})
    unknown = toolbox.execute("describe_table", {"table": "专利信息"})

    assert known.ok and "publish_time" in known.payload["description"]
    assert not unknown.ok and "招投标" in unknown.payload["available"]


def test_get_column_values_from_index_and_by_keyword(toolbox):
    status = toolbox.execute("get_column_values", {"table": "企业基本信息", "column": "status"})
    cancelled = toolbox.execute(
        "get_column_values", {"table": "企业基本信息", "column": "status", "keyword": "注销"}
    )

    assert status.ok and status.payload["values"][0][0] == "存续（在营、开业、在册）"
    assert cancelled.ok and cancelled.payload["values"]
    assert all("注销" in value for value, _ in cancelled.payload["values"])


def test_get_column_values_on_unindexed_column_queries_live(toolbox):
    result = toolbox.execute(
        "get_column_values", {"table": "招投标", "column": "area_code", "keyword": "4403"}
    )

    assert result.ok
    assert all(
        value.startswith("4403") and value.endswith(".0") for value, _ in result.payload["values"]
    )


@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        ({"table": "企业基本信息", "column": "oper_name"}, "个人信息"),
        ({"table": "企业基本信息", "column": "company_name"}, "不存在"),
        ({"table": "users", "column": "id"}, "不存在"),
    ],
)
def test_get_column_values_rejects_bad_targets(toolbox, arguments, fragment):
    result = toolbox.execute("get_column_values", arguments)

    assert not result.ok
    assert fragment in result.payload["error"]


def test_keyword_with_quotes_cannot_break_out(toolbox):
    result = toolbox.execute(
        "get_column_values",
        {"table": "企业基本信息", "column": "status", "keyword": "x' OR '1'='1"},
    )
    assert result.ok
    assert result.payload["values"] == []


def test_validate_sql_returns_errors_and_hints(toolbox):
    result = toolbox.execute("validate_sql", {"sql": "SELECT industry_name FROM `企业行业代码`"})

    assert not result.ok
    assert any("industry_code" in hint for hint in result.payload["hints"])


def test_preview_sql_returns_at_most_five_rows(toolbox):
    result = toolbox.execute(
        "preview_sql",
        {"sql": "SELECT `round`, COUNT(*) AS n FROM `融资数据` GROUP BY `round` ORDER BY n DESC"},
    )

    assert result.ok
    assert 0 < len(result.payload["rows"]) <= 5
    assert result.payload["columns"] == ["round", "n"]


def test_preview_sql_reports_the_full_row_count_and_a_column_summary(toolbox):
    result = toolbox.execute(
        "preview_sql",
        {"sql": "SELECT `round`, COUNT(*) AS n FROM `融资数据` GROUP BY `round` ORDER BY n DESC"},
    )

    payload = result.payload
    rounds = {
        row["round"]
        for row in toolbox.backend.execute(
            "SELECT DISTINCT `round` FROM `融资数据` WHERE `round` IS NOT NULL", max_rows=100
        ).rows
    }
    assert payload["row_count"] >= len(payload["rows"]) and payload["row_count"] > 5
    assert payload["column_summary"]["round"]["distinct"] == len(rounds)
    assert payload["column_summary"]["n"]["min"] >= 1
    assert (
        f"试运行返回 {payload['row_count']} 行" in result.summary
        and "展示前 5 行" in result.summary
    )


def test_preview_sql_explains_suspicious_empty_result(toolbox):
    result = toolbox.execute(
        "preview_sql",
        {
            "sql": "SELECT COUNT(DISTINCT eid) AS n FROM `企业基本信息` WHERE status = '存续' GROUP BY status"
        },
    )

    assert result.ok
    assert result.payload["row_count"] == 0
    assert "存续（在营、开业、在册）" in " ".join(result.payload["hints"])
    assert "取值" in result.summary


def test_zero_count_aggregate_is_treated_as_effectively_empty(toolbox):
    """COUNT(*) 永远返回一行；值为 0 的聚合和 0 行结果一样可能是取值写错。"""
    result = toolbox.execute(
        "preview_sql", {"sql": "SELECT COUNT(*) AS n FROM `企业基本信息` WHERE status = '存续'"}
    )

    assert result.ok
    assert result.payload["rows"] == [{"n": 0}]
    assert "存续（在营、开业、在册）" in " ".join(result.payload["hints"])


def test_preview_sql_blocks_unsafe_and_costly_queries(toolbox):
    unsafe = toolbox.execute("preview_sql", {"sql": "DELETE FROM `企业基本信息`"})
    costly = toolbox.execute(
        "preview_sql",
        {
            "sql": "SELECT a.title FROM `招投标` a, `招投标` b, `企业基本信息` c ORDER BY a.title LIMIT 5"
        },
    )

    assert not unsafe.ok
    assert not costly.ok and "超过上限" in costly.payload["error"]


def test_unknown_tools_and_bad_arguments_never_raise(toolbox):
    unknown = toolbox.execute("drop_database", {})
    missing = toolbox.execute("preview_sql", {})
    extra = toolbox.execute("search_schema", {"keywords": "招投标", "unexpected": 1})

    assert not unknown.ok and "未知工具" in unknown.payload["error"]
    assert not missing.ok and "参数" in missing.payload["error"]
    assert extra.ok
