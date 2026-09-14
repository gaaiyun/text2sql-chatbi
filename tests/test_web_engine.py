"""浏览器端引擎的 Python 入口：在 CPython 下验证它在 Pyodide 里要做的事。"""

from __future__ import annotations

import json

import pytest

from text2sql.web.engine import WebEngine


@pytest.fixture(scope="module")
def engine(demo_db_path):
    engine = WebEngine(str(demo_db_path))
    yield engine
    engine.close()


def test_info_describes_runtime_and_uses_sequential_orchestrator(engine):
    info = json.loads(engine.info())

    assert info["orchestrator"] == "sequential"
    assert info["nodes"][0] == "understand"
    assert info["duckdb"] and info["sqlglot"] and info["python"]
    assert info["value_index"]["columns"] > 0


def test_ask_emits_node_events_and_returns_json_result(engine):
    events = []

    result = json.loads(engine.ask("各城市有融资记录的企业有多少家", "t1", events.append))

    assert result["status"] == "answered"
    assert result["rows"]
    nodes = [json.loads(e)["node"] for e in events if json.loads(e)["type"] == "node"]
    assert nodes == [entry["node"] for entry in result["trace"]]


def test_threads_keep_context_until_reset(engine):
    engine.ask("广州市存续企业有多少家", "t2")
    follow = json.loads(engine.ask("那深圳呢", "t2"))
    assert "深圳" in follow["effective_question"]

    engine.reset("t2")
    fresh = json.loads(engine.ask("那深圳呢", "t2"))
    assert fresh["status"] == "declined"


def test_explain_returns_debugger_payload(engine):
    data = json.loads(engine.explain("资质状态分布"))

    assert data["sql"] and data["matches"]


LONG_TAIL = "每个城市招投标记录最多的企业分别是哪家"
AGENT_SQL = (
    "SELECT SUBSTR(e.`district_code`, 1, 4) AS `城市代码`, COUNT(*) AS `招投标记录数` "
    "FROM `招投标` b JOIN `企业基本信息` e ON e.`eid` = b.`eid` "
    "GROUP BY SUBSTR(e.`district_code`, 1, 4) ORDER BY `招投标记录数` DESC"
)


def fake_workers_ai(payload):
    """模拟 /api/llm：带工具的请求提交 SQL，不带工具的请求返回解读。"""
    if payload.get("tools"):
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "submit_sql",
                        "arguments": json.dumps(
                            {"sql": AGENT_SQL, "assumptions": ["按登记地统计"]}
                        ),
                    },
                }
            ],
        }
    else:
        message = {"role": "assistant", "content": "- 已统计"}
    return {"model": "@cf/test", "choices": [{"message": message}], "usage": {"prompt_tokens": 10}}


def test_llm_mode_answers_long_tail_questions_with_the_sql_agent(demo_db_path):
    engine = WebEngine(str(demo_db_path), llm_transport=fake_workers_ai, llm_model="@cf/test")
    events = []
    try:
        assert json.loads(engine.info())["modes"] == ["semantic", "auto"]
        semantic = json.loads(engine.ask(LONG_TAIL, "s1", mode="semantic"))
        auto = json.loads(engine.ask(LONG_TAIL, "a1", events.append, mode="auto"))
    finally:
        engine.close()

    assert semantic["status"] == "declined"
    assert auto["status"] == "answered"
    assert auto["planner"] == "llm"
    assert auto["agent_steps"][0]["tool"] == "submit_sql"
    assert any(json.loads(e)["type"] == "agent_step" for e in events)


def test_semantic_questions_do_not_call_the_model_in_auto_mode(demo_db_path):
    calls = []

    def counting(payload):
        calls.append(payload)
        return fake_workers_ai(payload)

    engine = WebEngine(str(demo_db_path), llm_transport=counting)
    try:
        result = json.loads(engine.ask("资质状态分布", "a2", mode="auto"))
    finally:
        engine.close()

    assert result["planner"] == "semantic"
    assert calls == []


def test_uploaded_files_become_a_queryable_dataset(demo_db_path, tmp_path):
    from text2sql.datasets.samples import write_retail_sample

    sql = "SELECT `城市`, SUM(`销售额`) AS `销售额合计` FROM `门店销售` GROUP BY `城市`"

    def transport(payload):
        if payload.get("tools"):
            call = {
                "id": "c1",
                "type": "function",
                "function": {"name": "submit_sql", "arguments": json.dumps({"sql": sql})},
            }
            return {"choices": [{"message": {"content": "", "tool_calls": [call]}}]}
        return {"choices": [{"message": {"content": "- 已汇总"}}]}

    files = write_retail_sample(tmp_path / "in")
    engine = WebEngine(str(demo_db_path), llm_transport=transport, upload_dir=str(tmp_path / "up"))
    try:
        summary = json.loads(engine.load_upload([[p.name, str(p)] for p in files]))
        result = json.loads(engine.ask("哪个城市销售额最高", "u1", mode="workspace"))
        again = json.loads(engine.load_upload([[files[0].name, str(files[0])]]))
    finally:
        engine.close()

    assert {t["name"] for t in summary["tables"]} == {"门店", "门店销售"}
    assert summary["suggestions"]
    assert result["status"] == "answered" and result["rows"]
    assert [t["name"] for t in again["tables"]] == ["门店"]


def test_upload_without_model_reports_why(demo_db_path, tmp_path):
    path = tmp_path / "a.csv"
    path.write_text("城市,金额\n广州,1\n", encoding="utf-8")
    engine = WebEngine(str(demo_db_path), upload_dir=str(tmp_path / "up"))
    try:
        summary = json.loads(engine.load_upload([[path.name, str(path)]]))
        result = json.loads(engine.ask("广州的金额", "u2", mode="workspace"))
        broken = json.loads(engine.load_upload([["x.docx", str(path)]]))
    finally:
        engine.close()

    assert summary["tables"][0]["rows"] == 1
    assert result["status"] == "declined"
    assert "模型" in result["message"]
    assert "docx" in broken["error"]


RETAIL_SQL = "SELECT `城市`, SUM(`销售额`) AS `销售额合计` FROM `门店销售` GROUP BY `城市` ORDER BY `销售额合计` DESC"


def recording_transport(prompts):
    """记录每次带工具请求的系统提示词，并提交固定 SQL。"""

    def transport(payload):
        if payload.get("tools"):
            prompts.append(payload["messages"][0]["content"])
            arguments = json.dumps({"sql": RETAIL_SQL, "assumptions": ["按门店所在城市汇总"]})
            call = {
                "id": "c1",
                "type": "function",
                "function": {"name": "submit_sql", "arguments": arguments},
            }
            return {"choices": [{"message": {"content": "", "tool_calls": [call]}}]}
        return {"choices": [{"message": {"content": "- 已汇总"}}]}

    return transport


@pytest.fixture
def retail_files(tmp_path):
    from text2sql.datasets.samples import write_retail_sample

    return [[p.name, str(p)] for p in write_retail_sample(tmp_path / "in")]


def test_builtin_dataset_loads_with_its_card_and_suggests_follow_ups(
    demo_db_path, tmp_path, retail_files
):
    from text2sql.datasets.cards import load_card

    engine = WebEngine(
        str(demo_db_path), llm_transport=recording_transport([]), upload_dir=str(tmp_path / "w")
    )
    try:
        summary = json.loads(engine.load_dataset("retail", retail_files))
        result = json.loads(engine.ask("按城市统计销售额的合计", "w1", mode="workspace"))
        missing = json.loads(engine.load_dataset("nope", retail_files))
    finally:
        engine.close()

    card = load_card("retail")
    assert summary["dataset"]["title"] == card.title
    assert summary["dataset"]["relationships"] == list(card.relationships)
    assert summary["suggestions"] == list(card.suggestions)
    assert {t["name"]: t["label"] for t in summary["tables"]} == {
        "门店": "门店",
        "门店销售": "门店销售",
    }
    assert result["status"] == "answered"
    assert result["suggestions"] and "按城市统计销售额的合计" not in result["suggestions"]
    assert "nope" in missing["error"]


def test_run_sql_goes_through_the_guard_and_recommends_a_chart(engine):
    answered = json.loads(engine.ask("各城市有融资记录的企业有多少家", "r1"))

    rerun = json.loads(engine.run_sql(answered["safe_sql"]))
    blocked = json.loads(engine.run_sql("DELETE FROM `企业基本信息`"))
    unknown = json.loads(engine.run_sql("SELECT `不存在的列` FROM `企业基本信息`"))

    assert rerun["status"] == "answered"
    assert rerun["rows"] == answered["rows"] and rerun["chart"]["type"] == answered["chart"]["type"]
    assert rerun["guard"]["is_safe"] and rerun["answer"]
    assert blocked["status"] == "rejected" and blocked["message"]
    assert unknown["status"] == "rejected" and "不存在的列" in unknown["message"]


def test_confirmed_answers_become_exemplars_for_similar_questions(
    demo_db_path, tmp_path, retail_files
):
    prompts = []
    engine = WebEngine(
        str(demo_db_path),
        llm_transport=recording_transport(prompts),
        upload_dir=str(tmp_path / "w"),
    )
    try:
        engine.load_dataset("retail", retail_files)
        engine.ask("按城市统计销售额的合计", "e1", mode="workspace")
        saved = json.loads(engine.remember("按城市统计销售额的合计", RETAIL_SQL, "workspace"))
        refused = json.loads(engine.remember("删掉门店", "DELETE FROM `门店`", "workspace"))
        second = json.loads(engine.ask("按城市统计销售额", "e2", mode="workspace"))
    finally:
        engine.close()

    assert saved == {"ok": True, "exemplars": 1}
    assert refused["error"]
    assert "本题没有相似示例" in prompts[0]
    assert "问题：按城市统计销售额的合计" in prompts[1] and RETAIL_SQL in prompts[1]
    plan = next(entry for entry in second["trace"] if entry["node"] == "plan")
    assert plan["detail"]["exemplars"] == ["confirmed-1"]


def test_values_mentioned_in_the_question_are_listed_in_the_prompt(
    demo_db_path, tmp_path, retail_files
):
    prompts = []
    engine = WebEngine(
        str(demo_db_path),
        llm_transport=recording_transport(prompts),
        upload_dir=str(tmp_path / "w"),
    )
    try:
        engine.load_dataset("retail", retail_files)
        engine.ask("广州各品类的销售额", "m1", mode="workspace")
    finally:
        engine.close()

    assert "问题中出现的真实取值" in prompts[0]
    assert "`门店销售`.城市 = '广州'" in prompts[0]


def test_evaluate_reproduces_offline_metrics_with_progress(engine):
    progress = []

    report = json.loads(engine.evaluate(progress.append))

    assert report["summary"]["precision"] == 1.0
    assert report["summary"]["total"] == len(report["outcomes"]) == len(progress)
