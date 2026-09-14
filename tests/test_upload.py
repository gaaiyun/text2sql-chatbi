"""用户上传的数据集：导入、画像、自动生成的语义层与表结构，以及经 SQL 智能体的完整问答。"""

from __future__ import annotations

import json

import duckdb
import pytest

from text2sql.agent.graph import Text2SQLAgent
from text2sql.agent.llm import ScriptedLLM, tool_call_response
from text2sql.datasets.cards import apply_card, load_card
from text2sql.datasets.samples import write_retail_sample
from text2sql.datasets.upload import (
    UploadError,
    build_catalog,
    build_schema,
    import_files,
    suggest_questions,
    table_name_for,
)
from text2sql.sql.guard import SQLGuard


@pytest.fixture
def sample(tmp_path):
    files = write_retail_sample(tmp_path / "samples")
    tables = import_files(tmp_path / "upload.duckdb", [(p.name, p) for p in files])
    return tmp_path / "upload.duckdb", tables


@pytest.mark.parametrize(
    ("filename", "taken", "expected"),
    [
        ("门店销售.csv", set(), "门店销售"),
        ("2025 Q1 sales (final).csv", set(), "t_2025_Q1_sales_final"),
        ("门店销售.xlsx", {"门店销售"}, "门店销售_2"),
        ("...csv", set(), "上传表"),
    ],
)
def test_table_names_are_safe_and_unique(filename, taken, expected):
    assert table_name_for(filename, taken) == expected


def test_sample_files_are_imported_with_row_counts_and_roles(sample):
    _, tables = sample
    sales = next(t for t in tables if t.name == "门店销售")
    roles = {c["name"]: c["role"] for c in sales.columns}

    assert sales.rows > 1000
    assert roles["销售额"] == "measure"
    assert roles["城市"] == "dimension"
    assert roles["日期"] == "time"
    assert {t.name for t in tables} == {"门店销售", "门店"}


def test_gbk_csv_and_personal_columns_are_handled(tmp_path):
    path = tmp_path / "会员.csv"
    path.write_bytes(
        "会员姓名,手机号,城市,消费金额\n张三,13800000000,广州,120.5\n李四,13900000000,深圳,88\n".encode(
            "gbk"
        )
    )

    tables = import_files(tmp_path / "gbk.duckdb", [(path.name, path)])
    columns = {c["name"]: c for c in tables[0].columns}

    assert tables[0].rows == 2
    assert columns["会员姓名"]["sensitive"] and columns["手机号"]["sensitive"]
    assert columns["会员姓名"]["samples"] == []
    assert columns["城市"]["samples"] == ["广州", "深圳"]


def test_parquet_files_are_supported(tmp_path):
    path = tmp_path / "orders.parquet"
    duckdb.sql("SELECT range AS id, range % 3 AS kind FROM range(10)").write_parquet(str(path))

    tables = import_files(tmp_path / "pq.duckdb", [(path.name, path)])

    assert tables[0].name == "orders"
    assert tables[0].rows == 10


def test_unsupported_or_empty_files_raise_clear_errors(tmp_path):
    bad = tmp_path / "notes.docx"
    bad.write_bytes(b"not a table")
    empty = tmp_path / "empty.csv"
    empty.write_bytes(b"")

    with pytest.raises(UploadError, match="docx"):
        import_files(tmp_path / "x.duckdb", [(bad.name, bad)])
    with pytest.raises(UploadError, match="empty.csv"):
        import_files(tmp_path / "y.duckdb", [(empty.name, empty)])


def test_generated_catalog_and_schema_work_with_the_guard(sample):
    _, tables = sample
    catalog = build_catalog(tables)
    schema = build_schema(tables)
    guard = SQLGuard.from_catalog(catalog, schema, max_rows=200)

    assert catalog.open_domain is True
    assert set(catalog.link_fallback) == {"门店销售", "门店"}
    assert catalog.validate(schema) == []
    assert guard.check("SELECT `城市`, SUM(`销售额`) AS s FROM `门店销售` GROUP BY `城市`").is_safe
    report = guard.check("SELECT `不存在` FROM `门店销售`")
    assert not report.is_safe and report.error_code == "unknown_column"


def test_generated_catalog_blocks_personal_columns(tmp_path):
    path = tmp_path / "会员.csv"
    path.write_text("会员姓名,城市\n张三,广州\n", encoding="utf-8")
    tables = import_files(tmp_path / "m.duckdb", [(path.name, path)])
    catalog = build_catalog(tables)
    guard = SQLGuard.from_catalog(catalog, build_schema(tables), max_rows=100)

    assert guard.check("SELECT `会员姓名` FROM `会员`").error_code == "sensitive_column"


def test_numeric_columns_are_measures_unless_they_look_like_codes(sample):
    _, tables = sample
    stores = {c["name"]: c["role"] for c in next(t for t in tables if t.name == "门店").columns}
    sales = {c["name"]: c["role"] for c in next(t for t in tables if t.name == "门店销售").columns}

    assert stores["面积（平方米）"] == "measure"  # 只有 8 行，但仍是数值
    assert sales["单价"] == "measure"
    assert stores["门店编号"] == "dimension"


def test_numeric_codes_years_and_rating_levels_are_dimensions(tmp_path):
    path = tmp_path / "员工.csv"
    lines = ["员工编号,入职年份,年龄,绩效等级,月薪,DeptID"]
    lines += [
        f"{10001 + i},{2015 + i % 10},{22 + i % 30},{1 + i % 5},{8000 + i * 137},{100 + i % 40}"
        for i in range(60)
    ]
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))

    table = import_files(tmp_path / "e.duckdb", [(path.name, path)])[0]

    assert {c["name"]: c["role"] for c in table.columns} == {
        "员工编号": "dimension",
        "入职年份": "dimension",
        "年龄": "measure",  # 含“年”字，但不是年份
        "绩效等级": "dimension",  # 60 行只有 5 个取值
        "月薪": "measure",
        "DeptID": "dimension",  # 驼峰命名的外键
    }


def test_anchor_year_follows_the_latest_date_in_the_data(tmp_path):
    path = tmp_path / "订单.csv"
    path.write_bytes("下单日期,金额\n2018-03-01,10\n2019-11-30,20\n".encode())

    tables = import_files(tmp_path / "o.duckdb", [(path.name, path)])

    assert tables[0].columns[0]["range"] == ["2018-03-01", "2019-11-30"]
    catalog = build_catalog(tables)
    assert catalog.anchor_year == 2019
    assert catalog.anchor_partial  # 只到 11 月，2019 年不完整

    path.write_bytes("下单日期,金额\n2019-12-31,20\n".encode())
    full_year = build_catalog(import_files(tmp_path / "p.duckdb", [(path.name, path)]))
    assert not full_year.anchor_partial


def test_card_labels_relationships_and_rules_flow_into_the_catalog(sample):
    _, tables = sample
    card = load_card("retail")

    apply_card(tables, card)
    catalog = build_catalog(tables, card=card)

    assert catalog.title == "连锁茶饮门店"
    assert catalog.tables["门店销售"].description.startswith("每行是某家门店")
    assert catalog.tables["门店销售"].columns["销售额"].description.endswith("数量 × 单价，单位元")
    assert catalog.relationships == (
        {"from": "门店销售.门店编号", "to": "门店.门店编号", "cardinality": "many_to_one"},
    )
    assert any("SUM(销售额)" in rule for rule in catalog.rules)
    assert [e.question for e in catalog.examples] == list(card.suggestions)
    assert catalog.validate(build_schema(tables)) == []


def test_card_synonyms_let_business_words_find_the_right_table(sample):
    from text2sql.agent.linking import SchemaLinker

    _, tables = sample
    card = load_card("retail")
    apply_card(tables, card)
    catalog = build_catalog(tables, card=card)

    assert "营业额" in catalog.tables["门店销售"].synonyms
    link = SchemaLinker(catalog, build_schema(tables)).link("各城市的营业额")
    assert link.names()[0] == "门店销售"


def test_suggestions_use_real_columns_from_the_main_table(sample):
    _, tables = sample

    suggestions = suggest_questions(tables)

    assert suggestions[:2] == ["按城市统计销售额的合计", "销售额最高的 10 条记录是哪些"]
    assert all("编号" not in q and "单价" not in q for q in suggestions)
    assert all(any(c["name"] in q for t in tables for c in t.columns) for q in suggestions)


def test_suggestions_include_a_join_through_a_shared_key(sample):
    _, tables = sample

    # 门店编号在门店表里唯一，门店名称只能关联过去才拿得到
    assert "按门店名称统计销售额的合计" in suggest_questions(tables)


def test_suggestions_for_a_table_without_measures(tmp_path):
    path = tmp_path / "工单.csv"
    rows = ["工单编号,状态,优先级"] + [
        f"W{i:03d},{'已关闭' if i % 3 else '处理中'},{'高中低'[i % 3]}" for i in range(40)
    ]
    path.write_bytes(("\n".join(rows) + "\n").encode("utf-8"))

    suggestions = suggest_questions(import_files(tmp_path / "w.duckdb", [(path.name, path)]))

    assert suggestions == ["各状态分别有多少条记录", "各优先级分别有多少条记录"]


def test_sql_agent_answers_questions_over_uploaded_tables(sample):
    from text2sql.agent.exemplars import ExemplarStore
    from text2sql.agent.values import build_value_index
    from text2sql.config import Settings
    from text2sql.db.backends import DuckDBBackend

    db_path, tables = sample
    catalog = build_catalog(tables)
    backend = DuckDBBackend(db_path)
    sql = "SELECT `城市`, SUM(`销售额`) AS `销售额合计` FROM `门店销售` GROUP BY `城市` ORDER BY `销售额合计` DESC"

    def handler(messages, tools):
        if tools is None:
            return "- 已按城市汇总"
        return tool_call_response("submit_sql", {"sql": sql, "assumptions": ["按门店所在城市汇总"]})

    agent = Text2SQLAgent(
        catalog=catalog,
        schema=build_schema(tables),
        backend=backend,
        settings=Settings.from_mapping({"T2S_LLM_NARRATIVE": "false"}),
        llm=ScriptedLLM(handler=handler),
        planners=("llm",),
        values=build_value_index(backend, catalog),
        exemplars=ExemplarStore([]),
        orchestrator="sequential",
    )
    try:
        result = agent.ask("哪个城市的销售额最高")
    finally:
        agent.close()

    assert result.status == "answered", result.message
    assert result.planner == "llm"
    assert result.rows and "销售额合计" in result.columns
    json.dumps(result.to_dict(), ensure_ascii=False, default=str)
