from __future__ import annotations

import pytest

from tests.semantic_cases import PARSE_CASES
from text2sql.db.schema import load_znjz_schema
from text2sql.semantic.catalog import load_znjz_catalog
from text2sql.semantic.compiler import compile_plan
from text2sql.semantic.parser import SemanticParser
from text2sql.sql.guard import SQLGuard

CATALOG = load_znjz_catalog()
PARSER = SemanticParser(CATALOG)
GUARD = SQLGuard.from_catalog(CATALOG, load_znjz_schema(), max_rows=500)


def compiled(question: str):
    result = PARSER.parse(question)
    assert result.plan is not None, (question, result.reason, result.unexplained)
    return compile_plan(result.plan, CATALOG, max_rows=500)


@pytest.mark.parametrize("question", [q for q, _ in PARSE_CASES])
def test_every_parsed_plan_compiles_passes_guard_and_executes(question, demo_backend):
    query = compiled(question)
    report = GUARD.check(query.sql)

    assert report.is_safe, (question, report.errors, query.sql)
    result = demo_backend.execute(report.safe_sql, max_rows=500)
    assert result.columns == query.columns


def test_enterprise_count_inside_financing_entity_counts_distinct_companies():
    sql = compiled("各融资轮次的企业数量").sql

    assert "COUNT(DISTINCT f.`eid`) AS `企业数量`" in sql
    assert "FROM `融资数据` f" in sql
    assert "GROUP BY f.`round`" in sql


def test_value_filters_use_semantic_layer_predicates():
    sql = compiled("广州市存续企业有多少家").sql

    assert "e.`status` LIKE '存续%'" in sql
    assert "e.`district_code` LIKE '4401%'" in sql


def test_time_constraint_is_compiled_inside_existence_subquery():
    sql = compiled("2020年以来有融资记录的企业数量").sql

    assert (
        "EXISTS (SELECT 1 FROM `融资数据` fx WHERE fx.`eid` = e.`eid` AND YEAR(fx.`round_date`) >= 2020)"
        in sql
    )


def test_trend_orders_by_time_ascending():
    sql = compiled("按成立年份统计企业数量趋势").sql
    assert "ORDER BY `成立年份` ASC" in sql


def test_ranking_orders_by_metric_with_deterministic_tie_breaker():
    sql = compiled("按行业统计企业数量 Top 10").sql

    assert "ORDER BY `企业数量` DESC, `行业代码` ASC" in sql
    assert sql.rstrip().endswith("LIMIT 10")


def test_share_uses_window_over_the_aggregate():
    sql = compiled("佛山市各企业类型的企业数量占比").sql
    assert "SUM(COUNT(DISTINCT e.`eid`)) OVER ()" in sql


def test_fact_entity_joins_enterprise_only_when_needed():
    by_round = compiled("各融资轮次的企业数量").sql
    by_company = compiled("统计对外投资数量最多的企业 Top 10").sql

    assert "JOIN `企业基本信息`" not in by_round
    assert "JOIN `企业基本信息` e ON e.`eid` = v.`eid`" in by_company
    assert "GROUP BY e.`eid`, e.`name`" in by_company


def test_event_count_label_follows_entity():
    assert "COUNT(*) AS `招投标记录数`" in compiled("各城市的招投标记录数").sql


def test_capital_bucket_is_ordered_by_bucket_lower_bound():
    sql = compiled("按注册资本区间统计企业数量").sql
    assert "ORDER BY MIN(COALESCE(e.`regist_capi_new`, -1)) ASC" in sql


def test_compiled_results_match_independent_sql(demo_backend):
    query = compiled("广州市存续企业有多少家")
    got = demo_backend.execute(GUARD.check(query.sql).safe_sql, max_rows=10).rows[0]["企业数量"]
    expected = demo_backend.execute(
        "SELECT COUNT(*) AS n FROM `企业基本信息` WHERE `status` LIKE '存续%' AND `district_code` LIKE '4401%'",
        max_rows=1,
    ).rows[0]["n"]

    assert got == expected


def test_list_results_satisfy_every_filter(demo_backend):
    query = compiled("有融资但没有招投标记录的企业有哪些")
    names = [
        row["企业名称"]
        for row in demo_backend.execute(GUARD.check(query.sql).safe_sql, max_rows=100).rows
    ]

    assert 0 < len(names) <= 20
    for name in names:
        check = demo_backend.execute(
            "SELECT "
            "EXISTS (SELECT 1 FROM `融资数据` f JOIN `企业基本信息` e ON e.eid = f.eid WHERE e.name = '"
            + name
            + "') AS has_fin, "
            "EXISTS (SELECT 1 FROM `招投标` b JOIN `企业基本信息` e ON e.eid = b.eid WHERE e.name = '"
            + name
            + "') AS has_bid",
            max_rows=1,
        ).rows[0]
        assert check == {"has_fin": True, "has_bid": False}


def test_detail_name_is_escaped_in_like_pattern():
    plan = PARSER.parse("查询“广州市云拓智能科技有限公司”的基本信息").plan
    plan.detail_name = "x' OR '1'='1"
    query = compile_plan(plan, CATALOG, max_rows=500)

    report = GUARD.check(query.sql)
    assert report.is_safe
    assert "LIKE '%x'' OR ''1''=''1%'" in query.sql


def test_description_reads_as_business_interpretation():
    query = compiled("广州市存续企业有多少家")

    assert "企业数量" in query.description
    assert "广州市" in query.description
    assert "存续" in query.description


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("2020年以来有融资记录的企业数量", "统计2020年以来有融资记录的企业：企业数量"),
        ("近三年每年的融资事件数", "按融资年份统计近3年（2024–2026）的融资事件：融资事件数"),
        ("哪家企业的招投标记录最多", "按企业名称统计招投标记录：招投标记录数，取前 1 名"),
        ("注册资本最高的10家企业", "列出企业，按注册资本从高到低，最多 10 条"),
        ("按行业统计企业数量 Top 10", "按行业代码统计企业：企业数量，取前 10 名"),
    ],
)
def test_descriptions_are_concise_and_keep_conditions(question, expected):
    assert compiled(question).description == expected
