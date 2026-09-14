from __future__ import annotations

import pytest

from tests.semantic_cases import PARSE_CASES
from text2sql.semantic.catalog import load_znjz_catalog
from text2sql.semantic.parser import SemanticParser, normalize_question


@pytest.fixture(scope="module")
def parser() -> SemanticParser:
    return SemanticParser(load_znjz_catalog())


def summary(result):
    plan = result.plan
    assert plan is not None, (result.reason, result.unexplained)
    return {
        "entity": plan.entity,
        "mode": plan.mode,
        "metrics": plan.metrics,
        "dimensions": plan.dimensions,
        "filters": sorted(f.key for f in plan.filters),
        "limit": plan.limit,
        "order": plan.order,
        "share": plan.share,
    }


@pytest.mark.parametrize(("question", "expected"), PARSE_CASES)
def test_supported_questions_parse_into_expected_plans(parser, question, expected):
    got = summary(parser.parse(question))
    for key, value in expected.items():
        assert got[key] == value, (question, key, got)


@pytest.mark.parametrize(
    ("question", "fragment"),
    [
        ("有融资但没有专利的企业", "专利"),
        ("查询所有企业的法定代表人姓名", "法定代表人"),
        ("今天天气怎么样", "天气"),
        ("帮我写一首关于企业的诗", "诗"),
    ],
)
def test_unexplained_terms_make_the_parser_decline(parser, question, fragment):
    result = parser.parse(question)

    assert result.plan is None
    assert any(fragment in term for term in result.unexplained), result.unexplained


@pytest.mark.parametrize(
    "question",
    [
        "按行业统计融资金额和招投标数量",  # 同时涉及两个事件表
        "各融资轮次企业的平均注册资本",  # 指标在该实体下没有安全口径
        "查询企业的基本信息",  # 详情类问题没有指定企业
        "企业",  # 没有可统计的内容
        "每个城市招投标记录最多的企业分别是哪家",  # 分组内取排名需要窗口函数
        "各城市注册资本最高的企业",  # 同上：不能退化成“全局注册资本最高”
        "中标最多的20家企业里有几家获得过融资",  # 按企业分组再数企业数恒为 1，需要子查询
        "注册资本最高的10家企业中有多少家是存续状态",  # 排序名单里再计数，名单模式会丢掉计数
    ],
)
def test_questions_outside_semantic_layer_are_declined_with_reason(parser, question):
    result = parser.parse(question)

    assert result.plan is None
    assert result.reason


def test_top_group_question_without_company_word_is_still_supported(parser):
    plan = parser.parse("哪个城市的招投标记录最多").plan

    assert plan.dimensions == ["bid_city"]
    assert plan.limit == 1


def test_negation_is_never_dropped(parser):
    """否定词不能被当成停用词吞掉，否则“没有融资”会被理解成“有融资”。"""
    result = parser.parse("没有获得过融资的企业数量")

    assert [f.key for f in result.plan.filters] == ["no_financing"]


def test_time_phrase_attaches_to_adjacent_existence_filter(parser):
    plan = parser.parse("2020年以来有融资记录的企业数量").plan
    financing = next(f for f in plan.filters if f.key == "has_financing")

    assert "YEAR(fx.`round_date`) >= 2020" in financing.sql
    assert financing.label == "2020年以来有融资记录"
    assert any("2020" in note for note in plan.assumptions)


def test_time_phrase_without_existence_filter_uses_entity_time_dimension(parser):
    plan = parser.parse("2023年成立的企业数量").plan

    assert [f.key for f in plan.filters] == ["start_year=2023"]


def test_relative_years_use_catalog_anchor(parser):
    plan = parser.parse("近三年每年的融资事件数").plan

    assert any("2024" in note and "2026" in note for note in plan.assumptions)


def test_scoped_words_resolve_by_entity(parser):
    assert parser.parse("企业的状态分布").plan.dimensions == ["status"]
    assert parser.parse("资质的状态分布").plan.dimensions == ["qual_state"]
    assert parser.parse("各城市企业数量").plan.dimensions == ["city"]


def test_assumptions_are_disclosed(parser):
    plan = parser.parse("各城市的招投标记录数").plan
    notes = " ".join(plan.assumptions)

    assert "项目地区" in notes
    assert "中标" in notes


def test_company_name_triggers_detail_mode(parser):
    result = parser.parse("查询“广州瑞煜数据科技有限公司”的基本信息")

    assert result.plan.mode == "detail"
    assert result.plan.detail_name == "广州瑞煜数据科技有限公司"


def test_matches_are_reported_for_trace(parser):
    result = parser.parse("广州市存续企业有多少家")
    kinds = {(m.kind, m.surface) for m in result.matches}

    assert ("value", "广州市") in kinds
    assert ("filter", "存续") in kinds


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("按行业统计企业数量 Top 10", "按行业统计企业数量top10"),
        ("近三年", "近3年"),
        ("前二十名", "前20名"),
        ("成立超过十五年", "成立超过15年"),
        ("１２３ＡＢＣ", "123abc"),
    ],
)
def test_normalize_question(raw, expected):
    assert normalize_question(raw) == expected
