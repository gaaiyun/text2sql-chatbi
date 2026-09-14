from __future__ import annotations

import pytest

from text2sql.agent.values import build_value_index
from text2sql.semantic.catalog import load_znjz_catalog

CATALOG = load_znjz_catalog()


@pytest.fixture(scope="module")
def index(demo_backend):
    return build_value_index(demo_backend, CATALOG, per_column_limit=30)


def test_status_values_are_scanned_from_the_live_database(index):
    column = index.get("企业基本信息", "status")

    assert column.values[0][0] == "存续（在营、开业、在册）"
    assert column.complete is True


def test_known_values_and_unscanned_columns(index):
    assert "天使轮" in index.known("融资数据", "round")
    assert index.known("招投标", "title") is None


def test_contains_distinguishes_absent_from_unknown(index):
    assert index.contains("企业基本信息", "status", "存续（在营、开业、在册）") is True
    assert index.contains("企业基本信息", "status", "存续") is False
    assert index.contains("招投标", "title", "任何标题") is None


def test_numeric_columns_compare_numerically(index):
    assert index.contains("招投标", "notice_type_main", "30") is True
    assert index.contains("招投标", "notice_type_main", 30.0) is True
    assert index.contains("招投标", "notice_type_main", "99") is False


def test_like_patterns_are_checked_against_known_values(index):
    assert index.matches_like("企业基本信息", "status", "存续%") is True
    assert index.matches_like("企业基本信息", "status", "%倒闭%") is False


def test_incomplete_columns_never_claim_absence(demo_backend):
    partial = build_value_index(demo_backend, CATALOG, per_column_limit=2)
    column = partial.get("企业基本信息", "status")

    assert column.complete is False
    assert partial.contains("企业基本信息", "status", "从未出现过的状态") is None


def test_mentions_find_literal_values_in_question(index):
    mentions = index.mentions("天使轮融资的企业有多少家")
    assert ("融资数据", "round", "天使轮") in mentions


def test_hints_render_values_with_frequency_for_prompt(index):
    hints = "\n".join(index.hints_for(["融资数据", "企业基本信息"]))

    assert "`融资数据`.round" in hints
    assert "'天使轮'" in hints
    assert "存续（在营、开业、在册）" in hints
    assert "30.0" not in index_hint_for_notice(index)


def index_hint_for_notice(index):
    return "\n".join(index.hints_for(["招投标"]))


def test_time_budget_and_backend_errors_degrade_gracefully():
    class Broken:
        def distinct_values(self, table, column, *, limit=30):
            raise RuntimeError("connection lost")

    broken = build_value_index(Broken(), CATALOG)
    assert broken.columns == {}
    assert broken.skipped

    class Slow:
        def distinct_values(self, table, column, *, limit=30):
            return [("x", 1)]

    budgeted = build_value_index(Slow(), CATALOG, time_budget_s=0)
    assert budgeted.columns == {}
    assert any("时间预算" in s for s in budgeted.skipped)
