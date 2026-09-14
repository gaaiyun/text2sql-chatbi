from __future__ import annotations

import json

import pytest

from tests.semantic_cases import PARSE_CASES
from text2sql.agent.exemplars import ZNJZ_EXEMPLARS_PATH, ExemplarStore, question_similarity
from text2sql.db.schema import load_znjz_schema
from text2sql.evaluation.compare import results_match
from text2sql.evaluation.runner import (
    ZNJZ_BENCHMARK_PATH,
    EvalSummary,
    check_gate,
    load_benchmark,
    render_markdown,
    replace_marked_section,
    run_evaluation,
)
from text2sql.semantic.catalog import load_znjz_catalog
from text2sql.sql.guard import SQLGuard

CATALOG = load_znjz_catalog()
ITEMS = load_benchmark(ZNJZ_BENCHMARK_PATH)


# --------------------------------------------------------------------------- 结果比对


def test_identical_results_match():
    assert results_match(["a", "b"], [(1, "x"), (2, "y")], ["a", "b"], [(1, "x"), (2, "y")]).match


def test_column_names_and_order_are_ignored():
    assert results_match(["城市", "n"], [("广州市", 5)], ["cnt", "city"], [(5, "广州市")]).match


def test_extra_predicted_columns_are_projected_away():
    result = results_match(
        ["n"], [(5,), (3,)], ["name", "n", "share"], [("甲", 5, 62.5), ("乙", 3, 37.5)]
    )
    assert result.match


def test_row_count_mismatch_reports_reason():
    result = results_match(["n"], [(1,), (2,)], ["n"], [(1,)])

    assert not result.match
    assert "行数" in result.reason


def test_order_matters_only_when_requested():
    gold = [("a", 3), ("b", 2)]
    pred = [("b", 2), ("a", 3)]

    assert results_match(["k", "v"], gold, ["k", "v"], pred, order_matters=False).match
    assert not results_match(["k", "v"], gold, ["k", "v"], pred, order_matters=True).match


def test_numbers_are_compared_at_business_precision():
    assert results_match(["v"], [(1234.57,)], ["v"], [(1234.5678,)]).match
    assert results_match(["v"], [(5,)], ["v"], [(5.0,)]).match
    assert not results_match(["v"], [(1234.57,)], ["v"], [(1234.6,)]).match


def test_nulls_and_strings_compare_exactly():
    assert results_match(["k"], [(None,), ("x",)], ["k"], [("x",), (None,)]).match
    assert not results_match(["k"], [("存续",)], ["k"], [("存续（在营、开业、在册）",)]).match


def test_missing_gold_column_fails():
    result = results_match(["a", "b"], [(1, 2)], ["a"], [(1,)])
    assert not result.match


# --------------------------------------------------------------------------- 评测集本身


def test_benchmark_shape():
    ids = [item.id for item in ITEMS]

    assert len(ITEMS) >= 60
    assert len(ids) == len(set(ids))
    assert {item.expect for item in ITEMS} == {"answer", "refuse"}
    assert all(item.gold_sql for item in ITEMS if item.expect == "answer")
    assert sum(1 for item in ITEMS if item.expect == "refuse") >= 7


@pytest.mark.parametrize("item", [i for i in ITEMS if i.expect == "answer"], ids=lambda i: i.id)
def test_every_gold_sql_is_safe_and_runs_on_demo(item, demo_backend):
    guard = SQLGuard.from_catalog(CATALOG, load_znjz_schema(), max_rows=5000)
    report = guard.check(item.gold_sql)

    assert report.is_safe, (item.id, report.errors)
    result = demo_backend.execute(report.safe_sql, max_rows=5000)
    assert 0 < result.row_count <= 500, (item.id, result.row_count)


def test_benchmark_is_not_copied_from_development_material():
    exemplars = ExemplarStore.load(ZNJZ_EXEMPLARS_PATH)
    development = {q for q, _ in PARSE_CASES} | {e.question for e in CATALOG.examples}
    for item in ITEMS:
        if item.expect != "answer" or item.category == "长尾复杂":
            continue
        assert item.question not in development, item.id
        closest = max(question_similarity(item.question, e.question) for e in exemplars.exemplars)
        assert closest < 0.85, (item.id, closest)


# --------------------------------------------------------------------------- 运行与报告


@pytest.fixture(scope="module")
def semantic_agent(demo_backend, demo_db_path):
    from text2sql.agent.graph import Text2SQLAgent
    from text2sql.config import Settings

    return Text2SQLAgent(
        catalog=CATALOG,
        schema=load_znjz_schema(),
        backend=demo_backend,
        settings=Settings.from_mapping({"T2S_DEMO_DB_PATH": str(demo_db_path)}),
        planners=("semantic",),
    )


def test_run_evaluation_scores_answers_refusals_and_linking(semantic_agent, demo_backend):
    subset = [i for i in ITEMS if i.id in {"b01", "b22", "b35", "b49", "r01", "r05"}]
    summary, outcomes = run_evaluation(
        semantic_agent, subset, backend=demo_backend, mode="semantic"
    )
    by_id = {o.id: o for o in outcomes}

    assert by_id["b01"].correct is True
    assert by_id["b22"].correct is True
    assert by_id["b49"].status == "declined" and by_id["b49"].correct is None
    assert by_id["r01"].refused_ok is True and by_id["r05"].refused_ok is True
    assert summary.answerable == 4 and summary.refusals_expected == 2
    assert summary.precision == 1.0
    assert summary.refusal_accuracy == 1.0
    assert 0 < summary.coverage < 1
    assert 0 <= summary.linking_recall <= 1
    assert summary.by_category["分布统计"]["correct"] == 1
    json.dumps(summary.to_dict(), ensure_ascii=False)


def test_gate_enforces_thresholds():
    good = EvalSummary(
        mode="semantic",
        total=10,
        answerable=8,
        refusals_expected=2,
        answered=6,
        correct=6,
        refused_ok=2,
    )
    bad = EvalSummary(
        mode="semantic",
        total=10,
        answerable=8,
        refusals_expected=2,
        answered=6,
        correct=5,
        refused_ok=1,
    )

    assert check_gate(good, min_precision=0.95, min_refusal=1.0, min_coverage=0.6) == []
    failures = check_gate(bad, min_precision=0.95, min_refusal=1.0, min_coverage=0.6)
    assert any("精确率" in f for f in failures) and any("拒答" in f for f in failures)


def test_markdown_report_and_marked_section_replacement(semantic_agent, demo_backend):
    subset = [i for i in ITEMS if i.id in {"b01", "b35", "r01"}]
    summary, outcomes = run_evaluation(
        semantic_agent, subset, backend=demo_backend, mode="semantic"
    )
    block = render_markdown(
        summary, outcomes, meta={"backend": "duckdb-demo", "date": "2026-09-14"}
    )

    assert "| 类别 |" in block
    assert "作答精确率" in block
    assert "b35" in block  # 未作答的题目要列出原因

    document = "前言\n<!-- EVAL:semantic:BEGIN -->\n旧内容\n<!-- EVAL:semantic:END -->\n结尾\n"
    updated = replace_marked_section(document, "semantic", block)
    assert "旧内容" not in updated and block in updated and updated.endswith("结尾\n")
    assert replace_marked_section(updated, "semantic", block) == updated
