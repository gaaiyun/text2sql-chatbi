from __future__ import annotations

from text2sql.db.schema import PhysicalSchema, load_znjz_schema

BASE_TABLES = {
    "企业基本信息",
    "企业基本信息_行业代码",
    "企业融资信息",
    "企业投资股东信息",
    "招投标信息",
    "商标资质信息",
}
VIEWS = {"企业行业代码", "融资数据", "投资数据", "招投标", "标签数据"}


def test_znjz_schema_has_six_tables_and_five_views():
    schema = load_znjz_schema()

    assert {t.name for t in schema.tables.values() if t.kind == "table"} == BASE_TABLES
    assert {t.name for t in schema.tables.values() if t.kind == "view"} == VIEWS


def test_enterprise_table_columns_and_types():
    enterprise = load_znjz_schema().get("企业基本信息")

    assert enterprise is not None
    assert len(enterprise.columns) == 40
    types = {c.name: c.type for c in enterprise.columns}
    assert types["status"] == "varchar"
    assert types["start_date"] == "datetime"
    assert types["regist_capi_new"] == "double"
    assert types["scope"] == "text"


def test_view_columns_follow_select_aliases_not_base_names():
    schema = load_znjz_schema()
    bidding = schema.get("招投标")

    assert [c.name for c in bidding.columns] == [
        "eid",
        "u_id",
        "id",
        "title",
        "publish_time",
        "area_code",
        "notice_type_main",
        "notice_type_sub",
        "industry_code",
        "project_number",
        "project_bid_money",
        "create_time",
        "row_update_time",
        "merge_data_time",
    ]
    assert "role1" not in bidding.column_names()
    assert "name" not in bidding.column_names()


def test_view_column_types_are_inherited_from_base_table():
    schema = load_znjz_schema()
    bidding = {c.name: c.type for c in schema.get("招投标").columns}

    assert bidding["publish_time"] == "datetime"
    assert bidding["eid"] == "varchar"
    assert bidding["industry_code"] == "null"


def test_investment_view_has_no_should_capi_conv():
    """v1 提示词让模型使用 投资数据.should_capi_conv，但视图里并没有这一列。"""
    investment = load_znjz_schema().get("投资数据")

    assert "should_capi_conv" not in investment.column_names()
    assert "stock_percent" in investment.column_names()


def test_lookup_is_case_insensitive_and_strips_quotes():
    schema = PhysicalSchema.from_ddl("CREATE TABLE `Orders` (`id` bigint, `Amount` decimal(10,2));")

    assert schema.get("orders") is schema.get("`ORDERS`")
    assert schema.get("orders").has_column("amount")


def test_view_underlying_table_is_recorded():
    schema = load_znjz_schema()

    assert schema.get("融资数据").base_tables == ("企业融资信息",)
    assert schema.get("企业基本信息").base_tables == ()


def test_sqlglot_mapping_covers_every_object():
    mapping = load_znjz_schema().to_sqlglot_mapping()

    assert set(mapping) == BASE_TABLES | VIEWS
    assert mapping["融资数据"]["round"] == "VARCHAR"
    assert mapping["企业基本信息"]["regist_capi_new"] == "DOUBLE"


def test_dataset_ddl_has_no_account_information():
    from text2sql.db.schema import ZNJZ_DDL_PATH

    ddl = ZNJZ_DDL_PATH.read_text(encoding="utf-8")

    assert "DEFINER=`" not in ddl
    assert "AUTO_INCREMENT=" not in ddl
