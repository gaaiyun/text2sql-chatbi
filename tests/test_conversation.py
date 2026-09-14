from __future__ import annotations

import pytest

from text2sql.agent.conversation import looks_like_follow_up, rewrite_follow_up
from text2sql.semantic.catalog import load_znjz_catalog
from text2sql.semantic.parser import SemanticParser

PARSER = SemanticParser(load_znjz_catalog())


def rewrite(previous: str, question: str) -> str:
    result = rewrite_follow_up(question, previous, PARSER)
    assert result is not None, (previous, question)
    assert PARSER.parse(result.effective_question).ok, result.effective_question
    return result.effective_question


def plan_of(text: str):
    return PARSER.parse(text).plan


@pytest.mark.parametrize(
    ("previous", "question", "expected"),
    [
        ("广州市存续企业有多少家", "那深圳呢", "深圳存续企业有多少家"),
        ("各城市的企业数量", "按年份看呢", "各年份的企业数量"),
        ("2023年各月的招投标数量", "2024年呢", "2024年各月的招投标数量"),
        ("按行业统计企业数量 Top 10", "前20呢", "按行业统计企业数量前20"),
        ("各城市的企业数量", "只看存续的", "各城市的存续企业数量"),
        ("广州市存续企业有多少家", "去掉存续", "广州市企业有多少家"),
        ("各城市的企业数量", "平均注册资本呢", "各城市的平均注册资本"),
        # 新词自带分组前缀，前文也已有“各”，不能拼成“各各城市”
        ("各行业门类的平均注册资本", "那各城市呢", "各城市的平均注册资本"),
        # “每年”换成维度时要补上“按”，否则读成“近3年行业的……”
        ("近三年每年的融资事件数", "按行业统计呢", "近3年按行业的融资事件数"),
        ("近三年每年的融资事件数", "行业呢", "近3年按行业的融资事件数"),
    ],
)
def test_follow_ups_rewrite_previous_question(previous, question, expected):
    assert rewrite(previous, question) == expected


def test_follow_up_without_previous_dimension_adds_one():
    text = rewrite("广州市存续企业有多少家", "按年份看呢")
    plan = plan_of(text)

    assert plan.mode == "group"
    assert plan.dimensions == ["start_year"]
    assert sorted(f.key for f in plan.filters) == ["active", "city=4401"]


def test_location_granularity_can_change_within_group():
    plan = plan_of(rewrite("广州市存续企业有多少家", "天河区呢"))
    assert sorted(f.key for f in plan.filters) == ["active", "district=440106"]


def test_operations_are_described_for_the_user():
    result = rewrite_follow_up("那深圳呢", "广州市存续企业有多少家", PARSER)
    assert result.operations == ["把「广州市」换成「深圳」"]


def test_unexplained_follow_up_is_not_rewritten():
    assert rewrite_follow_up("那专利呢", "广州市存续企业有多少家", PARSER) is None


def test_standalone_question_is_not_treated_as_follow_up():
    assert not looks_like_follow_up("各融资轮次的企业数量", PARSER)
    assert looks_like_follow_up("那深圳呢", PARSER)
    assert looks_like_follow_up("按年份看呢", PARSER)
    assert looks_like_follow_up("2024年呢", PARSER)
