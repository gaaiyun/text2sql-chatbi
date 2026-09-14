from __future__ import annotations

import json

import pytest

from text2sql.agent.linking import SchemaLinker
from text2sql.agent.llm import LLMResponse, ScriptedLLM, ToolsNotSupportedError, tool_call_response
from text2sql.agent.prompts import PromptContext
from text2sql.agent.sql_agent import SQLAgent
from text2sql.agent.tools import AgentToolbox
from text2sql.agent.values import build_value_index
from text2sql.db.schema import load_znjz_schema
from text2sql.semantic.catalog import load_znjz_catalog
from text2sql.sql.guard import SQLGuard

CATALOG = load_znjz_catalog()
SCHEMA = load_znjz_schema()
CONTEXT = PromptContext.from_catalog(CATALOG, schema_text="（测试用表结构）")


@pytest.fixture(scope="module")
def toolbox(demo_backend):
    return AgentToolbox(
        catalog=CATALOG,
        schema=SCHEMA,
        linker=SchemaLinker(CATALOG, SCHEMA),
        guard=SQLGuard.from_catalog(CATALOG, SCHEMA),
        backend=demo_backend,
        values=build_value_index(demo_backend, CATALOG),
    )


def tool_messages(call):
    return [json.loads(m["content"]) for m in call["messages"] if m["role"] == "tool"]


def test_multi_step_tool_use_ends_with_submitted_sql(toolbox):
    final_sql = "SELECT COUNT(DISTINCT eid) AS n FROM `企业基本信息` WHERE status LIKE '存续%'"
    llm = ScriptedLLM(
        [
            tool_call_response("get_column_values", {"table": "企业基本信息", "column": "status"}),
            tool_call_response("preview_sql", {"sql": final_sql}),
            tool_call_response(
                "submit_sql", {"sql": final_sql, "assumptions": ["存续按 status 前缀匹配"]}
            ),
        ]
    )

    draft = SQLAgent(llm, toolbox).draft("存续企业有多少家", system_prompt="system")

    assert draft.sql == final_sql
    assert draft.assumptions == ["存续按 status 前缀匹配"]
    assert [step.tool for step in draft.steps] == ["get_column_values", "preview_sql", "submit_sql"]
    assert draft.mode == "tool_calling"
    assert draft.usage.calls == 3
    # 第三次调用时，模型已经看到前两次工具结果
    observed = tool_messages(llm.calls[2])
    assert observed[0]["values"][0][0] == "存续（在营、开业、在册）"
    assert observed[1]["rows"][0]["n"] > 0


def test_agent_self_corrects_after_empty_preview_diagnosis(toolbox):
    wrong = "SELECT COUNT(*) AS n FROM `企业基本信息` WHERE status = '存续'"
    right = "SELECT COUNT(*) AS n FROM `企业基本信息` WHERE status LIKE '存续%'"

    def handler(messages, tools):
        tool_results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not tool_results:
            return tool_call_response("preview_sql", {"sql": wrong})
        if "hints" in tool_results[-1] and len(tool_results) == 1:
            assert "存续（在营、开业、在册）" in " ".join(tool_results[-1]["hints"])
            return tool_call_response("preview_sql", {"sql": right})
        return tool_call_response("submit_sql", {"sql": right})

    draft = SQLAgent(ScriptedLLM(handler=handler), toolbox).draft(
        "存续企业有多少家", system_prompt="system"
    )

    assert draft.sql == right
    assert "取值可能写错" in draft.steps[0].summary


def test_assumptions_given_as_one_string_are_split_into_items(toolbox):
    sql = "SELECT COUNT(*) AS n FROM `企业基本信息`"
    llm = ScriptedLLM(
        [
            tool_call_response(
                "submit_sql", {"sql": sql, "assumptions": "1. 统计全部企业\n2. 不去重"}
            )
        ]
    )

    draft = SQLAgent(llm, toolbox).draft("企业总数", system_prompt="system")

    assert draft.assumptions == ["统计全部企业", "不去重"]


def test_rejected_submission_is_returned_to_the_model(toolbox):
    llm = ScriptedLLM(
        [
            tool_call_response("submit_sql", {"sql": "SELECT industry_name FROM `企业行业代码`"}),
            tool_call_response(
                "submit_sql", {"sql": "SELECT industry_code FROM `企业行业代码` LIMIT 5"}
            ),
        ]
    )

    draft = SQLAgent(llm, toolbox).draft("行业代码", system_prompt="system")

    assert draft.sql == "SELECT industry_code FROM `企业行业代码` LIMIT 5"
    assert draft.steps[0].ok is False
    rejection = tool_messages(llm.calls[1])[0]
    assert any("industry_code" in hint for hint in rejection["hints"])


def test_plain_text_sql_answer_is_accepted(toolbox):
    llm = ScriptedLLM(["口径：全部企业\n```sql\nSELECT COUNT(*) AS n FROM `企业基本信息`\n```"])

    draft = SQLAgent(llm, toolbox).draft("企业总数", system_prompt="system")

    assert draft.sql == "SELECT COUNT(*) AS n FROM `企业基本信息`"
    assert draft.assumptions == ["全部企业"]


def test_model_is_nudged_once_when_it_answers_without_sql(toolbox):
    llm = ScriptedLLM(
        [
            "我先想一想",
            tool_call_response("submit_sql", {"sql": "SELECT COUNT(*) AS n FROM `招投标`"}),
        ]
    )

    draft = SQLAgent(llm, toolbox).draft("招投标记录数", system_prompt="system")

    assert draft.sql is not None
    assert "submit_sql" in llm.calls[1]["messages"][-1]["content"]


def test_step_budget_is_enforced(toolbox):
    llm = ScriptedLLM(
        handler=lambda m, t: tool_call_response("search_schema", {"keywords": "融资"})
    )

    draft = SQLAgent(llm, toolbox, max_steps=3).draft("融资", system_prompt="system")

    assert draft.sql is None
    assert "3" in draft.error
    assert len(llm.calls) == 3


def test_last_call_is_nudged_to_submit(toolbox):
    llm = ScriptedLLM(
        handler=lambda m, t: tool_call_response("search_schema", {"keywords": "融资"})
    )

    SQLAgent(llm, toolbox, max_steps=3).draft("融资", system_prompt="system")

    assert "submit_sql" not in llm.calls[1]["messages"][-1].get("content", "")
    last = llm.calls[2]["messages"][-1]
    assert (
        last["role"] == "user" and "最后一次" in last["content"] and "submit_sql" in last["content"]
    )


def test_exhausted_budget_falls_back_to_the_last_successful_preview(toolbox):
    previewed = "SELECT COUNT(*) AS n FROM `企业基本信息` WHERE status LIKE '存续%'"
    replies = iter(
        [
            tool_call_response("preview_sql", {"sql": previewed}),
            tool_call_response("search_schema", {"keywords": "企业"}),
            tool_call_response("search_schema", {"keywords": "状态"}),
        ]
    )

    draft = SQLAgent(ScriptedLLM(handler=lambda m, t: next(replies)), toolbox, max_steps=3).draft(
        "存续企业有多少家", system_prompt="system"
    )

    assert draft.sql == previewed and draft.error is None
    assert "最后一次试运行成功" in draft.assumptions[0]
    assert draft.steps[-1].tool == "submit_sql" and draft.steps[-1].arguments["auto"] is True


def test_empty_previews_are_not_used_as_fallback(toolbox):
    empty = "SELECT COUNT(*) AS n FROM `企业基本信息` WHERE status = '不存在的状态'"
    replies = iter(
        [
            tool_call_response("preview_sql", {"sql": empty}),
            tool_call_response("search_schema", {"keywords": "企业"}),
        ]
    )

    draft = SQLAgent(ScriptedLLM(handler=lambda m, t: next(replies)), toolbox, max_steps=2).draft(
        "某状态企业有多少家", system_prompt="system"
    )

    assert draft.sql is None and "2" in draft.error


def test_provider_without_tools_falls_back_to_single_shot(toolbox):
    llm = ScriptedLLM(
        [
            ToolsNotSupportedError("tools unsupported"),
            "口径：按融资轮次分组\n```sql\nSELECT `round`, COUNT(*) AS n FROM `融资数据` GROUP BY `round`\n```",
        ]
    )

    draft = SQLAgent(llm, toolbox).draft(
        "各轮次融资数", system_prompt="agent", fallback_prompt="single"
    )

    assert draft.mode == "single_shot"
    assert draft.sql.startswith("SELECT `round`")
    assert llm.calls[1]["tools"] is None
    assert llm.calls[1]["messages"][0]["content"] == "single"


def test_on_step_callback_streams_each_tool_call(toolbox):
    seen = []
    llm = ScriptedLLM(
        [
            tool_call_response("search_schema", {"keywords": "资质"}),
            tool_call_response("submit_sql", {"sql": "SELECT COUNT(*) AS n FROM `标签数据`"}),
        ]
    )

    SQLAgent(llm, toolbox).draft("资质数量", system_prompt="system", on_step=seen.append)

    assert [step.tool for step in seen] == ["search_schema", "submit_sql"]


def test_feedback_from_previous_attempt_is_sent_as_user_message(toolbox):
    llm = ScriptedLLM(
        [tool_call_response("submit_sql", {"sql": "SELECT COUNT(*) AS n FROM `标签数据`"})]
    )

    SQLAgent(llm, toolbox).draft(
        "资质数量", system_prompt="system", feedback="上一次提交的 SQL 没有通过：超时"
    )

    assert "上一次提交的 SQL 没有通过：超时" in llm.calls[0]["messages"][1]["content"]


def test_large_tool_results_are_truncated_before_returning_to_model(toolbox):
    class Huge:
        names = ("search_schema",)
        guard = toolbox.guard

        def execute(self, name, arguments):
            from text2sql.agent.tools import ToolResult

            return ToolResult(True, {"blob": "x" * 50_000}, "huge")

    llm = ScriptedLLM(
        [
            tool_call_response("search_schema", {"keywords": "x"}),
            tool_call_response("submit_sql", {"sql": "SELECT COUNT(*) AS n FROM `标签数据`"}),
        ]
    )
    SQLAgent(llm, Huge()).draft("x", system_prompt="system")

    tool_content = next(m["content"] for m in llm.calls[1]["messages"] if m["role"] == "tool")
    assert len(tool_content) <= SQLAgent.max_tool_chars + 50


def test_unknown_tool_name_gets_error_result(toolbox):
    llm = ScriptedLLM(
        [
            LLMResponse(tool_calls=tool_call_response("hack", {}).tool_calls),
            tool_call_response("submit_sql", {"sql": "SELECT COUNT(*) AS n FROM `标签数据`"}),
        ]
    )

    draft = SQLAgent(llm, toolbox).draft("x", system_prompt="system")

    assert draft.steps[0].ok is False
    assert "未知工具" in tool_messages(llm.calls[1])[0]["error"]
