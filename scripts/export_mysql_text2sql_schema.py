#!/usr/bin/env python
"""Export a MySQL database schema as Text2SQL knowledge-base documents.

The script reads connection settings from CLI arguments plus environment
variables. It never stores database passwords in generated files.

Example:
    $env:DB_PASSWORD_SCENARIO_1_3 = "<your-password>"
    python scripts/export_mysql_text2sql_schema.py `
      --host your-db-host `
      --port 3306 `
      --database your-db-name `
      --user your-db-user `
      --password-env DB_PASSWORD_SCENARIO_1_3 `
      --profile-columns
"""

from __future__ import annotations

import argparse
import os
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

SKIP_PROFILE_TYPES = {
    "binary",
    "blob",
    "geometry",
    "json",
    "longblob",
    "mediumblob",
    "tinyblob",
    "varbinary",
}


def quote_identifier(identifier: str) -> str:
    """Safely quote a MySQL identifier, including Chinese names."""
    if identifier is None:
        raise ValueError("identifier cannot be None")
    identifier = str(identifier)
    if not identifier.strip():
        raise ValueError("identifier cannot be empty")
    return f"`{identifier.replace('`', '``')}`"


def qualified_name(database: str, table_name: str) -> str:
    return f"{quote_identifier(database)}.{quote_identifier(table_name)}"


def markdown_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        value = str(value)
    text = str(value)
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    text = text.replace("|", "\\|")
    return text.strip()


def compact_value(value: Any, max_len: int = 80) -> str:
    text = markdown_cell(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def fetch_dicts(cursor: DictCursor, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    cursor.execute(sql, tuple(params))
    return list(cursor.fetchall())


def fetch_tables(cursor: DictCursor, database: str) -> list[dict]:
    return fetch_dicts(
        cursor,
        """
        SELECT
            TABLE_NAME AS name,
            TABLE_TYPE AS table_type,
            ENGINE AS engine,
            TABLE_ROWS AS table_rows,
            TABLE_COLLATION AS table_collation,
            CREATE_TIME AS create_time,
            UPDATE_TIME AS update_time,
            TABLE_COMMENT AS comment
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s
        ORDER BY TABLE_NAME
        """,
        (database,),
    )


def fetch_columns(cursor: DictCursor, database: str) -> dict[str, list[dict]]:
    rows = fetch_dicts(
        cursor,
        """
        SELECT
            TABLE_NAME AS table_name,
            COLUMN_NAME AS name,
            ORDINAL_POSITION AS ordinal_position,
            COLUMN_TYPE AS column_type,
            DATA_TYPE AS data_type,
            IS_NULLABLE AS is_nullable,
            COLUMN_KEY AS column_key,
            COLUMN_DEFAULT AS column_default,
            EXTRA AS extra,
            CHARACTER_SET_NAME AS character_set_name,
            COLLATION_NAME AS collation_name,
            COLUMN_COMMENT AS comment
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s
        ORDER BY TABLE_NAME, ORDINAL_POSITION
        """,
        (database,),
    )
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["table_name"]].append(row)
    return dict(grouped)


def fetch_indexes(cursor: DictCursor, database: str) -> dict[str, list[dict]]:
    rows = fetch_dicts(
        cursor,
        """
        SELECT
            TABLE_NAME AS table_name,
            INDEX_NAME AS index_name,
            NON_UNIQUE AS non_unique,
            SEQ_IN_INDEX AS seq_in_index,
            COLUMN_NAME AS column_name,
            INDEX_TYPE AS index_type,
            INDEX_COMMENT AS comment
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s
        ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
        """,
        (database,),
    )

    by_table_index: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["table_name"], row["index_name"])
        item = by_table_index.setdefault(
            key,
            {
                "index_name": row["index_name"],
                "non_unique": row["non_unique"],
                "columns": [],
                "index_type": row["index_type"],
                "comment": row["comment"],
            },
        )
        item["columns"].append(row["column_name"])

    grouped: dict[str, list[dict]] = defaultdict(list)
    for (table_name, _), item in by_table_index.items():
        grouped[table_name].append(item)
    return dict(grouped)


def fetch_views(cursor: DictCursor, database: str) -> dict[str, str]:
    rows = fetch_dicts(
        cursor,
        """
        SELECT TABLE_NAME AS table_name, VIEW_DEFINITION AS view_definition
        FROM information_schema.VIEWS
        WHERE TABLE_SCHEMA = %s
        ORDER BY TABLE_NAME
        """,
        (database,),
    )
    return {row["table_name"]: row["view_definition"] for row in rows}


def fetch_create_sql(cursor: DictCursor, database: str, table_name: str) -> str:
    cursor.execute(f"SHOW CREATE TABLE {qualified_name(database, table_name)}")
    row = cursor.fetchone()
    if not row:
        return ""
    values = list(row.values())
    return str(values[1]) if len(values) > 1 and values[1] is not None else ""


def fetch_exact_count(cursor: DictCursor, database: str, table_name: str) -> int | None:
    cursor.execute(f"SELECT COUNT(*) AS n FROM {qualified_name(database, table_name)}")
    row = cursor.fetchone()
    return int(row["n"]) if row and row.get("n") is not None else None


def profile_column(
    cursor: DictCursor,
    database: str,
    table_name: str,
    column: dict,
    sample_limit: int,
) -> dict:
    data_type = str(column.get("data_type") or "").lower()
    if data_type in SKIP_PROFILE_TYPES:
        return {"skipped": f"skip {data_type}"}

    table = qualified_name(database, table_name)
    col = quote_identifier(column["name"])

    cursor.execute(f"""
        SELECT
            COUNT(*) AS total_rows,
            SUM(CASE
                WHEN {col} IS NULL OR TRIM(CAST({col} AS CHAR)) = '' THEN 1
                ELSE 0
            END) AS empty_rows,
            COUNT(DISTINCT {col}) AS distinct_values
        FROM {table}
        """)
    stats = cursor.fetchone() or {}

    cursor.execute(
        f"""
        SELECT {col} AS value, COUNT(*) AS n
        FROM {table}
        WHERE {col} IS NOT NULL AND TRIM(CAST({col} AS CHAR)) <> ''
        GROUP BY {col}
        ORDER BY n DESC
        LIMIT %s
        """,
        (sample_limit,),
    )
    samples = [compact_value(row["value"]) for row in cursor.fetchall()]

    return {
        "total_rows": int(stats.get("total_rows") or 0),
        "empty_rows": int(stats.get("empty_rows") or 0),
        "distinct_values": int(stats.get("distinct_values") or 0),
        "samples": samples,
    }


def collect_schema(args: argparse.Namespace) -> dict:
    password = os.environ.get(args.password_env, "")
    conn = pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=password,
        database=args.database,
        charset=args.charset,
        connect_timeout=args.connect_timeout,
        cursorclass=DictCursor,
    )

    try:
        with conn.cursor() as cursor:
            tables = fetch_tables(cursor, args.database)
            columns_by_table = fetch_columns(cursor, args.database)
            indexes_by_table = fetch_indexes(cursor, args.database)
            views = fetch_views(cursor, args.database)

            for table in tables:
                name = table["name"]
                if args.exact_row_counts:
                    table["table_rows"] = fetch_exact_count(cursor, args.database, name)
                table["columns"] = columns_by_table.get(name, [])
                table["indexes"] = indexes_by_table.get(name, [])
                table["view_definition"] = views.get(name)
                table["create_sql"] = fetch_create_sql(cursor, args.database, name)

                if args.profile_columns:
                    for column in table["columns"]:
                        column["profile"] = profile_column(
                            cursor,
                            args.database,
                            name,
                            column,
                            args.sample_limit,
                        )

            return {"database": args.database, "tables": tables, "views": views}
    finally:
        conn.close()


def infer_join_hints(schema: dict) -> list[tuple[str, list[str]]]:
    by_column: dict[str, list[str]] = defaultdict(list)
    for table in schema.get("tables", []):
        for column in table.get("columns", []):
            name = column.get("name")
            if not name:
                continue
            by_column[name].append(table["name"])

    ignore = {"id", "ID", "序号", "备注", "创建时间", "更新时间"}
    hints = []
    for column_name, tables in by_column.items():
        unique_tables = sorted(set(tables))
        if column_name in ignore or len(unique_tables) < 2:
            continue
        hints.append((column_name, unique_tables))
    return sorted(hints, key=lambda item: (-len(item[1]), item[0]))


def format_profile(profile: dict | None) -> tuple[str, str, str]:
    if not profile:
        return "", "", ""
    if profile.get("skipped"):
        return profile["skipped"], "", ""
    total = profile.get("total_rows", 0)
    empty = profile.get("empty_rows", 0)
    distinct = profile.get("distinct_values", "")
    samples = ", ".join(profile.get("samples") or [])
    return f"{empty}/{total}", str(distinct), samples


def render_markdown_schema(schema: dict, generated_at: str | None = None) -> str:
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tables = schema.get("tables", [])

    lines: list[str] = [
        f"# {schema['database']} Text2SQL Schema",
        "",
        f"- Generated at: {generated_at}",
        f"- Database: `{schema['database']}`",
        f"- Objects: {len(tables)}",
        "",
        "## SQL 生成硬规则",
        "",
        "- 只生成单条 `SELECT` 查询；禁止 `INSERT`、`UPDATE`、`DELETE`、`DROP`、`TRUNCATE`、`ALTER`、多语句。",
        "- 表名和字段名必须来自本文档的白名单，中文表名和字段名需要用反引号包裹。",
        "- 不要使用 `SELECT *`；只选择回答问题必要的字段。",
        "- 面向列表、TopN、明细类问题必须加 `LIMIT`，除非用户明确要求全量导出。",
        "- 汇总口径要写清楚去重字段、时间字段、筛选条件和排序规则。",
        "",
        "## 表清单",
        "",
        "| 表/视图 | 类型 | 行数估计 | 引擎 | 排序规则 | 说明 |",
        "| --- | --- | ---: | --- | --- | --- |",
    ]

    for table in tables:
        lines.append(
            "| "
            f"`{markdown_cell(table['name'])}` | "
            f"{markdown_cell(table.get('table_type'))} | "
            f"{markdown_cell(table.get('table_rows'))} | "
            f"{markdown_cell(table.get('engine'))} | "
            f"{markdown_cell(table.get('table_collation'))} | "
            f"{markdown_cell(table.get('comment'))} |"
        )

    join_hints = infer_join_hints(schema)
    if join_hints:
        lines.extend(["", "## 可能的 JOIN 键", ""])
        for column_name, table_names in join_hints:
            joined_tables = ", ".join(f"`{markdown_cell(name)}`" for name in table_names)
            lines.append(f"- `{markdown_cell(column_name)}`: {joined_tables}")

    for table in tables:
        lines.extend(
            [
                "",
                f"### `{markdown_cell(table['name'])}`",
                "",
                f"- Type: {markdown_cell(table.get('table_type'))}",
                f"- Rows: {markdown_cell(table.get('table_rows'))}",
                f"- Comment: {markdown_cell(table.get('comment')) or '无'}",
                "",
                "| 字段 | 类型 | 可空 | 键 | 默认值 | 说明 | 空值/总行 | 去重值 | 高频样例 |",
                "| --- | --- | --- | --- | --- | --- | ---: | ---: | --- |",
            ]
        )
        for column in table.get("columns", []):
            empty_total, distinct, samples = format_profile(column.get("profile"))
            default = (
                "NULL"
                if column.get("column_default") is None
                else compact_value(column.get("column_default"))
            )
            lines.append(
                "| "
                f"`{markdown_cell(column.get('name'))}` | "
                f"{markdown_cell(column.get('column_type'))} | "
                f"{markdown_cell(column.get('is_nullable'))} | "
                f"{markdown_cell(column.get('column_key'))} | "
                f"{default} | "
                f"{markdown_cell(column.get('comment'))} | "
                f"{markdown_cell(empty_total)} | "
                f"{markdown_cell(distinct)} | "
                f"{markdown_cell(samples)} |"
            )

        if table.get("indexes"):
            lines.extend(["", "索引：", ""])
            lines.append("| 索引 | 唯一 | 字段 | 类型 | 说明 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for index in table["indexes"]:
                unique = "否" if int(index.get("non_unique") or 0) else "是"
                columns = ", ".join(f"`{markdown_cell(name)}`" for name in index.get("columns", []))
                lines.append(
                    "| "
                    f"`{markdown_cell(index.get('index_name'))}` | "
                    f"{unique} | "
                    f"{columns} | "
                    f"{markdown_cell(index.get('index_type'))} | "
                    f"{markdown_cell(index.get('comment'))} |"
                )

        if table.get("view_definition"):
            lines.extend(["", "视图定义：", "", "```sql", table["view_definition"], "```"])

        if table.get("create_sql"):
            lines.extend(["", "DDL：", "", "```sql", table["create_sql"], "```"])

    lines.append("")
    return "\n".join(lines)


def render_create_sql(schema: dict) -> str:
    lines = [
        f"-- {schema['database']} DDL exported for Text2SQL schema",
        "-- Generated by scripts/export_mysql_text2sql_schema.py",
        "",
    ]
    for table in schema.get("tables", []):
        create_sql = str(table.get("create_sql") or "").strip()
        if not create_sql:
            continue
        if not create_sql.endswith(";"):
            create_sql += ";"
        lines.extend([f"-- {table['name']}", create_sql, ""])
    return "\n".join(lines)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export MySQL schema, DDL and optional column profiles for Text2SQL."
    )
    parser.add_argument("--host", default=os.getenv("DB_HOST_SCENARIO_1_3"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DB_PORT_SCENARIO_1_3", "3306")))
    parser.add_argument("--database", default=os.getenv("DB_NAME_SCENARIO_1_3"))
    parser.add_argument("--user", default=os.getenv("DB_USER_SCENARIO_1_3"))
    parser.add_argument(
        "--password-env",
        default="DB_PASSWORD_SCENARIO_1_3",
        help="Environment variable that contains the MySQL password.",
    )
    parser.add_argument("--charset", default="utf8mb4")
    parser.add_argument("--connect-timeout", type=int, default=10)
    parser.add_argument("--sample-limit", type=int, default=5)
    parser.add_argument(
        "--profile-columns",
        action="store_true",
        help="Collect null counts, distinct counts and sample values for each column.",
    )
    parser.add_argument(
        "--exact-row-counts",
        action="store_true",
        help="Run COUNT(*) for every table instead of using information_schema estimates.",
    )
    parser.add_argument("--output-md", type=Path)
    parser.add_argument("--output-sql", type=Path)
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    missing = [name for name in ("host", "database", "user") if not getattr(args, name)]
    if missing:
        parser.error(f"missing required connection values: {', '.join(missing)}")

    if args.output_md is None:
        args.output_md = Path("schema") / f"{args.database}_text2sql_schema.md"
    if args.output_sql is None:
        args.output_sql = Path("schema") / f"{args.database}_ddl.sql"
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    schema = collect_schema(args)

    markdown = render_markdown_schema(schema)
    ddl = render_create_sql(schema)
    write_text(args.output_md, markdown)
    write_text(args.output_sql, ddl)

    print(f"[OK] Markdown schema: {args.output_md}")
    print(f"[OK] DDL SQL: {args.output_sql}")
    print(f"[INFO] Tables/views: {len(schema.get('tables', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
