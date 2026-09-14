import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.export_mysql_text2sql_schema import (
    quote_identifier,
    render_create_sql,
    render_markdown_schema,
)


def test_quote_identifier_escapes_chinese_and_backticks():
    assert quote_identifier("企业基本信息") == "`企业基本信息`"
    assert quote_identifier("we`ird") == "`we``ird`"


def test_render_markdown_schema_includes_tables_columns_profiles_and_join_hints():
    schema = {
        "database": "demo_db",
        "tables": [
            {
                "name": "企业基本信息",
                "table_type": "BASE TABLE",
                "engine": "InnoDB",
                "table_rows": 2,
                "table_collation": "utf8mb4_general_ci",
                "comment": "企业基础档案",
                "create_sql": "CREATE TABLE `企业基本信息` (`企业名称` varchar(255));",
                "columns": [
                    {
                        "name": "企业名称",
                        "column_type": "varchar(255)",
                        "is_nullable": "NO",
                        "column_key": "PRI",
                        "column_default": None,
                        "extra": "",
                        "comment": "企业名称",
                        "profile": {
                            "total_rows": 2,
                            "empty_rows": 0,
                            "distinct_values": 2,
                            "samples": ["甲公司", "乙公司"],
                        },
                    },
                    {
                        "name": "行业",
                        "column_type": "varchar(255)",
                        "is_nullable": "YES",
                        "column_key": "",
                        "column_default": None,
                        "extra": "",
                        "comment": "所属行业",
                        "profile": {
                            "total_rows": 2,
                            "empty_rows": 0,
                            "distinct_values": 1,
                            "samples": ["智能制造"],
                        },
                    },
                ],
                "indexes": [
                    {
                        "index_name": "PRIMARY",
                        "non_unique": 0,
                        "columns": ["企业名称"],
                        "index_type": "BTREE",
                        "comment": "",
                    }
                ],
            },
            {
                "name": "资质证书",
                "table_type": "BASE TABLE",
                "engine": "InnoDB",
                "table_rows": 1,
                "table_collation": "utf8mb4_general_ci",
                "comment": "企业资质",
                "create_sql": "CREATE TABLE `资质证书` (`企业名称` varchar(255));",
                "columns": [
                    {
                        "name": "企业名称",
                        "column_type": "varchar(255)",
                        "is_nullable": "NO",
                        "column_key": "",
                        "column_default": None,
                        "extra": "",
                        "comment": "",
                    }
                ],
                "indexes": [],
            },
        ],
        "views": {},
    }

    markdown = render_markdown_schema(schema, generated_at="2026-07-07 09:00:00")

    assert "# demo_db Text2SQL Schema" in markdown
    assert (
        "| `企业名称` | varchar(255) | NO | PRI | NULL | 企业名称 | 0/2 | 2 | 甲公司, 乙公司 |"
        in markdown
    )
    assert "## 表清单" in markdown
    assert "### `企业基本信息`" in markdown
    assert "- `企业名称`: `企业基本信息`, `资质证书`" in markdown


def test_render_create_sql_keeps_all_create_statements():
    schema = {
        "database": "demo_db",
        "tables": [
            {
                "name": "企业基本信息",
                "create_sql": "CREATE TABLE `企业基本信息` (`id` int)",
            },
            {"name": "资质证书", "create_sql": "CREATE TABLE `资质证书` (`id` int);"},
        ],
    }

    ddl = render_create_sql(schema)

    assert "-- demo_db DDL exported for Text2SQL schema" in ddl
    assert "CREATE TABLE `企业基本信息` (`id` int);" in ddl
    assert "CREATE TABLE `资质证书` (`id` int);" in ddl
