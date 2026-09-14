from __future__ import annotations

import duckdb
import pytest

from text2sql.db import demo
from text2sql.db.schema import load_znjz_schema


def _scalar(con, sql):
    return con.execute(sql).fetchone()[0]


@pytest.fixture(scope="module")
def con(demo_db_path):
    connection = duckdb.connect(str(demo_db_path), read_only=True)
    yield connection
    connection.close()


def test_every_production_object_exists_with_identical_columns(con):
    schema = load_znjz_schema()
    for table in schema.tables.values():
        rows = con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [table.name],
        ).fetchall()
        assert [r[0] for r in rows] == [c.name for c in table.columns], table.name


def test_views_keep_only_real_event_rows(con):
    assert _scalar(con, 'SELECT COUNT(*) FROM "融资数据"') == _scalar(
        con, 'SELECT COUNT(*) FROM "企业融资信息" WHERE cf_id IS NOT NULL'
    )
    assert _scalar(con, 'SELECT COUNT(*) FROM "招投标"') == _scalar(
        con, 'SELECT COUNT(*) FROM "招投标信息" WHERE cbid_id IS NOT NULL'
    )


def test_wide_tables_keep_placeholder_rows_like_production(con):
    """生产宽表里没有事件的企业也占一行，漏掉 cf_id IS NOT NULL 会把企业数算成事件数。"""
    enterprises = _scalar(con, 'SELECT COUNT(*) FROM "企业基本信息"')
    placeholders = _scalar(con, 'SELECT COUNT(*) FROM "企业融资信息" WHERE cf_id IS NULL')
    financed = _scalar(
        con, 'SELECT COUNT(DISTINCT cf_eid) FROM "企业融资信息" WHERE cf_id IS NOT NULL'
    )

    assert placeholders == enterprises - financed
    assert _scalar(con, 'SELECT COUNT(*) FROM "招投标信息" WHERE cbid_id IS NULL') > 0


def test_enterprise_scale_and_uniqueness(con):
    assert _scalar(con, 'SELECT COUNT(*) FROM "企业基本信息"') == demo.DEMO_ENTERPRISES
    assert _scalar(con, 'SELECT COUNT(DISTINCT eid) FROM "企业基本信息"') == demo.DEMO_ENTERPRISES
    assert _scalar(con, 'SELECT COUNT(DISTINCT name) FROM "企业基本信息"') == demo.DEMO_ENTERPRISES


def test_value_traps_from_production_are_reproduced(con):
    statuses = {r[0] for r in con.execute('SELECT DISTINCT status FROM "企业基本信息"').fetchall()}
    assert "存续（在营、开业、在册）" in statuses
    assert "存续" not in statuses

    area = _scalar(con, 'SELECT area_code FROM "招投标" WHERE area_code IS NOT NULL LIMIT 1')
    assert area.endswith(".0")

    states = {r[0] for r in con.execute('SELECT DISTINCT state FROM "标签数据"').fetchall()}
    assert {"有效", "过期"} <= states


def test_fact_tables_have_enough_rows_for_analysis(con):
    assert _scalar(con, 'SELECT COUNT(DISTINCT eid) FROM "融资数据"') >= 50
    assert _scalar(con, 'SELECT COUNT(*) FROM "招投标"') >= 10_000
    assert _scalar(con, 'SELECT COUNT(*) FROM "投资数据"') >= 1_000
    assert _scalar(con, 'SELECT COUNT(*) FROM "标签数据"') >= 1_000
    years = _scalar(con, 'SELECT COUNT(DISTINCT YEAR(publish_time)) FROM "招投标"')
    assert years >= 10


def test_manufacturing_section_is_not_empty(con):
    count = _scalar(con, "SELECT COUNT(*) FROM \"企业行业代码\" WHERE industry_code LIKE 'C%'")
    assert count > 0


def test_event_dates_never_precede_company_founding(con):
    violations = _scalar(
        con,
        'SELECT COUNT(*) FROM "融资数据" f JOIN "企业基本信息" e ON e.eid = f.eid '
        "WHERE f.round_date < e.start_date",
    )
    assert violations == 0


def test_generation_is_deterministic(tmp_path, con):
    other = tmp_path / "again.duckdb"
    demo.build_demo_database(other)
    with duckdb.connect(str(other), read_only=True) as again:
        for sql in (
            "SELECT string_agg(eid, ',' ORDER BY eid) FROM \"企业基本信息\"",
            'SELECT COUNT(*), SUM(project_bid_money) FROM "招投标"',
        ):
            assert again.execute(sql).fetchone() == con.execute(sql).fetchone()


def test_meta_table_records_generator_version(con):
    assert _scalar(con, "SELECT value FROM _t2s_meta WHERE key = 'version'") == demo.DEMO_VERSION


def test_ensure_demo_database_rebuilds_on_version_mismatch(tmp_path):
    path = tmp_path / "stale.duckdb"
    with duckdb.connect(str(path)) as stale:
        stale.execute("CREATE TABLE _t2s_meta (key VARCHAR, value VARCHAR)")
        stale.execute("INSERT INTO _t2s_meta VALUES ('version', 'old')")

    demo.ensure_demo_database(path)

    with duckdb.connect(str(path), read_only=True) as fresh:
        assert (
            _scalar(fresh, "SELECT value FROM _t2s_meta WHERE key = 'version'") == demo.DEMO_VERSION
        )
        assert _scalar(fresh, 'SELECT COUNT(*) FROM "企业基本信息"') == demo.DEMO_ENTERPRISES


def test_ensure_demo_database_keeps_current_build(demo_db_path):
    before = demo_db_path.stat().st_mtime_ns
    demo.ensure_demo_database(demo_db_path)
    assert demo_db_path.stat().st_mtime_ns == before
