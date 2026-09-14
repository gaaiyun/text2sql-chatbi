"""HTTP API：真实演示库 + 可编排的模型替身，通过 TestClient 走完整的 ASGI 栈（含 lifespan）。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from text2sql.agent.exemplars import ZNJZ_EXEMPLARS_PATH, ExemplarStore
from text2sql.agent.graph import Text2SQLAgent
from text2sql.agent.llm import ScriptedLLM, tool_call_response
from text2sql.agent.values import build_value_index
from text2sql.api.app import create_app
from text2sql.config import Settings
from text2sql.db.backends import DuckDBBackend
from text2sql.db.schema import load_znjz_schema
from text2sql.semantic.catalog import load_znjz_catalog

CATALOG = load_znjz_catalog()
SCHEMA = load_znjz_schema()


@pytest.fixture(scope="module")
def shared(demo_backend):
    return {
        "values": build_value_index(demo_backend, CATALOG),
        "exemplars": ExemplarStore.load(ZNJZ_EXEMPLARS_PATH),
    }


@pytest.fixture
def make_client(demo_db_path, shared):
    """按需组装 app；每个 app 持有自己的只读连接，关闭时不影响会话级的演示库连接。"""
    opened: list[TestClient] = []

    def factory(*, llm=None, planners=("semantic",), raise_errors=True, **env):
        settings = Settings.from_mapping({"T2S_DEMO_DB_PATH": str(demo_db_path), **env})

        def agent_factory(s: Settings) -> Text2SQLAgent:
            return Text2SQLAgent(
                catalog=CATALOG,
                schema=SCHEMA,
                backend=DuckDBBackend(demo_db_path),
                settings=s,
                llm=llm,
                planners=planners,
                values=shared["values"],
                exemplars=shared["exemplars"],
            )

        app = create_app(settings, agent_factory=agent_factory)
        client = TestClient(app, raise_server_exceptions=raise_errors)
        client.__enter__()
        opened.append(client)
        return client

    yield factory
    for client in opened:
        client.__exit__(None, None, None)


def sse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        events.append((fields["event"], json.loads(fields["data"])))
    return events


# --------------------------------------------------------------------------- 元数据


def test_health_reports_runtime_without_secrets(make_client):
    client = make_client(OPENAI_API_KEY="")

    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["backend"] == "duckdb-demo"
    assert body["llm"] is None
    assert body["nodes"] == 11


def test_catalog_and_examples(make_client):
    client = make_client()

    assert client.get("/api/v1/catalog").json()["dataset"] == "znjz"
    examples = client.get("/api/v1/examples").json()
    assert examples and {"question", "category", "available"} <= set(examples[0])
    assert all(not e["available"] for e in examples if e["requires_llm"])


# --------------------------------------------------------------------------- 问答


def test_query_returns_answer_sql_rows_and_trace(make_client):
    response = make_client().post("/api/v1/query", json={"question": "广州市存续企业有多少家"})
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "answered"
    assert body["planner"] == "semantic"
    assert "LIKE" in body["safe_sql"]
    assert body["rows"] and body["trace"]


@pytest.mark.parametrize(
    "payload",
    [
        {"question": ""},
        {"question": "企" * 501},
        {"question": "资质状态分布", "thread_id": "../etc/passwd"},
        {},
    ],
)
def test_invalid_requests_are_rejected_with_422(make_client, payload):
    assert make_client().post("/api/v1/query", json=payload).status_code == 422


def test_follow_up_questions_share_a_thread(make_client):
    client = make_client()

    client.post("/api/v1/query", json={"question": "广州市存续企业有多少家", "thread_id": "demo-1"})
    second = client.post(
        "/api/v1/query", json={"question": "那深圳呢", "thread_id": "demo-1"}
    ).json()
    history = client.get("/api/v1/threads/demo-1").json()

    assert second["status"] == "answered"
    assert "深圳" in second["effective_question"]
    assert [turn["question"] for turn in history["turns"]] == ["广州市存续企业有多少家", "那深圳呢"]


def test_stream_emits_node_events_then_result(make_client):
    response = make_client().post("/api/v1/query/stream", json={"question": "资质状态分布"})
    events = sse_events(response.text)

    assert response.headers["content-type"].startswith("text/event-stream")
    assert [name for name, _ in events if name == "node"]
    assert events[-1][0] == "result"
    assert events[-1][1]["result"]["status"] == "answered"


def test_stream_forwards_sql_agent_tool_steps(make_client):
    sql = (
        "SELECT q.`state` AS `资质状态`, COUNT(*) AS `资质记录数` FROM `标签数据` q "
        "GROUP BY q.`state` ORDER BY `资质记录数` DESC"
    )

    def handler(messages, tools):
        if tools is None:
            return "- 已统计资质状态"
        return tool_call_response("submit_sql", {"sql": sql, "assumptions": ["按资质状态统计"]})

    client = make_client(llm=ScriptedLLM(handler=handler), planners=("llm",))
    events = sse_events(client.post("/api/v1/query/stream", json={"question": "资质状态分布"}).text)

    steps = [data for name, data in events if name == "agent_step"]
    assert steps and steps[0]["tool"] == "submit_sql"
    assert events[-1][1]["result"]["planner"] == "llm"


# --------------------------------------------------------------------------- 访问控制与兼容


def test_password_protects_api_but_not_health(make_client):
    client = make_client(APP_PASSWORD="s3cret")

    assert client.get("/health").status_code == 200
    assert client.post("/api/v1/query", json={"question": "资质状态分布"}).status_code == 401
    assert client.get("/api/v1/catalog").status_code == 401
    ok = client.post(
        "/api/v1/query", json={"question": "资质状态分布"}, headers={"X-App-Password": "s3cret"}
    )
    assert ok.status_code == 200


def test_legacy_endpoint_keeps_v1_contract(make_client):
    client = make_client(APP_PASSWORD="s3cret")

    body = client.post(
        "/api/agent/query",
        json={"question": "资质状态分布", "scenario": "industry", "password": "s3cret"},
    ).json()

    assert body["success"] is True
    assert body["scenario"] == "industry"
    assert body["analysis"] and body["report"]
    assert body["safety"]["is_safe"] is True


def test_rate_limit_returns_429(make_client):
    client = make_client(T2S_RATE_LIMIT="2/minute")

    codes = [
        client.post("/api/v1/query", json={"question": "资质状态分布"}).status_code
        for _ in range(3)
    ]

    assert codes == [200, 200, 429]


def test_cors_allows_configured_origin(make_client):
    client = make_client(T2S_ALLOWED_ORIGINS="https://bi.example")

    response = client.options(
        "/api/v1/query",
        headers={"Origin": "https://bi.example", "Access-Control-Request-Method": "POST"},
    )

    assert response.headers["access-control-allow-origin"] == "https://bi.example"


def test_internal_errors_do_not_leak_details(make_client):
    client = make_client(raise_errors=False)
    client.app.state.agent.ask = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db-password"))

    response = client.post("/api/v1/query", json={"question": "资质状态分布"})

    assert response.status_code == 500
    assert "db-password" not in response.text
    assert response.json()["error_id"]
