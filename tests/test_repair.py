from __future__ import annotations

import pytest

from text2sql.agent.repair import (
    Diagnosis,
    diagnose_cost,
    diagnose_empty_result,
    diagnose_execution_error,
    diagnose_guard,
)
from text2sql.agent.values import build_value_index
from text2sql.db.backends import CostEstimate, QueryExecutionError, QueryTimeoutError
from text2sql.db.schema import load_znjz_schema
from text2sql.semantic.catalog import load_znjz_catalog
from text2sql.sql.guard import SQLGuard

CATALOG = load_znjz_catalog()
SCHEMA = load_znjz_schema()
GUARD = SQLGuard.from_catalog(CATALOG, SCHEMA)


@pytest.fixture(scope="module")
def values(demo_backend):
    return build_value_index(demo_backend, CATALOG)


def test_guard_rejections_that_a_model_can_fix_are_repairable():
    diagnosis = diagnose_guard(GUARD.check("SELECT industry_name FROM `企业行业代码`"))

    assert diagnosis.code == "unknown_column"
    assert diagnosis.repairable is True
    assert any("industry_code" in hint for hint in diagnosis.hints)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT oper_name FROM `企业基本信息`",
        "SELECT SLEEP(5)",
        "DELETE FROM `企业基本信息`",
        "SELECT user FROM mysql.user",
    ],
)
def test_policy_rejections_are_not_sent_back_for_repair(sql):
    assert diagnose_guard(GUARD.check(sql)).repairable is False


def test_timeout_is_repairable_with_scope_hints():
    diagnosis = diagnose_execution_error(QueryTimeoutError("查询超过 20 秒"), "SELECT 1")

    assert diagnosis.code == "timeout"
    assert diagnosis.repairable
    assert any("聚合" in hint for hint in diagnosis.hints)


@pytest.mark.parametrize(
    "message",
    [
        'Binder Error: Referenced column "industry_name" not found in FROM clause!',
        "(1054, \"Unknown column 'industry_name' in 'field list'\")",
    ],
)
def test_execution_unknown_column_is_extracted_from_duckdb_and_mysql_messages(message):
    diagnosis = diagnose_execution_error(
        QueryExecutionError(message), "SELECT industry_name FROM t"
    )

    assert diagnosis.code == "execution_unknown_column"
    assert "industry_name" in diagnosis.message


def test_unclassified_execution_error_keeps_message():
    diagnosis = diagnose_execution_error(
        QueryExecutionError("Conversion Error: could not convert string"), "SELECT 1"
    )

    assert diagnosis.code == "type_error"
    assert diagnosis.repairable


def test_cost_diagnosis_only_when_limit_exceeded():
    assert diagnose_cost(CostEstimate(max_cardinality=10), 1000) is None
    assert diagnose_cost(CostEstimate(max_cardinality=None), 1000) is None
    diagnosis = diagnose_cost(CostEstimate(max_cardinality=5_000_000_000), 50_000_000)
    assert diagnosis.code == "cost_exceeded"
    assert "5,000,000,000" in diagnosis.message


def test_empty_result_with_nonexistent_literal_is_suspicious(values):
    diagnosis = diagnose_empty_result(
        "SELECT COUNT(*) AS n FROM `企业基本信息` e WHERE e.`status` = '存续'", values, SCHEMA
    )

    assert diagnosis is not None and diagnosis.code == "suspicious_empty"
    joined = " ".join(diagnosis.hints)
    assert "存续（在营、开业、在册）" in joined
    assert "LIKE '存续%'" in joined


def test_empty_result_with_existing_literal_is_not_suspicious(values):
    assert (
        diagnose_empty_result(
            "SELECT eid FROM `企业基本信息` WHERE status = '注销'", values, SCHEMA
        )
        is None
    )


def test_unscanned_columns_cannot_be_judged(values):
    assert (
        diagnose_empty_result("SELECT id FROM `招投标` WHERE area_code = '440300'", values, SCHEMA)
        is None
    )


def test_in_list_and_like_patterns_are_checked(values):
    in_list = diagnose_empty_result(
        "SELECT id FROM `融资数据` WHERE `round` IN ('天使轮', 'D轮')", values, SCHEMA
    )
    like = diagnose_empty_result(
        "SELECT eid FROM `企业基本信息` WHERE status LIKE '%倒闭%'", values, SCHEMA
    )

    assert in_list is not None and "D轮" in in_list.message
    assert like is not None and "倒闭" in like.message


def test_feedback_text_contains_sql_reason_and_hints():
    diagnosis = Diagnosis(code="unknown_column", message="字段 x 不存在", hints=["相近字段：y"])
    text = diagnosis.feedback("SELECT x FROM t")

    assert "SELECT x FROM t" in text
    assert "字段 x 不存在" in text
    assert "相近字段：y" in text
