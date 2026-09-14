from __future__ import annotations

import pytest

from text2sql.agent.linking import SchemaLinker
from text2sql.agent.values import build_value_index
from text2sql.db.schema import load_znjz_schema
from text2sql.semantic.catalog import load_znjz_catalog

CATALOG = load_znjz_catalog()
LINKER = SchemaLinker(CATALOG, load_znjz_schema())


@pytest.mark.parametrize(
    ("question", "expected_first"),
    [
        ("各融资轮次的企业数量", "融资数据"),
        ("有效资质的数量按年份分布", "标签数据"),
        ("按行业统计企业数量", "企业行业代码"),
        ("2023年发布的招投标公告标题有哪些", "招投标"),
        ("被投企业经营状态分布", "投资数据"),
    ],
)
def test_most_relevant_table_ranks_first(question, expected_first):
    assert LINKER.link(question).names()[0] == expected_first


def test_enterprise_table_is_added_when_question_needs_company_attributes():
    names = LINKER.link("2023年中标记录最多的企业名称").names()

    assert "招投标" in names
    assert "企业基本信息" in names


def test_wide_table_is_suppressed_when_its_view_covers_the_question():
    names = LINKER.link("融资轮次分布").names()

    assert "融资数据" in names
    assert "企业融资信息" not in names


@pytest.mark.parametrize(
    ("question", "wide_table"),
    [
        ("对外投资的认缴出资额合计", "企业投资股东信息"),
        ("招投标记录里企业角色代码的分布", "招投标信息"),
    ],
)
def test_wide_table_is_kept_for_columns_only_it_has(question, wide_table):
    assert wide_table in LINKER.link(question).names()


def test_vague_question_falls_back_to_all_analysis_views():
    link = LINKER.link("整体看看")

    assert set(link.names()) == {
        "企业基本信息",
        "企业行业代码",
        "融资数据",
        "投资数据",
        "招投标",
        "标签数据",
    }
    assert "全部分析表" in link.tables[0].reasons[0]


def test_generic_catalog_falls_back_to_every_table():
    from text2sql.db.schema import ColumnDef, PhysicalSchema, TableDef
    from text2sql.semantic.catalog import SemanticCatalog

    tables = {
        "门店销售": {"columns": {"城市": {}, "销售额": {}}},
        "门店": {"columns": {"门店编号": {}, "开业日期": {}}},
    }
    catalog = SemanticCatalog.from_dict({"dataset": "upload", "title": "上传", "tables": tables})
    schema = PhysicalSchema(
        {
            name: TableDef(name, "table", tuple(ColumnDef(c, "varchar") for c in spec["columns"]))
            for name, spec in tables.items()
        }
    )

    link = SchemaLinker(catalog, schema).link("帮我看看整体情况")

    assert link.names() == ["门店销售", "门店"]


def test_reasons_explain_each_link():
    table = LINKER.link("各融资轮次的企业数量").tables[0]
    assert any("融资轮次" in reason for reason in table.reasons)


def test_render_shows_grain_relationships_types_and_hints(demo_backend):
    values = build_value_index(demo_backend, CATALOG)
    text = LINKER.render(LINKER.link("招投标记录数按项目城市分布"), values=values)

    assert "`招投标`" in text
    assert "一行一条招投标公告记录" in text
    assert "`招投标`.eid → `企业基本信息`.eid" in text
    assert "publish_time DATETIME" in text
    assert "LIKE '4403%'" in text
    assert "notice_type_main" in text and "取值" in text


def test_render_never_exposes_sensitive_columns():
    text = LINKER.render(LINKER.link("企业法定代表人和经营状态"))

    assert "oper_name" not in text
    assert "法定代表人" not in text
