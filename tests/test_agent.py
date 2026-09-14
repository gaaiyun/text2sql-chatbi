"""Agent 工作流集成测试：真实演示库 + 可编排的模型替身。"""

from __future__ import annotations

import json

import pytest

from text2sql.agent.exemplars import ZNJZ_EXEMPLARS_PATH, ExemplarStore
from text2sql.agent.graph import NODE_ORDER, Text2SQLAgent
from text2sql.agent.llm import ScriptedLLM, tool_call_response
from text2sql.agent.values import build_value_index
from text2sql.config import Settings
from text2sql.db.schema import load_znjz_schema
from text2sql.semantic.catalog import load_znjz_catalog

CATALOG = load_znjz_catalog()
SCHEMA = load_znjz_schema()
LONG_TAIL = "每个城市招投标记录最多的企业分别是哪家"
GOOD_SQL = (
    "SELECT SUBSTR(e.`district_code`, 1, 4) AS `城市代码`, COUNT(*) AS `招投标记录数`\n"
    "FROM `招投标` b JOIN `企业基本信息` e ON e.`eid` = b.`eid`\n"
    "GROUP BY SUBSTR(e.`district_code`, 1, 4)\nORDER BY `招投标记录数` DESC"
)


@pytest.fixture(scope="module")
def values(demo_backend):
    return build_value_index(demo_backend, CATALOG)


@pytest.fixture(scope="module")
def exemplars():
    return ExemplarStore.load(ZNJZ_EXEMPLARS_PATH)


@pytest.fixture
def make_agent(demo_backend, demo_db_path, values, exemplars):
    def factory(llm=None, planners=("semantic", "llm"), **overrides):
        mapping = {"T2S_DEMO_DB_PATH": str(demo_db_path)}
        mapping.update({k: str(v) for k, v in overrides.items()})
        return Text2SQLAgent(
            catalog=CATALOG,
            schema=SCHEMA,
            backend=demo_backend,
            settings=Settings.from_mapping(mapping),
            llm=llm,
            planners=planners,
            values=values,
            exemplars=exemplars,
        )

    return factory


def sql_then_narrative(submissions, narrative="- 共返回结果"):
    """SQL 生成调用（带 tools）按顺序提交 submissions；解读调用（不带 tools）返回 narrative。"""
    queue = list(submissions)

    def handler(messages, tools):
        if tools is None:
            return narrative
        sql = queue.pop(0) if len(queue) > 1 else queue[0]
        return tool_call_response("submit_sql", {"sql": sql, "assumptions": ["按企业登记地统计"]})

    return ScriptedLLM(handler=handler)


def node_names(result):
    return [entry["node"] for entry in result.trace]


# --------------------------------------------------------------------------- 语义层与预检


def test_semantic_question_is_answered_offline_with_full_trace(make_agent):
    result = make_agent().ask("广州市存续企业有多少家")

    assert result.status == "answered" and result.success
    assert result.planner == "semantic"
    assert result.rows[0]["企业数量"] > 0
    assert "LIKE '存续%'" in result.sql
    assert result.chart["type"] == "kpi"
    assert result.quality["score"] >= 0.9
    assert node_names(result) == [
        "understand",
        "link",
        "plan",
        "guard",
        "execute",
        "profile",
        "visualize",
        "narrate",
        "reflect",
        "finalize",
    ]
    assert all("ms" in entry for entry in result.trace)
    assert result.narrative_source == "deterministic"


def test_graph_declares_eleven_nodes():
    assert NODE_ORDER == (
        "understand",
        "link",
        "plan",
        "guard",
        "execute",
        "repair",
        "profile",
        "visualize",
        "narrate",
        "reflect",
        "finalize",
    )


def test_write_request_is_rejected_before_any_planning(make_agent):
    result = make_agent().ask("删除所有注销企业的数据")

    assert result.status == "rejected"
    assert result.sql is None
    assert node_names(result) == ["understand", "finalize"]
    assert "只读" in result.message


def test_personal_information_request_is_rejected_without_calling_the_model(make_agent):
    llm = ScriptedLLM(handler=lambda m, t: pytest.fail("不应调用模型"))

    result = make_agent(llm=llm).ask("查询所有企业法定代表人的姓名和电话")

    assert result.status == "rejected"
    assert node_names(result) == ["understand", "finalize"]
    assert llm.calls == []


def test_count_question_answered_per_group_is_repaired(make_agent):
    per_group = (
        "SELECT COUNT(DISTINCT `eid`) AS n FROM `标签数据` GROUP BY `eid` HAVING COUNT(*) > 3"
    )
    total = (
        "SELECT COUNT(*) AS n FROM (SELECT `eid` FROM `标签数据` GROUP BY `eid` "
        "HAVING COUNT(*) > 3) t"
    )
    llm = sql_then_narrative([per_group, total])

    result = make_agent(llm=llm).ask("标签记录超过3条的企业有多少家")

    assert result.status == "answered", result.message
    assert result.row_count == 1 and "repair" in node_names(result)
    feedback = next(
        c for c in llm.calls if c["tools"] and "上一次提交" in c["messages"][1]["content"]
    )
    assert "外层" in feedback["messages"][1]["content"]


def test_out_of_domain_question_is_declined(make_agent):
    assert make_agent().ask("今天天气怎么样").status == "declined"


def test_long_tail_without_model_is_declined_with_reason_and_suggestions(make_agent):
    result = make_agent().ask(LONG_TAIL)

    assert result.status == "declined"
    assert "窗口函数" in result.message
    assert "OPENAI_API_KEY" in result.message
    assert 1 <= len(result.suggestions) <= 3


# --------------------------------------------------------------------------- 模型路径


def test_llm_path_uses_tools_executes_and_narrates(make_agent):
    llm = sql_then_narrative([GOOD_SQL], narrative="- 招投标记录数最多的城市代码是 4401")
    result = make_agent(llm=llm).ask(LONG_TAIL)

    assert result.status == "answered", result.message
    assert result.planner == "llm"
    assert result.agent_steps[-1]["tool"] == "submit_sql"
    assert result.assumptions == ["按企业登记地统计"]
    assert result.usage["calls"] >= 2
    assert result.narrative_source == "llm"


def test_invented_numbers_in_model_narrative_fall_back_to_deterministic(make_agent):
    llm = sql_then_narrative(
        [GOOD_SQL], narrative="- 招投标记录总数达到 9999999 条，同比增长 37.5%"
    )
    result = make_agent(llm=llm).ask(LONG_TAIL)

    assert result.narrative_source == "llm_rejected"
    assert "9999999" not in result.answer
    check = next(c for c in result.quality["checks"] if c["name"] == "numbers_grounded")
    assert check["status"] == "warn"


def test_execution_error_is_repaired_with_feedback(make_agent):
    broken = "SELECT COUNT(*) AS n FROM `招投标` WHERE publish_time > 'not-a-date'"
    llm = sql_then_narrative([broken, GOOD_SQL])
    result = make_agent(llm=llm).ask(LONG_TAIL)

    assert result.status == "answered", result.message
    assert "repair" in node_names(result)
    feedback_prompts = [
        c
        for c in llm.calls
        if c["tools"] and "上一次提交的 SQL 没有通过" in c["messages"][1]["content"]
    ]
    assert feedback_prompts


def test_suspicious_empty_result_is_repaired_with_real_values(make_agent):
    wrong = "SELECT COUNT(*) AS n FROM `企业基本信息` WHERE status = '存续'"
    right = "SELECT COUNT(*) AS n FROM `企业基本信息` WHERE status LIKE '存续%'"
    llm = sql_then_narrative([wrong, right], narrative="- 结果见表格")
    result = make_agent(llm=llm).ask("状态标记为存续的公司一共多少家")

    assert result.status == "answered"
    assert result.rows[0]["n"] > 0
    feedback = next(
        c for c in llm.calls if c["tools"] and "上一次提交" in c["messages"][1]["content"]
    )
    assert "存续（在营、开业、在册）" in feedback["messages"][1]["content"]


def test_policy_rejection_from_plain_text_sql_is_final(make_agent):
    llm = ScriptedLLM(["```sql\nSELECT oper_name FROM `企业基本信息` LIMIT 5\n```"])
    result = make_agent(llm=llm).ask("列出各企业的法定代表人姓名")

    assert result.status == "rejected"
    assert "个人信息" in result.message
    assert "repair" not in node_names(result)


def test_cost_guard_stops_expensive_queries_when_repairs_exhausted(make_agent):
    costly = (
        "SELECT a.title FROM `招投标` a, `招投标` b, `企业基本信息` c ORDER BY a.title LIMIT 10"
    )
    llm = ScriptedLLM(handler=lambda m, t: "```sql\n" + costly + "\n```")
    result = make_agent(llm=llm, T2S_MAX_REPAIRS=0).ask(LONG_TAIL)

    assert result.status == "failed"
    assert "超过上限" in result.message


# --------------------------------------------------------------------------- 多轮与流式


def test_follow_up_uses_checkpointed_history_per_thread(make_agent):
    agent = make_agent()
    first = agent.ask("广州市存续企业有多少家", thread_id="t-follow")
    second = agent.ask("那深圳呢", thread_id="t-follow")
    fresh = agent.ask("那深圳呢", thread_id="t-other")

    assert second.status == "answered"
    assert second.effective_question == "深圳存续企业有多少家"
    assert second.rows[0]["企业数量"] != first.rows[0]["企业数量"]
    assert second.assumptions[0].startswith("承接上一轮")
    assert fresh.status == "declined"
    assert "上一轮" in fresh.message
    assert [turn["question"] for turn in agent.history("t-follow")] == [
        "广州市存续企业有多少家",
        "那深圳呢",
    ]


def test_stream_emits_nodes_agent_steps_and_result(make_agent):
    llm = sql_then_narrative([GOOD_SQL])
    events = list(make_agent(llm=llm).stream(LONG_TAIL))
    kinds = [event["type"] for event in events]

    assert kinds[-1] == "result"
    assert "agent_step" in kinds
    assert [e["node"] for e in events if e["type"] == "node"][:3] == ["understand", "link", "plan"]
    assert events[-1]["result"]["status"] == "answered"


# --------------------------------------------------------------------------- 规划器开关与输出


def test_llm_only_planner_skips_semantic_layer(make_agent):
    llm = sql_then_narrative(
        [
            "SELECT COUNT(DISTINCT eid) AS n FROM `企业基本信息` WHERE status LIKE '存续%' AND district_code LIKE '4401%'"
        ]
    )
    result = make_agent(llm=llm, planners=("llm",)).ask("广州市存续企业有多少家")

    assert result.planner == "llm"


def test_semantic_only_planner_never_calls_the_model(make_agent):
    llm = ScriptedLLM(
        handler=lambda m, t: pytest.fail("semantic-only agent must not call the model")
    )
    agent = make_agent(llm=llm, planners=("semantic",))

    assert agent.ask("广州市存续企业有多少家").narrative_source == "deterministic"
    assert agent.ask(LONG_TAIL).status == "declined"
    assert llm.calls == []


def test_result_serializes_with_backward_compatible_fields(make_agent):
    payload = make_agent().ask("各融资轮次的企业数量").to_dict()

    json.dumps(payload, ensure_ascii=False)
    for key in (
        "question",
        "success",
        "sql",
        "safe_sql",
        "columns",
        "rows",
        "row_count",
        "analysis",
        "report",
        "chart",
        "safety",
        "trace",
        "quality",
    ):
        assert key in payload
    assert payload["analysis"] == payload["answer"]


def test_report_markdown_contains_answer_sql_table_and_checks(make_agent):
    report = make_agent().ask("各融资轮次的企业数量").report_markdown()

    assert report.startswith("# 各融资轮次的企业数量")
    assert "```sql" in report
    assert "| 融资轮次 | 企业数量 |" in report
    assert "质量检查" in report


def test_status_describes_runtime_without_secrets(make_agent):
    status = make_agent(llm=ScriptedLLM(["x"])).status()

    assert status["backend"] == "duckdb-demo"
    assert status["planners"] == ["semantic", "llm"]
    assert status["value_index"]["columns"] > 0
    assert "api_key" not in json.dumps(status)


def test_from_settings_builds_offline_agent(tmp_path):
    agent = Text2SQLAgent.from_settings(
        Settings.from_mapping({"T2S_DEMO_DB_PATH": str(tmp_path / "demo.duckdb")})
    )
    try:
        assert agent.llm is None
        assert agent.ask("资质状态分布").status == "answered"
    finally:
        agent.close()


def test_llm_errors_become_failed_results(make_agent):
    from text2sql.agent.llm import LLMError

    llm = ScriptedLLM([LLMError("模型调用失败（APIConnectionError）：reset")])
    result = make_agent(llm=llm).ask(LONG_TAIL)

    assert result.status == "failed"
    assert "模型调用失败" in result.message
