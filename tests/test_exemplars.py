from __future__ import annotations

import pytest

from text2sql.agent.exemplars import ZNJZ_EXEMPLARS_PATH, ExemplarStore
from text2sql.db.schema import load_znjz_schema
from text2sql.semantic.catalog import load_znjz_catalog
from text2sql.sql.guard import SQLGuard

CATALOG = load_znjz_catalog()
GUARD = SQLGuard.from_catalog(CATALOG, load_znjz_schema())
STORE = ExemplarStore.load(ZNJZ_EXEMPLARS_PATH)


def test_store_has_enough_diverse_exemplars():
    assert len(STORE.exemplars) >= 20
    assert len({e.id for e in STORE.exemplars}) == len(STORE.exemplars)
    tags = {tag for e in STORE.exemplars for tag in e.tags}
    assert {"值域陷阱", "反连接", "窗口函数", "宽表过滤"} <= tags


@pytest.mark.parametrize("exemplar", STORE.exemplars, ids=lambda e: e.id)
def test_every_exemplar_passes_guard_and_returns_rows(exemplar, demo_backend):
    report = GUARD.check(exemplar.sql)

    assert report.is_safe, (exemplar.id, report.errors, report.hints)
    result = demo_backend.execute(report.safe_sql, max_rows=50)
    assert result.row_count > 0, exemplar.id


def test_tables_are_extracted_without_cte_names():
    exemplar = next(e for e in STORE.exemplars if "WITH" in e.sql.upper())
    assert "ranked" not in exemplar.tables
    assert "招投标" in exemplar.tables


def test_search_prefers_structurally_similar_exemplar():
    results = STORE.search(
        "有融资记录但没有招投标记录的企业名单", tables=["企业基本信息", "融资数据", "招投标"]
    )

    assert "NOT EXISTS" in results[0][0].sql


def test_domain_stopwords_do_not_dominate_similarity():
    """“企业/公司/数量”几乎出现在每个问题里，放进相似度会让毫不相关的问题互相匹配。"""
    results = STORE.search("2023年中标记录最多的企业", tables=["招投标"])

    assert "招投标" in results[0][0].tables
    assert "投资数据" not in results[0][0].tables


def test_irrelevant_question_retrieves_nothing():
    assert STORE.search("今天天气怎么样") == []


def test_render_formats_exemplars_for_prompt():
    text = STORE.render(STORE.search("各城市有融资记录的企业数量", k=2))

    assert "问题：" in text
    assert "```sql" in text
