from __future__ import annotations

import json

import pytest

from text2sql.agent.llm import ScriptedLLM
from text2sql.agent.narrative import deterministic_narrative, llm_narrative
from text2sql.agent.profiling import profile_result
from text2sql.agent.reflection import check_numbers, extract_numbers, reflect
from text2sql.semantic.catalog import load_znjz_catalog

CATALOG = load_znjz_catalog()
CITY_ROWS = [
    {"城市": "广州市", "企业数量": 50},
    {"城市": "深圳市", "企业数量": 30},
    {"城市": "佛山市", "企业数量": 10},
    {"城市": "东莞市", "企业数量": 6},
    {"城市": "珠海市", "企业数量": 4},
]
CITY_PROFILE = profile_result(["城市", "企业数量"], CITY_ROWS)


def test_deterministic_narrative_lists_facts_as_bullets():
    text = deterministic_narrative(CITY_PROFILE)

    assert text.startswith("- 共 5 个城市")
    assert "「广州市」的企业数量最高，为 50" in text


def test_deterministic_narrative_for_empty_result_says_no_data():
    text = deterministic_narrative(profile_result(["城市", "企业数量"], []))
    assert "没有返回数据" in text


def test_llm_narrative_sends_facts_and_returns_usage():
    llm = ScriptedLLM(["- 广州市企业最多，为 50 家"])

    text, usage = llm_narrative(
        llm, question="各城市企业数量", description="按城市统计企业：企业数量", profile=CITY_PROFILE
    )

    assert text == "- 广州市企业最多，为 50 家"
    assert usage.calls == 1
    assert "「广州市」的企业数量最高" in llm.calls[0]["messages"][1]["content"]


def test_extract_numbers_handles_separators_percent_and_sign():
    assert extract_numbers("共 5,198 条，占 33.5%，较上年 -2，A轮 3 笔") == [
        5198.0,
        33.5,
        -2.0,
        3.0,
    ]


def test_grounded_numbers_pass():
    grounded, ungrounded = check_numbers(
        "- 广州市最多，为 50 家，占合计 100 的 50%\n- 前 3 个城市合计占 90%",
        profile=CITY_PROFILE,
        rows=CITY_ROWS,
        question="各城市企业数量",
    )

    assert ungrounded == []
    assert "50" in grounded and "90" in grounded


def test_invented_numbers_are_caught():
    _, ungrounded = check_numbers(
        "- 广州市为 52 家，同比增长 18.5%",
        profile=CITY_PROFILE,
        rows=CITY_ROWS,
        question="各城市企业数量",
    )
    assert ungrounded == ["52", "18.5"]


def test_small_ordinals_and_question_numbers_are_allowed():
    _, ungrounded = check_numbers(
        "- 2023 年前 3 个城市占比较高",
        profile=CITY_PROFILE,
        rows=CITY_ROWS,
        question="2023年各城市企业数量",
    )
    assert ungrounded == []


def base_kwargs(**overrides):
    kwargs = dict(
        question="各城市企业数量",
        task="distribution",
        top_n=None,
        sql="SELECT city, COUNT(*) FROM `企业基本信息` GROUP BY city",
        rows=CITY_ROWS,
        truncated=False,
        max_rows=500,
        profile=CITY_PROFILE,
        narrative_source="deterministic",
        ungrounded_numbers=[],
        assumptions=["城市按登记地"],
        planner="semantic",
        catalog=CATALOG,
    )
    kwargs.update(overrides)
    return kwargs


def statuses(report):
    return {c.name: c.status for c in report.checks}


def test_clean_result_scores_full_marks():
    report = reflect(**base_kwargs())

    assert report.score == 1.0
    assert report.ok
    assert set(statuses(report).values()) == {"pass"}


def test_empty_result_is_flagged_but_not_failed():
    report = reflect(**base_kwargs(rows=[], profile=profile_result(["城市", "企业数量"], [])))
    assert statuses(report)["result_present"] == "warn"


def test_rejected_llm_narrative_is_disclosed():
    report = reflect(**base_kwargs(narrative_source="llm_rejected", ungrounded_numbers=["52"]))

    check = next(c for c in report.checks if c.name == "numbers_grounded")
    assert check.status == "warn"
    assert "52" in check.detail and "确定性" in check.detail


def test_truncation_and_top_n_consistency():
    rows = [{"城市": f"城市{i}", "企业数量": 100 - i} for i in range(500)]
    profile = profile_result(["城市", "企业数量"], rows, truncated=True)
    report = reflect(
        **base_kwargs(rows=rows, profile=profile, truncated=True, top_n=10, task="ranking")
    )

    assert statuses(report)["truncation"] == "warn"
    assert statuses(report)["top_n"] == "warn"


def test_ranking_without_order_by_is_flagged():
    report = reflect(
        **base_kwargs(
            task="ranking",
            top_n=5,
            rows=CITY_ROWS,
            sql="SELECT city, COUNT(*) AS n FROM `企业基本信息` GROUP BY city LIMIT 5",
        )
    )
    assert statuses(report)["top_n"] == "warn"


def test_unordered_trend_is_flagged():
    rows = [{"年份": 2025, "数量": 3}, {"年份": 2023, "数量": 1}, {"年份": 2024, "数量": 2}]
    report = reflect(
        **base_kwargs(task="trend", rows=rows, profile=profile_result(["年份", "数量"], rows))
    )
    assert statuses(report)["time_order"] == "warn"


@pytest.mark.parametrize(
    ("sql", "status"),
    [
        ("SELECT COUNT(DISTINCT cf_eid) FROM `企业融资信息`", "warn"),
        ("SELECT COUNT(DISTINCT cf_eid) FROM `企业融资信息` WHERE cf_id IS NOT NULL", "pass"),
        ("SELECT COUNT(*) FROM `融资数据`", "pass"),
    ],
)
def test_wide_table_placeholder_rows_are_detected(sql, status):
    assert statuses(reflect(**base_kwargs(sql=sql)))["wide_table"] == status


def test_null_dimension_values_are_flagged():
    rows = [{"行业代码": None, "企业数量": 2}, {"行业代码": "I6510", "企业数量": 40}]
    report = reflect(
        **base_kwargs(rows=rows, profile=profile_result(["行业代码", "企业数量"], rows))
    )
    assert statuses(report)["null_dimension"] == "warn"


def test_llm_sql_without_assumptions_is_flagged():
    report = reflect(**base_kwargs(planner="llm", assumptions=[]))
    assert statuses(report)["assumptions"] == "warn"


def test_score_averages_pass_and_warn():
    report = reflect(**base_kwargs(planner="llm", assumptions=[]))

    passes = sum(1 for c in report.checks if c.status == "pass")
    assert report.score == pytest.approx((passes + 0.5) / len(report.checks), abs=1e-3)
    json.dumps(report.to_dict(), ensure_ascii=False)
