"""MCP server：用 MCP SDK 的进程内客户端走真实协议（初始化、列工具、调用工具、读资源、取提示词）。"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp import Client

from text2sql.agent.exemplars import ZNJZ_EXEMPLARS_PATH, ExemplarStore
from text2sql.agent.graph import Text2SQLAgent
from text2sql.agent.values import build_value_index
from text2sql.config import Settings
from text2sql.db.schema import load_znjz_schema
from text2sql.mcp_server import PREVIEW_ROWS, build_server
from text2sql.semantic.catalog import load_znjz_catalog

CATALOG = load_znjz_catalog()
SCHEMA = load_znjz_schema()
TOOLS = {
    "ask_database",
    "search_schema",
    "describe_table",
    "get_column_values",
    "run_readonly_sql",
    "describe_semantic_layer",
}


@pytest.fixture(scope="module")
def server(demo_backend, demo_db_path):
    agent = Text2SQLAgent(
        catalog=CATALOG,
        schema=SCHEMA,
        backend=demo_backend,
        settings=Settings.from_mapping({"T2S_DEMO_DB_PATH": str(demo_db_path)}),
        planners=("semantic",),
        values=build_value_index(demo_backend, CATALOG),
        exemplars=ExemplarStore.load(ZNJZ_EXEMPLARS_PATH),
    )
    return build_server(agent=agent)


def session(server, action):
    async def scenario():
        async with Client(server) as client:
            return await action(client)

    return asyncio.run(scenario())


def call(server, name, arguments=None):
    return session(server, lambda client: client.call_tool(name, arguments or {}))


# --------------------------------------------------------------------------- 发现


def test_tools_are_listed_with_read_only_annotations_and_schemas(server):
    tools = session(server, lambda client: client.list_tools()).tools
    ask = next(t for t in tools if t.name == "ask_database")

    assert {t.name for t in tools} == TOOLS
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)
    assert all(not t.annotations.destructive_hint for t in tools)
    assert "thread_id" in ask.input_schema["properties"]
    assert {"status", "answer", "sql", "rows"} <= set(ask.output_schema["properties"])


def test_server_instructions_point_to_the_right_entry_tool(server):
    async def handshake(client):
        return client.instructions, client.server_info

    instructions, info = session(server, handshake)

    assert "ask_database" in instructions
    assert info.name == "text2sql-analysis"


# --------------------------------------------------------------------------- 问答


def test_ask_database_returns_structured_answer(server):
    result = call(server, "ask_database", {"question": "广州市存续企业有多少家"})
    data = result.structured_content

    assert not result.is_error
    assert data["status"] == "answered"
    assert "LIKE" in data["sql"]
    assert data["rows"] and data["thread_id"]
    assert data["quality_score"] == 1.0


def test_ask_database_supports_follow_up_through_thread_id(server):
    call(server, "ask_database", {"question": "广州市存续企业有多少家", "thread_id": "mcp-1"})
    second = call(server, "ask_database", {"question": "那深圳呢", "thread_id": "mcp-1"})

    assert "深圳" in second.structured_content["effective_question"]


def test_ask_database_preview_is_capped(server):
    data = call(
        server, "ask_database", {"question": "按成立年份统计企业数量趋势"}
    ).structured_content

    assert len(data["rows"]) <= PREVIEW_ROWS
    assert data["truncated"] == (data["row_count"] > len(data["rows"]))


def test_declined_question_carries_reason_and_suggestions(server):
    data = call(
        server, "ask_database", {"question": "每个城市招投标记录最多的企业分别是哪家"}
    ).structured_content

    assert data["status"] == "declined"
    assert data["answer"]
    assert data["suggestions"]


def test_invalid_thread_id_is_a_tool_error(server):
    result = call(server, "ask_database", {"question": "资质状态分布", "thread_id": "../x"})

    assert result.is_error


# --------------------------------------------------------------------------- 写 SQL 的工具


def test_run_readonly_sql_executes_through_the_guard(server):
    data = call(
        server,
        "run_readonly_sql",
        {"sql": "SELECT status, COUNT(*) AS n FROM `企业基本信息` GROUP BY status"},
    ).structured_content

    assert data["ok"] is True
    assert data["data"]["rows"]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM `企业基本信息`",
        "SELECT oper_name FROM `企业基本信息`",
        "SELECT user FROM mysql.user",
    ],
)
def test_run_readonly_sql_refuses_unsafe_statements(server, sql):
    data = call(server, "run_readonly_sql", {"sql": sql}).structured_content

    assert data["ok"] is False
    assert data["data"]["errors"]


def test_schema_tools_expose_documentation_but_not_personal_data(server):
    tables = call(server, "search_schema", {"keywords": "融资轮次"}).structured_content
    described = call(server, "describe_table", {"table": "融资数据"}).structured_content
    sensitive = call(
        server, "get_column_values", {"table": "企业基本信息", "column": "oper_name"}
    ).structured_content

    assert any(t["name"] == "融资数据" for t in tables["data"]["tables"])
    assert "round" in described["data"]["description"]
    assert sensitive["ok"] is False


# --------------------------------------------------------------------------- 资源与提示词


def test_semantic_layer_tool_and_resource_agree(server):
    tool_data = call(server, "describe_semantic_layer").structured_content
    resource = session(server, lambda client: client.read_resource("text2sql://semantic-layer"))

    assert tool_data["dataset"] == json.loads(resource.contents[0].text)["dataset"] == "znjz"


def test_analysis_prompt_guides_tool_usage(server):
    prompt = session(
        server, lambda client: client.get_prompt("analyze_question", {"question": "各城市企业数量"})
    )
    text = prompt.messages[0].content.text

    assert "各城市企业数量" in text
    assert "ask_database" in text
    assert "run_readonly_sql" in text
