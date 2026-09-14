from __future__ import annotations

import copy

import pytest
import yaml

from text2sql.db.schema import load_znjz_schema
from text2sql.semantic.catalog import ZNJZ_SEMANTIC_PATH, SemanticCatalog, load_znjz_catalog


@pytest.fixture(scope="module")
def catalog() -> SemanticCatalog:
    return load_znjz_catalog()


def test_catalog_loads_dataset_metadata(catalog):
    assert catalog.dataset == "znjz"
    assert catalog.anchor_year == 2026
    assert catalog.anchor_partial  # 数据只到 2026 年上半年
    assert set(catalog.entities) == {
        "enterprise",
        "financing",
        "investment",
        "bidding",
        "qualification",
    }


def test_catalog_is_consistent_with_production_ddl(catalog):
    assert catalog.validate(load_znjz_schema()) == []


def test_summary_is_a_public_json_friendly_view(catalog):
    """API、MCP、界面共用的语义层概要：只含业务口径，不含 SQL 片段和敏感列。"""
    import json

    summary = catalog.summary()
    text = json.dumps(summary, ensure_ascii=False)

    assert summary["dataset"] == "znjz"
    assert {e["key"] for e in summary["entities"]} == set(catalog.entities)
    financing_amount = next(m for m in summary["metrics"] if m["key"] == "financing_amount")
    assert financing_amount["entities"] == ["financing"]
    assert any(f["key"] == "has_financing" and f["synonyms"] for f in summary["filters"])
    assert summary["examples"] and {"question", "category", "requires_llm"} <= set(
        summary["examples"][0]
    )
    assert "SELECT" not in text and "oper_name" not in text


def test_linking_and_domain_settings_live_in_yaml(catalog):
    """数据集相关的召回规则和领域词写在语义层里，代码本身不带行业假设。"""
    assert "融资数据" in catalog.link_fallback
    assert catalog.link_anchor["table"] == "企业基本信息"
    assert any(c["table"] == "企业行业代码" for c in catalog.link_companions)
    assert "招投标" in catalog.domain_words
    assert catalog.domain_topics
    assert catalog.open_domain is False


def test_validate_reports_unknown_tables_in_linking_rules():
    raw = yaml.safe_load(ZNJZ_SEMANTIC_PATH.read_text(encoding="utf-8"))
    raw["linking"]["companions"].append({"table": "不存在的表", "terms": ["x"]})

    problems = SemanticCatalog.from_dict(raw).validate(load_znjz_schema())

    assert problems == ["linking.companions: 不存在的表 未在语义层登记"]


def test_minimal_catalog_gets_generic_defaults():
    minimal = SemanticCatalog.from_dict({"dataset": "upload", "title": "上传的数据", "tables": {}})

    assert minimal.link_fallback == ()
    assert minimal.link_anchor is None
    assert minimal.link_companions == ()
    assert minimal.domain_words == ()
    assert minimal.open_domain is False


def test_whitelist_covers_all_eleven_objects(catalog):
    assert catalog.whitelist == set(load_znjz_schema().tables)


def test_sensitive_columns_apply_across_tables(catalog):
    assert catalog.is_sensitive("企业基本信息", "oper_name")
    assert catalog.is_sensitive("企业融资信息", "OPER_NAME")
    assert catalog.is_sensitive("企业投资股东信息", "invest_oper_name")
    assert not catalog.is_sensitive("招投标", "title")


def test_entity_default_metrics_resolve_for_their_entity(catalog):
    for key, entity in catalog.entities.items():
        assert catalog.metric_sql(entity.default_metric, key), key


def test_enterprise_count_compiles_per_entity(catalog):
    assert catalog.metric_sql("enterprise_count", "enterprise") == "COUNT(DISTINCT e.`eid`)"
    assert catalog.metric_sql("enterprise_count", "financing") == "COUNT(DISTINCT f.`eid`)"
    assert catalog.metric_sql("avg_capital", "bidding") is None


def test_city_dimension_expands_to_labelled_case(catalog):
    sql = catalog.dimensions["city"].sql

    assert sql.startswith("CASE SUBSTR(e.`district_code`, 1, 4)")
    assert "WHEN '4401' THEN '广州市'" in sql
    assert sql.endswith("ELSE '其他' END")


def test_value_map_surfaces_include_labels_and_aliases(catalog):
    surfaces = catalog.value_maps["city"].surfaces()

    assert surfaces["广州市"] == "4401"
    assert surfaces["广州"] == "4401"
    assert catalog.value_maps["industry_section"].surfaces()["制造业"] == "C"
    assert catalog.value_maps["district"].surfaces()["天河区"] == "440106"


def test_district_codes_nest_inside_known_cities(catalog):
    cities = catalog.code_tables["city"]
    for code in catalog.code_tables["district"]:
        assert len(code) == 6 and code.isdigit()
        assert code[:4] in cities, code


def test_filters_with_time_slots_declare_time_column(catalog):
    for key, flt in catalog.filters.items():
        if "{time}" in flt.sql:
            assert flt.time_column, key


def test_surface_terms_are_unambiguous_within_semantic_components(catalog):
    assert catalog.ambiguous_surfaces() == {}


def test_rules_document_known_value_traps(catalog):
    rules = "\n".join(catalog.rules)

    assert "存续（在营、开业、在册）" in rules
    assert ".0" in rules
    assert "COUNT(DISTINCT eid)" in rules


def test_wide_tables_point_to_preferred_views(catalog):
    for name in (
        "企业融资信息",
        "企业投资股东信息",
        "招投标信息",
        "商标资质信息",
        "企业基本信息_行业代码",
    ):
        doc = catalog.tables[name]
        assert doc.prefer in catalog.tables
        assert catalog.tables[doc.prefer].kind == "view"


def test_scan_value_columns_exist_in_documentation(catalog):
    targets = catalog.value_scan_targets()

    assert ("企业基本信息", "status") in targets
    assert ("融资数据", "round") in targets
    assert all(catalog.tables[t].columns[c].scan_values for t, c in targets)


def test_examples_match_what_each_path_can_actually_answer(catalog):
    from text2sql.semantic.parser import SemanticParser

    parser = SemanticParser(catalog)
    assert len(catalog.examples) >= 16
    for example in catalog.examples:
        parsed = parser.parse(example.question)
        if example.requires_llm:
            assert parsed.plan is None, example.question
        else:
            assert parsed.plan is not None, (example.question, parsed.reason)
    assert {e.category for e in catalog.examples} >= {
        "分布",
        "排名",
        "趋势",
        "筛选",
        "存在性",
        "占比",
        "长尾",
    }


def test_validate_reports_unknown_columns_and_tables():
    raw = yaml.safe_load(ZNJZ_SEMANTIC_PATH.read_text(encoding="utf-8"))
    broken = copy.deepcopy(raw)
    broken["dimensions"]["status"]["sql"] = "e.`industry_name`"
    broken["tables"]["不存在的表"] = {
        "kind": "table",
        "label": "x",
        "description": "x",
        "columns": {},
    }

    problems = SemanticCatalog.from_dict(broken).validate(load_znjz_schema())

    assert any("industry_name" in p for p in problems)
    assert any("不存在的表" in p for p in problems)


def test_validate_reports_unparseable_sql():
    raw = yaml.safe_load(ZNJZ_SEMANTIC_PATH.read_text(encoding="utf-8"))
    broken = copy.deepcopy(raw)
    broken["metrics"]["avg_capital"]["sql"]["enterprise"] = "AVG(e.`regist_capi_new`"

    problems = SemanticCatalog.from_dict(broken).validate(load_znjz_schema())

    assert any("avg_capital" in p for p in problems)
