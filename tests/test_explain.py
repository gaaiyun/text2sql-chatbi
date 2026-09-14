"""解析调试台的解释：把匹配结果翻译成业务语言，Streamlit 页面与网页共用。"""

from __future__ import annotations

import json

import pytest

from text2sql.db.schema import load_znjz_schema
from text2sql.semantic.catalog import load_znjz_catalog
from text2sql.semantic.explain import explain_question
from text2sql.semantic.parser import SemanticParser
from text2sql.sql.guard import SQLGuard

CATALOG = load_znjz_catalog()
PARSER = SemanticParser(CATALOG)
GUARD = SQLGuard.from_catalog(CATALOG, load_znjz_schema(), max_rows=500)


def explain(question: str) -> dict:
    return explain_question(question, parser=PARSER, catalog=CATALOG, guard=GUARD, max_rows=500)


def test_supported_question_is_explained_down_to_guarded_sql():
    data = explain("2020年以来有融资记录的企业数量")

    meanings = {m["surface"]: (m["kind_label"], m["meaning"]) for m in data["matches"]}
    assert meanings["2020年以来"] == ("时间范围", "2020 年及以后")
    assert meanings["有融资记录"] == ("筛选", "有融资记录")
    assert data["plan"]["entity"] == "enterprise"
    assert "YEAR(fx.`round_date`) >= 2020" in data["sql"]
    assert data["guard"]["is_safe"] is True
    assert data["declined"] is None


def test_matches_carry_character_offsets_for_inline_highlighting():
    data = explain("广州市存续企业有多少家")
    city = next(m for m in data["matches"] if m["surface"] == "广州市")

    assert data["normalized"][city["start"] : city["end"]] == "广州市"
    assert city["meaning"] == "登记城市 = 广州市（4401）"


@pytest.mark.parametrize(
    ("question", "fragment"),
    [("有融资但没有专利的企业", "专利"), ("每个城市招投标记录最多的企业分别是哪家", "窗口函数")],
)
def test_declined_questions_explain_why(question, fragment):
    data = explain(question)

    assert data["sql"] is None
    assert fragment in (data["declined"] + "".join(data["unexplained"]))


def test_explanation_is_json_serializable():
    json.dumps(explain("注册资本最高的10家企业"), ensure_ascii=False)
