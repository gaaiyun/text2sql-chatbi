from __future__ import annotations

import json

from text2sql.agent.prompts import (
    ConversationTurn,
    PromptContext,
    narrative_messages,
    single_shot_system_prompt,
    sql_agent_system_prompt,
    sql_agent_user_message,
)
from text2sql.semantic.catalog import load_znjz_catalog

CATALOG = load_znjz_catalog()


def context(history=None) -> PromptContext:
    return PromptContext.from_catalog(
        CATALOG,
        schema_text="### `融资数据`（视图）融资事件\n- 字段：\n  - round VARCHAR：融资轮次",
        exemplars_text="问题：各轮次数量\n```sql\nSELECT `round`, COUNT(*) FROM `融资数据` GROUP BY `round`\n```",
        max_rows=500,
        history=history or [],
    )


def test_agent_prompt_lists_tools_workflow_and_constraints():
    prompt = sql_agent_system_prompt(context())

    for tool in (
        "search_schema",
        "describe_table",
        "get_column_values",
        "validate_sql",
        "preview_sql",
        "submit_sql",
    ):
        assert tool in prompt
    assert "返回 0 行" in prompt
    assert "只写一条 SELECT" in prompt
    assert "500" in prompt
    assert "2026" in prompt


def test_agent_prompt_injects_rules_schema_and_exemplars():
    prompt = sql_agent_system_prompt(context())

    assert "存续（在营、开业、在册）" in prompt
    assert "### `融资数据`" in prompt
    assert "问题：各轮次数量" in prompt
    assert CATALOG.title in prompt


def test_single_shot_prompt_asks_for_assumption_and_code_block_only():
    prompt = single_shot_system_prompt(context())

    assert "口径：" in prompt
    assert "```sql" in prompt
    assert "preview_sql" not in prompt


def test_history_is_included_only_for_follow_up_capable_context():
    turn = ConversationTurn(
        question="广州市存续企业有多少家",
        sql="SELECT COUNT(*) FROM `企业基本信息` WHERE status LIKE '存续%'",
        description="统计存续企业、登记城市为广州市的企业：企业数量",
        assumptions=[],
    )

    with_history = sql_agent_system_prompt(context(history=[turn]))
    without = sql_agent_system_prompt(context())

    assert "上一轮问题：广州市存续企业有多少家" in with_history
    assert "status LIKE '存续%'" in with_history
    assert "上一轮问题" not in without


def test_missing_exemplars_are_marked_explicitly():
    ctx = context()
    ctx.exemplars_text = ""
    assert "（本题没有相似示例）" in sql_agent_system_prompt(ctx)


def test_user_message_carries_repair_feedback():
    assert sql_agent_user_message("各城市企业数量") == "问题：各城市企业数量"
    message = sql_agent_user_message("各城市企业数量", feedback="上一次提交的 SQL 没有通过")
    assert message.startswith("问题：各城市企业数量")
    assert "上一次提交的 SQL 没有通过" in message


def test_narrative_messages_ground_every_number_in_facts():
    facts = {"row_count": 2, "top": {"城市": "广州市", "企业数量": 120}}
    messages = narrative_messages("各城市企业数量", "按城市统计企业：企业数量", facts)

    assert messages[0]["role"] == "system"
    assert "不得编造" in messages[0]["content"]
    user = messages[1]["content"]
    assert json.dumps(facts, ensure_ascii=False, indent=2) in user
    assert "原样出现在结果事实中" in user
