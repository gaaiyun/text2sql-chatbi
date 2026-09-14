from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import pytest

from text2sql.config import MySQLConfig, Settings
from text2sql.db import demo
from text2sql.db.backends import (
    DuckDBBackend,
    MySQLBackend,
    QueryExecutionError,
    QueryTimeoutError,
    build_backend,
    normalize_value,
    transpile,
)


def test_transpile_mysql_to_duckdb_keeps_chinese_identifiers():
    sql = "SELECT YEAR(`start_date`) AS y FROM `企业基本信息` LIMIT 5, 10"
    out = transpile(sql, to="duckdb")

    assert '"企业基本信息"' in out
    assert "OFFSET 5" in out
    assert "`" not in out


def test_transpile_to_mysql_is_identity():
    sql = "SELECT `status` FROM `企业基本信息`"
    assert transpile(sql, to="mysql") == sql


def test_transpile_keeps_mysql_timestamps_timezone_naive():
    out = transpile(
        "SELECT CAST(x AS TIMESTAMP), TIMESTAMP '2024-01-01 00:00:00' FROM t", to="duckdb"
    )

    assert "TIMESTAMPTZ" not in out
    assert "TIMESTAMP" in out


def test_midnight_datetimes_render_as_dates():
    """DATETIME 列里大量是 00:00:00 的纯日期，展示成 2025-05-27 比 2025-05-27 00:00:00 更可读。"""
    assert normalize_value(datetime(2025, 5, 27)) == "2025-05-27"


def test_normalize_value_drops_timezone_like_mysql_clients():
    from datetime import timedelta, timezone

    aware = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=8)))
    assert normalize_value(aware) == "2024-01-02 03:04:05"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (Decimal("1.50"), 1.5),
        (datetime(2024, 1, 2, 3, 4, 5), "2024-01-02 03:04:05"),
        (float("nan"), None),
        (float("inf"), None),
        (None, None),
        ("存续", "存续"),
        (b"\x01\x02", "0102"),
        (7, 7),
    ],
)
def test_normalize_value_is_json_safe(raw, expected):
    assert normalize_value(raw) == expected
    json.dumps(normalize_value(raw))


def test_duckdb_backend_executes_mysql_dialect(demo_backend):
    result = demo_backend.execute("SELECT COUNT(*) AS n FROM `企业基本信息`", max_rows=10)

    assert result.columns == ["n"]
    assert result.rows == [{"n": demo.DEMO_ENTERPRISES}]
    assert result.row_count == 1
    assert result.truncated is False
    assert '"企业基本信息"' in result.executed_sql
    assert result.elapsed_ms >= 0


def test_duckdb_backend_normalizes_decimal_and_timestamps(demo_backend):
    result = demo_backend.execute(
        "SELECT CAST(1.25 AS DECIMAL(10,2)) AS d, TIMESTAMP '2024-05-06 07:08:09' AS t, NULL AS z",
        max_rows=5,
    )
    assert result.rows == [{"d": 1.25, "t": "2024-05-06 07:08:09", "z": None}]


def test_duckdb_backend_marks_truncation(demo_backend):
    result = demo_backend.execute("SELECT eid FROM `企业基本信息`", max_rows=7)

    assert result.row_count == 7
    assert result.truncated is True


def test_duckdb_backend_wraps_database_errors(demo_backend):
    with pytest.raises(QueryExecutionError) as info:
        demo_backend.execute("SELECT industry_name FROM `企业行业代码`", max_rows=5)
    assert "industry_name" in str(info.value)


def test_duckdb_backend_interrupts_slow_queries(demo_db_path):
    backend = DuckDBBackend(demo_db_path, timeout_s=0.3)
    try:
        with pytest.raises(QueryTimeoutError):
            backend.execute(
                "SELECT COUNT(*) FROM `招投标` a, `招投标` b WHERE a.title < b.title",
                max_rows=5,
            )
        # 中断之后连接仍可继续使用
        assert backend.execute("SELECT 1 AS ok", max_rows=1).rows == [{"ok": 1}]
    finally:
        backend.close()


def test_duckdb_backend_runs_without_thread_support(demo_backend, monkeypatch):
    """浏览器里的 Pyodide 不能启动线程：放弃超时中断，查询照常执行。"""
    import threading

    def no_threads(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Timer, "start", no_threads)

    result = demo_backend.execute("SELECT 1 AS n", max_rows=5)

    assert result.rows == [{"n": 1}]


def test_duckdb_distinct_values_are_sorted_by_frequency(demo_backend):
    values = demo_backend.distinct_values("企业基本信息", "status", limit=3)

    assert values[0][0] == "存续（在营、开业、在册）"
    assert len(values) == 3
    assert values[0][1] >= values[1][1] >= values[2][1]


def test_duckdb_cost_estimate_flags_cartesian_sort(demo_backend):
    cheap = demo_backend.estimate_cost(
        "SELECT status, COUNT(*) FROM `企业基本信息` GROUP BY status"
    )
    costly = demo_backend.estimate_cost(
        "SELECT a.title FROM `招投标` a, `招投标` b, `企业基本信息` c ORDER BY a.title LIMIT 10"
    )
    streaming = demo_backend.estimate_cost("SELECT a.title FROM `招投标` a, `招投标` b LIMIT 10")

    assert cheap.max_cardinality is not None and cheap.max_cardinality < 100_000
    # 必须物化的排序会报出笛卡尔积规模；能流式取前 N 行的 LIMIT 不会
    assert costly.max_cardinality is not None and costly.max_cardinality > 10_000_000_000
    assert streaming.max_cardinality is not None and streaming.max_cardinality < 1_000_000


class _FakeCursor:
    def __init__(self, rows, description, fail=False):
        self.executed: list[str] = []
        self._rows = rows
        self.description = description
        self._fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if self._fail and not sql.startswith("SET"):
            raise RuntimeError("(1054, \"Unknown column 'industry_name' in 'field list'\")")

    def fetchmany(self, size):
        return self._rows[:size]

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def _mysql_backend(rows, description, fail=False):
    calls = {}
    cursor = _FakeCursor(rows, description, fail=fail)
    connection = _FakeConnection(cursor)

    def connect(**kwargs):
        calls.update(kwargs)
        return connection

    config = MySQLConfig(
        host="db.example", port=3306, user="reader", password="pw", database="znjz"
    )
    return MySQLBackend(config, timeout_s=15, connect=connect), calls, cursor, connection


def test_mysql_backend_uses_read_only_session_and_execution_timeout():
    backend, calls, cursor, connection = _mysql_backend(
        rows=[("存续（在营、开业、在册）", Decimal("17477"))],
        description=[("status",), ("cnt",)],
    )

    result = backend.execute(
        "SELECT `status`, COUNT(*) AS cnt FROM `企业基本信息` GROUP BY `status`", max_rows=100
    )

    assert "READ ONLY" in calls["init_command"]
    assert calls["host"] == "db.example"
    assert cursor.executed[0] == "SET SESSION MAX_EXECUTION_TIME=15000"
    assert result.rows == [{"status": "存续（在营、开业、在册）", "cnt": 17477.0}]
    assert result.executed_sql.startswith("SELECT `status`")
    assert connection.closed is True


def test_mysql_backend_closes_connection_on_error():
    backend, _, _, connection = _mysql_backend(rows=[], description=None, fail=True)

    with pytest.raises(QueryExecutionError) as info:
        backend.execute("SELECT industry_name FROM `企业行业代码`", max_rows=5)

    assert "Unknown column" in str(info.value)
    assert connection.closed is True


def test_mysql_cost_estimate_reads_explain_json():
    plan = {
        "query_block": {
            "nested_loop": [
                {
                    "table": {
                        "table_name": "a",
                        "rows_examined_per_scan": 576690,
                        "rows_produced_per_join": 576690,
                    }
                },
                {
                    "table": {
                        "table_name": "b",
                        "rows_examined_per_scan": 576690,
                        "rows_produced_per_join": 332571356100,
                    }
                },
            ]
        }
    }
    backend, _, cursor, _ = _mysql_backend(rows=[(json.dumps(plan),)], description=[("EXPLAIN",)])

    estimate = backend.estimate_cost("SELECT a.title FROM `招投标` a, `招投标` b")

    assert estimate.max_cardinality == 332571356100
    assert cursor.executed[-1].startswith("EXPLAIN FORMAT=JSON")


def test_build_backend_prefers_mysql_when_configured():
    settings = Settings.from_mapping(
        {
            "DB_HOST_SCENARIO_1_3": "db.example",
            "DB_USER_SCENARIO_1_3": "reader",
            "DB_PASSWORD_SCENARIO_1_3": "pw",
        }
    )
    backend = build_backend(settings)

    assert isinstance(backend, MySQLBackend)
    assert backend.name == "mysql"


def test_build_backend_builds_demo_database_when_missing(tmp_path):
    path = tmp_path / "fresh" / "demo.duckdb"
    settings = Settings.from_mapping({"T2S_DEMO_DB_PATH": str(path)})

    backend = build_backend(settings)
    try:
        assert isinstance(backend, DuckDBBackend)
        assert backend.name == "duckdb-demo"
        assert path.exists()
        assert backend.execute("SELECT COUNT(*) AS n FROM `融资数据`", max_rows=1).rows[0]["n"] > 0
    finally:
        backend.close()
