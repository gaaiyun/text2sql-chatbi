"""两种编排器跑同一组节点与同一张路由表，结果必须逐项一致。

服务端（CLI / API / MCP）用 LangGraph：检查点、流式事件、线程级会话记忆。浏览器里的 Pyodide 装不上
LangGraph 的原生依赖（ormsgpack、uuid-utils），网页端改用顺序编排器。这组测试用完整评测集、
多轮追问和 SQL 智能体修复回路核对两者输出，保证网页上看到的就是服务端那套逻辑。
"""

from __future__ import annotations

import pytest

from text2sql.agent.exemplars import ZNJZ_EXEMPLARS_PATH, ExemplarStore
from text2sql.agent.graph import (
    CONDITIONAL_EDGES,
    END_NODE,
    FIXED_EDGES,
    NODE_ORDER,
    Text2SQLAgent,
)
from text2sql.agent.llm import ScriptedLLM, tool_call_response
from text2sql.agent.runner import SequentialRunner
from text2sql.agent.values import build_value_index
from text2sql.config import Settings
from text2sql.db.schema import load_znjz_schema
from text2sql.evaluation.runner import load_benchmark
from text2sql.semantic.catalog import load_znjz_catalog

CATALOG = load_znjz_catalog()
SCHEMA = load_znjz_schema()
BENCHMARK = load_benchmark()


@pytest.fixture(scope="module")
def make_pair(demo_backend, demo_db_path):
    shared = {
        "catalog": CATALOG,
        "schema": SCHEMA,
        "backend": demo_backend,
        "settings": Settings.from_mapping({"T2S_DEMO_DB_PATH": str(demo_db_path)}),
        "values": build_value_index(demo_backend, CATALOG),
        "exemplars": ExemplarStore.load(ZNJZ_EXEMPLARS_PATH),
    }

    def factory(llm_factory=None, planners=("semantic",)):
        return tuple(
            Text2SQLAgent(
                **shared,
                llm=llm_factory() if llm_factory else None,
                planners=planners,
                orchestrator=orchestrator,
            )
            for orchestrator in ("langgraph", "sequential")
        )

    return factory


def comparable(result) -> dict:
    """去掉耗时、线程号这类每次运行都不同的字段，其余逐项比较。"""
    data = result if isinstance(result, dict) else result.to_dict()
    data = {k: v for k, v in data.items() if k not in ("elapsed_ms", "thread_id", "report")}
    data["trace"] = [
        (entry["node"], entry["status"], {k: v for k, v in entry["detail"].items() if k != "ms"})
        for entry in data["trace"]
    ]
    data["agent_steps"] = [
        {k: v for k, v in step.items() if k != "latency_ms"} for step in data["agent_steps"]
    ]
    return data


# --------------------------------------------------------------------------- 路由表


def test_routing_table_reaches_every_node_and_ends_at_finalize():
    sources = set(FIXED_EDGES) | set(CONDITIONAL_EDGES)
    targets = set(FIXED_EDGES.values()) | {t for ts in CONDITIONAL_EDGES.values() for t in ts}

    assert sources == set(NODE_ORDER)
    assert targets - {END_NODE} <= set(NODE_ORDER)
    assert FIXED_EDGES["finalize"] == END_NODE
    assert not set(FIXED_EDGES) & set(CONDITIONAL_EDGES)


def test_langgraph_edges_are_built_from_the_routing_table(make_pair):
    graph_agent, _ = make_pair()
    drawn = {(e.source, e.target) for e in graph_agent.graph.get_graph().edges}
    expected = {(s, t) for s, t in FIXED_EDGES.items()}
    expected |= {(s, t) for s, ts in CONDITIONAL_EDGES.items() for t in ts}
    expected.add(("__start__", "understand"))

    assert drawn == expected


# --------------------------------------------------------------------------- 等价性


@pytest.mark.parametrize("item", BENCHMARK, ids=[item.id for item in BENCHMARK])
def test_benchmark_results_are_identical(make_pair, item):
    graph_agent, sequential_agent = make_pair()

    assert comparable(sequential_agent.ask(item.question)) == comparable(
        graph_agent.ask(item.question)
    )


def test_multi_turn_conversations_are_identical(make_pair):
    graph_agent, sequential_agent = make_pair()
    turns = ["广州市存续企业有多少家", "那深圳呢", "按年份看呢", "去掉存续", "那佛山呢"]

    for question in turns:
        expected = graph_agent.ask(question, thread_id="conversation")
        actual = sequential_agent.ask(question, thread_id="conversation")
        assert comparable(actual) == comparable(expected), question

    assert sequential_agent.history("conversation") == graph_agent.history("conversation")


def test_sql_agent_repair_loop_is_identical(make_pair):
    # 取值写错：库里的状态是“存续（在营、开业、在册）”，等值匹配为 0，触发可疑空结果修复
    wrong = "SELECT COUNT(*) AS n FROM `企业基本信息` WHERE status = '存续'"
    right = "SELECT COUNT(*) AS n FROM `企业基本信息` WHERE status LIKE '存续%'"

    def llm_factory():
        submissions = [wrong, right]

        def handler(messages, tools):
            if tools is None:
                return "- 结果见表格"
            sql = submissions.pop(0) if len(submissions) > 1 else submissions[0]
            return tool_call_response("submit_sql", {"sql": sql, "assumptions": ["按经营状态统计"]})

        return ScriptedLLM(handler=handler)

    graph_agent, sequential_agent = make_pair(llm_factory, planners=("llm",))
    expected = graph_agent.ask("状态标记为存续的公司一共多少家")
    actual = sequential_agent.ask("状态标记为存续的公司一共多少家")

    assert expected.status == "answered"
    assert any(entry["node"] == "repair" for entry in expected.trace)
    assert comparable(actual) == comparable(expected)


def test_stream_events_are_identical(make_pair):
    def events(agent):
        collected = list(agent.stream("各城市有融资记录的企业有多少家"))
        kinds = [(e["type"], e.get("node") or e.get("tool")) for e in collected[:-1]]
        return kinds, comparable(collected[-1]["result"])

    graph_agent, sequential_agent = make_pair()

    assert events(sequential_agent) == events(graph_agent)


# --------------------------------------------------------------------------- 顺序编排器自身


def test_sequential_runner_emits_events_as_nodes_finish(make_pair):
    _, sequential_agent = make_pair()
    seen = []

    state = sequential_agent.runner.run("资质状态分布", "live", emit=seen.append)

    assert [e["node"] for e in seen if e["type"] == "node"] == [t["node"] for t in state["trace"]]


def test_sequential_runner_rejects_routes_outside_the_table():
    class Rogue:
        def _node_understand(self, state):
            return {"route": "execute", "trace": [{"node": "understand"}]}

    with pytest.raises(ValueError, match="execute"):
        SequentialRunner(Rogue()).run("x", "t")


def test_sequential_runner_stops_runaway_loops():
    class Loop:
        def _node_understand(self, state):
            return {"route": "link", "trace": [{"node": "understand"}]}

        def _node_link(self, state):
            return {"trace": [{"node": "link"}]}

        def _node_plan(self, state):
            return {"route": "guard", "trace": [{"node": "plan"}]}

        def _node_guard(self, state):
            return {"route": "repair", "trace": [{"node": "guard"}]}

        def _node_repair(self, state):
            return {"route": "guard", "trace": [{"node": "repair"}]}

    with pytest.raises(RecursionError):
        SequentialRunner(Loop(), max_steps=20).run("x", "t")


def test_status_reports_orchestrator(make_pair):
    graph_agent, sequential_agent = make_pair()

    assert graph_agent.status()["orchestrator"] == "langgraph"
    assert sequential_agent.status()["orchestrator"] == "sequential"
    assert sequential_agent.status()["checkpointer"] is None
