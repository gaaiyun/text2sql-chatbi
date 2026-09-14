"""SQL 安全门测试。前半部分移植自 v1 的 safe_sql / sql_injection 测试语料，后半部分覆盖 v2 新增的列级校验。"""

from __future__ import annotations

import json

import pytest

from text2sql.db.schema import PhysicalSchema, load_znjz_schema
from text2sql.semantic.catalog import load_znjz_catalog
from text2sql.sql.guard import SQLGuard


@pytest.fixture(scope="module")
def guard() -> SQLGuard:
    return SQLGuard.from_catalog(load_znjz_catalog(), load_znjz_schema(), max_rows=500)


def rejected(guard: SQLGuard, sql: str, code: str | None = None):
    report = guard.check(sql)
    assert report.is_safe is False, sql
    assert report.safe_sql is None
    if code:
        assert report.error_code == code, (sql, report.error_code, report.errors)
    return report


# --------------------------------------------------------------------------- 基础放行与 LIMIT


def test_select_is_allowed_and_limit_is_added(guard):
    report = guard.check("SELECT `status`, COUNT(*) AS cnt FROM `企业基本信息` GROUP BY `status`")

    assert report.is_safe
    assert report.safe_sql.endswith("LIMIT 500")
    assert report.modifications == ["补充 LIMIT 500"]
    assert report.referenced_tables == ["企业基本信息"]


def test_original_text_is_kept_when_no_rewrite_needed(guard):
    sql = "SELECT e.`name` FROM `企业基本信息` e WHERE e.`start_date` IS NOT NULL LIMIT 20"
    report = guard.check(sql + ";")

    assert report.safe_sql == sql
    assert report.modifications == []


def test_limit_above_maximum_is_capped(guard):
    report = guard.check("SELECT eid FROM `企业基本信息` LIMIT 999999")

    assert report.safe_sql.endswith("LIMIT 500")
    assert report.modifications == ["LIMIT 999999 超过上限，收紧为 500"]


def test_mysql_offset_limit_syntax_is_capped_on_row_count(guard):
    report = guard.check("SELECT eid FROM `企业基本信息` LIMIT 100, 5000")

    assert report.is_safe
    assert "5000" not in report.safe_sql
    assert "500" in report.safe_sql


def test_limit_added_to_union(guard):
    report = guard.check("SELECT `round` AS v FROM `融资数据` UNION SELECT `state` FROM `标签数据`")

    assert report.is_safe
    assert report.safe_sql.endswith("LIMIT 500")


def test_trailing_line_comment_cannot_swallow_added_limit(guard):
    import sqlglot

    report = guard.check("SELECT eid FROM `企业基本信息` -- 取企业")

    assert report.is_safe
    reparsed = sqlglot.parse_one(report.safe_sql, read="mysql")
    assert reparsed.args.get("limit") is not None


def test_cte_and_subquery_aliases_are_not_tables(guard):
    sql = """
    WITH bids AS (SELECT eid, COUNT(*) AS n FROM `招投标` GROUP BY eid)
    SELECT e.`name`, COALESCE(f.fin_cnt, 0) AS fin_cnt, bids.n
    FROM `企业基本信息` e
    JOIN bids ON bids.eid = e.eid
    LEFT JOIN (SELECT eid, COUNT(*) AS fin_cnt FROM `融资数据` GROUP BY eid) f ON f.eid = e.eid
    ORDER BY bids.n DESC LIMIT 10
    """
    report = guard.check(sql)

    assert report.is_safe, report.errors
    assert report.referenced_tables == ["企业基本信息", "招投标", "融资数据"]


def test_select_alias_can_be_used_in_group_and_order(guard):
    report = guard.check(
        "SELECT YEAR(start_date) AS y, COUNT(*) AS n FROM `企业基本信息` GROUP BY y ORDER BY y"
    )
    assert report.is_safe, report.errors


def test_count_star_is_allowed(guard):
    assert guard.check("SELECT COUNT(*) FROM `招投标`").is_safe


def test_single_row_aggregate_needs_no_limit(guard):
    """没有 GROUP BY 的纯聚合最多返回一行，补 LIMIT 只会在轨迹里制造噪音。"""
    report = guard.check(
        "SELECT COUNT(DISTINCT eid) AS n, AVG(regist_capi_new) AS avg_cap FROM `企业基本信息`"
    )

    assert report.is_safe
    assert report.modifications == []
    assert "LIMIT" not in report.safe_sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT status, COUNT(*) FROM `企业基本信息` GROUP BY status",
        "SELECT COUNT(*) OVER () AS n FROM `企业基本信息`",
        "SELECT eid, COUNT(*) OVER (PARTITION BY status) FROM `企业基本信息`",
    ],
)
def test_grouped_or_windowed_aggregates_still_get_limit(guard, sql):
    assert "LIMIT 500" in guard.check(sql).safe_sql


def test_unicode_literals_are_allowed(guard):
    assert guard.check("SELECT eid FROM `企业基本信息` WHERE `status` LIKE '存续%'").is_safe


def test_keywords_inside_string_literals_are_not_false_positives(guard):
    """v1 用正则黑名单，标题里出现 DROP/UPDATE 会被误杀；AST 检查只看结构。"""
    report = guard.check(
        "SELECT `title` FROM `招投标` WHERE `title` LIKE '%UPDATE%' OR `title` LIKE '%DROP%'"
    )
    assert report.is_safe


# --------------------------------------------------------------------------- 写操作与多语句


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO `企业基本信息` (name) VALUES ('hack')",
        "UPDATE `企业基本信息` SET name='x' WHERE eid='1'",
        "DELETE FROM `企业基本信息` WHERE eid='1'",
        "DROP TABLE `企业基本信息`",
        "TRUNCATE TABLE `招投标信息`",
        "ALTER TABLE `企业基本信息` ADD COLUMN x INT",
        "CREATE TABLE x (id INT)",
        "GRANT SELECT ON `企业基本信息` TO admin",
        "REPLACE INTO `企业基本信息` (name) VALUES ('x')",
        "CALL refresh_all()",
        "LOCK TABLES `企业基本信息` READ",
        "SHOW TABLES",
        "EXPLAIN SELECT 1",
        "SET GLOBAL max_connections = 1",
        "drop table `企业基本信息`",
        "DrOp TaBlE `企业基本信息`",
        "WITH x AS (SELECT 1) DELETE FROM `企业基本信息`",
    ],
)
def test_non_select_statements_are_rejected(guard, sql):
    report = rejected(guard, sql)
    assert report.error_code in {"not_select", "parse_error", "forbidden_node"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT eid FROM `企业基本信息`; DROP TABLE `企业基本信息`;",
        "SELECT eid FROM `企业基本信息`; SELECT eid FROM `招投标`",
        "SELECT eid FROM `企业基本信息`; DELETE FROM `融资数据`; INSERT INTO `标签数据` VALUES (1)",
    ],
)
def test_multiple_statements_are_rejected(guard, sql):
    rejected(guard, sql)


def test_trailing_semicolon_is_a_single_statement(guard):
    assert guard.check("SELECT eid FROM `企业基本信息`;").is_safe


# --------------------------------------------------------------------------- 危险构造


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT eid FROM `企业基本信息` INTO OUTFILE '/tmp/x.csv'", None),
        ("SELECT eid FROM `企业基本信息` INTO DUMPFILE '/tmp/x.bin'", None),
        ("SELECT LOAD_FILE('/etc/passwd') FROM `企业基本信息`", "forbidden_function"),
        ("SELECT SLEEP(10)", "forbidden_function"),
        ("SELECT BENCHMARK(100000000, MD5('x'))", "forbidden_function"),
        ("SELECT USER()", "forbidden_function"),
        ("SELECT CURRENT_USER()", "forbidden_function"),
        ("SELECT DATABASE()", "forbidden_function"),
        ("SELECT VERSION()", "forbidden_function"),
        ("SELECT @@version", "forbidden_node"),
        ("SELECT @x := 1", "forbidden_node"),
        ("SELECT eid FROM `企业基本信息` FOR UPDATE", "forbidden_node"),
        ("SELECT UPDATEXML(1, CONCAT(0x7e, 'x'), 1)", "forbidden_function"),
    ],
)
def test_dangerous_constructs_are_rejected(guard, sql, code):
    rejected(guard, sql, code)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT table_name FROM information_schema.tables",
        "SELECT user FROM mysql.user",
        "SELECT * FROM performance_schema.threads",
        "SELECT eid FROM other_db.`企业基本信息`",
    ],
)
def test_cross_database_access_is_rejected(guard, sql):
    report = rejected(guard, sql)
    assert report.error_code in {"cross_database", "select_star"}


def test_non_whitelisted_table_is_rejected(guard):
    report = rejected(guard, "SELECT id FROM `users`", "table_not_allowed")
    assert "users" in report.errors[0]


def test_select_star_is_rejected_with_actionable_message(guard):
    report = rejected(guard, "SELECT * FROM `融资数据`", "select_star")
    assert "字段" in report.errors[0]
    rejected(guard, "SELECT f.* FROM `融资数据` f", "select_star")


def test_sensitive_columns_are_rejected_even_when_aliased(guard):
    rejected(guard, "SELECT oper_name FROM `企业基本信息`", "sensitive_column")
    rejected(
        guard,
        "SELECT e.name FROM `企业基本信息` e WHERE e.`OPER_NAME` LIKE '陈%'",
        "sensitive_column",
    )
    rejected(guard, "SELECT invest_oper_name AS x FROM `企业投资股东信息`", "sensitive_column")


# --------------------------------------------------------------------------- 列级校验


def test_hallucinated_column_is_rejected_before_execution_with_hint(guard):
    report = rejected(
        guard,
        "SELECT `industry_name`, COUNT(*) AS cnt FROM `企业行业代码` GROUP BY `industry_name`",
        "unknown_column",
    )
    assert "industry_name" in report.errors[0]
    assert any("industry_code" in hint for hint in report.hints)


def test_view_specific_missing_column_is_caught(guard):
    """投资数据 视图没有 should_capi_conv，只有宽表 企业投资股东信息 有。"""
    report = rejected(guard, "SELECT `should_capi_conv` FROM `投资数据`", "unknown_column")
    assert any("企业投资股东信息" in hint for hint in report.hints)


def test_unknown_qualified_column_is_rejected(guard):
    rejected(
        guard,
        "SELECT e.name FROM `企业基本信息` e WHERE e.company_name LIKE '%科技%'",
        "unknown_column",
    )


def test_common_hallucinations_get_semantic_hints(guard):
    report = rejected(guard, "SELECT company_name FROM `企业基本信息`", "unknown_column")
    assert any("name" in hint for hint in report.hints)


def test_hints_never_suggest_sensitive_columns(guard):
    report = rejected(guard, "SELECT company_name FROM `企业基本信息`", "unknown_column")
    assert not any("oper_name" in hint for hint in report.hints)


def test_ambiguous_column_is_rejected_with_alias_advice(guard):
    report = rejected(
        guard,
        "SELECT eid FROM `企业基本信息` e JOIN `融资数据` f ON f.eid = e.eid",
        "ambiguous_column",
    )
    assert "别名" in report.errors[0]


def test_referenced_columns_are_reported(guard):
    report = guard.check(
        "SELECT e.`name`, f.`round` FROM `企业基本信息` e JOIN `融资数据` f ON f.eid = e.eid"
    )

    assert report.is_safe
    assert {"企业基本信息.name", "融资数据.round", "企业基本信息.eid", "融资数据.eid"} <= set(
        report.referenced_columns
    )


def test_uppercase_column_names_resolve_like_mysql(guard):
    assert guard.check("SELECT STATUS FROM `企业基本信息`").is_safe


# --------------------------------------------------------------------------- 边界


@pytest.mark.parametrize("sql", ["", "   \n  "])
def test_empty_sql_is_rejected(guard, sql):
    rejected(guard, sql, "empty")


def test_overlong_sql_is_rejected(guard):
    sql = "SELECT eid FROM `企业基本信息` WHERE " + " OR ".join(f"eid = '{i}'" for i in range(2000))
    rejected(guard, sql, "too_long")


def test_unparseable_sql_is_rejected(guard):
    rejected(guard, "SELEC eid FORM `企业基本信息`", "parse_error")


def test_report_is_json_serializable(guard):
    json.dumps(guard.check("SELECT eid FROM `企业基本信息`").to_dict(), ensure_ascii=False)
    json.dumps(guard.check("DROP TABLE x").to_dict(), ensure_ascii=False)


def test_generic_schema_without_catalog():
    schema = PhysicalSchema.from_ddl(
        "CREATE TABLE users (id bigint, name varchar(20)); CREATE TABLE orders (id bigint, user_id bigint);"
    )
    guard = SQLGuard(schema, allowed_tables=["users", "orders"], max_rows=42)

    report = guard.check("SELECT u.id FROM users u JOIN orders o ON u.id = o.user_id")
    assert report.is_safe
    assert report.safe_sql.endswith("LIMIT 42")
    assert guard.check("SELECT id FROM secrets").error_code == "table_not_allowed"
