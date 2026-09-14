"""SQL 执行后端。

规范 SQL 一律使用 MySQL 方言（与生产库一致）。DuckDB 演示库在执行前用 sqlglot 转换方言，
所以安全门、评测和提示词只需要面对一种方言。
"""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime
from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import duckdb
import sqlglot
from sqlglot import exp

from text2sql.config import MySQLConfig, Settings


class BackendError(RuntimeError):
    """后端错误基类。消息面向用户和修复节点，不包含连接凭据。"""


class QueryExecutionError(BackendError):
    pass


class QueryTimeoutError(BackendError):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    elapsed_ms: float
    executed_sql: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CostEstimate:
    max_cardinality: int | None
    detail: str = ""


class Backend(Protocol):
    name: str
    dialect: str

    def execute(self, sql: str, *, max_rows: int = 500) -> QueryResult: ...

    def distinct_values(
        self, table: str, column: str, *, limit: int = 30
    ) -> list[tuple[Any, int]]: ...

    def estimate_cost(self, sql: str) -> CostEstimate: ...

    def close(self) -> None: ...


def normalize_value(value: Any) -> Any:
    """把驱动返回值转成 JSON 安全的 Python 值。"""
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, datetime):
        # 与 MySQL 客户端一致：输出不带时区的本地时间；零点的值本质上是日期，只展示日期
        naive = value.replace(tzinfo=None)
        if naive.hour == naive.minute == naive.second == naive.microsecond == 0:
            return naive.date().isoformat()
        return naive.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, (date, dt_time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [normalize_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): normalize_value(v) for k, v in value.items()}
    return str(value)


def _naive_timestamps(node: exp.Expression) -> exp.Expression:
    # sqlglot 把 MySQL 的 TIMESTAMP 解析为 TIMESTAMPTZ；但 MySQL 客户端拿到的永远是不带时区的值，
    # 在 DuckDB 上保持 TIMESTAMPTZ 会让同一条 SQL 的结果随机器时区变化
    if isinstance(node, exp.DataType) and node.this == exp.DataType.Type.TIMESTAMPTZ:
        return exp.DataType.build("DATETIME")
    return node


def transpile(sql: str, *, to: str) -> str:
    if to == "mysql":
        return sql
    statements = sqlglot.parse(sql, read="mysql")
    return ";\n".join(s.transform(_naive_timestamps).sql(to) for s in statements if s is not None)


def _unique_columns(names: Iterable[str]) -> list[str]:
    """重复列名（如 a.eid 与 b.eid）追加序号，避免转成字典时静默覆盖。"""
    seen: dict[str, int] = {}
    result = []
    for name in names:
        count = seen.get(name, 0) + 1
        seen[name] = count
        result.append(name if count == 1 else f"{name}_{count}")
    return result


def _rows_to_result(
    description, raw_rows, *, max_rows: int, started: float, executed_sql: str
) -> QueryResult:
    columns = _unique_columns(str(d[0]) for d in (description or []))
    truncated = len(raw_rows) > max_rows
    kept = raw_rows[:max_rows]
    rows = [
        {col: normalize_value(val) for col, val in zip(columns, row, strict=False)} for row in kept
    ]
    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        executed_sql=executed_sql,
    )


def _max_numeric(node: Any, keys: set[str]) -> int | None:
    best: int | None = None
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys:
                try:
                    number = int(float(value))
                except (TypeError, ValueError):
                    number = None
                if number is not None:
                    best = number if best is None else max(best, number)
            child = _max_numeric(value, keys)
            if child is not None:
                best = child if best is None else max(best, child)
    elif isinstance(node, list):
        for item in node:
            child = _max_numeric(item, keys)
            if child is not None:
                best = child if best is None else max(best, child)
    return best


class DuckDBBackend:
    name = "duckdb-demo"
    dialect = "duckdb"

    def __init__(self, path: Path | str, *, timeout_s: float = 20.0) -> None:
        self.path = Path(path)
        self.timeout_s = timeout_s
        self._con = duckdb.connect(str(self.path), read_only=True)

    def execute(self, sql: str, *, max_rows: int = 500) -> QueryResult:
        executed = transpile(sql, to="duckdb")
        cursor = self._con.cursor()
        timer: threading.Timer | None = threading.Timer(self.timeout_s, cursor.interrupt)
        started = time.perf_counter()
        try:
            timer.start()
        except RuntimeError:  # 浏览器里的 Pyodide 不能启动线程，只能放弃超时中断
            timer = None
        try:
            cursor.execute(executed)
            description = cursor.description
            raw_rows = cursor.fetchmany(max_rows + 1)
        except duckdb.InterruptException as exc:
            raise QueryTimeoutError(f"查询超过 {self.timeout_s:g} 秒，已中断") from exc
        except duckdb.Error as exc:
            raise QueryExecutionError(str(exc)) from exc
        finally:
            if timer is not None:
                timer.cancel()
            cursor.close()
        return _rows_to_result(
            description, raw_rows, max_rows=max_rows, started=started, executed_sql=executed
        )

    def distinct_values(self, table: str, column: str, *, limit: int = 30) -> list[tuple[Any, int]]:
        col = '"' + column.replace('"', '""') + '"'
        tbl = '"' + table.replace('"', '""') + '"'
        sql = (
            f"SELECT {col} AS v, COUNT(*) AS n FROM {tbl} WHERE {col} IS NOT NULL "
            f"GROUP BY 1 ORDER BY n DESC, v LIMIT {int(limit)}"
        )
        cursor = self._con.cursor()
        try:
            return [(normalize_value(v), int(n)) for v, n in cursor.execute(sql).fetchall()]
        finally:
            cursor.close()

    def estimate_cost(self, sql: str) -> CostEstimate:
        cursor = self._con.cursor()
        try:
            rows = cursor.execute("EXPLAIN (FORMAT JSON) " + transpile(sql, to="duckdb")).fetchall()
            plan = json.loads(rows[0][1])
        except (duckdb.Error, ValueError, IndexError) as exc:
            return CostEstimate(max_cardinality=None, detail=f"无法估算：{exc}")
        finally:
            cursor.close()
        return CostEstimate(
            max_cardinality=_max_numeric(plan, {"Estimated Cardinality"}),
            detail="DuckDB EXPLAIN 估计基数最大值",
        )

    def close(self) -> None:
        self._con.close()


class MySQLBackend:
    name = "mysql"
    dialect = "mysql"

    def __init__(
        self,
        config: MySQLConfig,
        *,
        timeout_s: float = 20.0,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.timeout_s = timeout_s
        if connect is None:
            import pymysql

            connect = pymysql.connect
        self._connect = connect

    def _open(self):
        try:
            return self._connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
                charset=self.config.charset,
                connect_timeout=10,
                read_timeout=int(self.timeout_s) + 5,
                write_timeout=int(self.timeout_s) + 5,
                autocommit=True,
                # 会话级只读：即使安全门被绕过，数据库也拒绝任何写入
                init_command="SET SESSION TRANSACTION READ ONLY",
            )
        except Exception as exc:  # noqa: BLE001 - 驱动异常类型众多，统一转成后端错误
            raise BackendError(
                f"无法连接数据库 {self.config.host}:{self.config.port}：{exc}"
            ) from exc

    def execute(self, sql: str, *, max_rows: int = 500) -> QueryResult:
        started = time.perf_counter()
        conn = self._open()
        try:
            with conn.cursor() as cursor:
                cursor.execute(f"SET SESSION MAX_EXECUTION_TIME={int(self.timeout_s * 1000)}")
                cursor.execute(sql)
                description = cursor.description
                raw_rows = list(cursor.fetchmany(max_rows + 1))
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if "3024" in message or "maximum statement execution time exceeded" in message.lower():
                raise QueryTimeoutError(f"查询超过 {self.timeout_s:g} 秒，已被数据库终止") from exc
            raise QueryExecutionError(message) from exc
        finally:
            conn.close()
        return _rows_to_result(
            description, raw_rows, max_rows=max_rows, started=started, executed_sql=sql
        )

    def distinct_values(self, table: str, column: str, *, limit: int = 30) -> list[tuple[Any, int]]:
        col = "`" + column.replace("`", "``") + "`"
        tbl = "`" + table.replace("`", "``") + "`"
        result = self.execute(
            f"SELECT {col} AS v, COUNT(*) AS n FROM {tbl} WHERE {col} IS NOT NULL "
            f"GROUP BY {col} ORDER BY n DESC LIMIT {int(limit)}",
            max_rows=int(limit),
        )
        return [(row["v"], int(row["n"])) for row in result.rows]

    def estimate_cost(self, sql: str) -> CostEstimate:
        conn = self._open()
        try:
            with conn.cursor() as cursor:
                cursor.execute("EXPLAIN FORMAT=JSON " + sql)
                rows = cursor.fetchall()
            plan = json.loads(rows[0][0])
        except Exception as exc:  # noqa: BLE001
            return CostEstimate(max_cardinality=None, detail=f"无法估算：{exc}")
        finally:
            conn.close()
        return CostEstimate(
            max_cardinality=_max_numeric(
                plan, {"rows_produced_per_join", "rows_examined_per_scan"}
            ),
            detail="MySQL EXPLAIN 估计行数最大值",
        )

    def close(self) -> None:  # 每次查询独立连接，无需释放
        return None


def build_backend(settings: Settings) -> Backend:
    if settings.database == "mysql":
        assert settings.mysql is not None
        return MySQLBackend(settings.mysql, timeout_s=settings.query_timeout_s)
    from text2sql.db.demo import ensure_demo_database

    path = ensure_demo_database(settings.demo_db_path)
    return DuckDBBackend(path, timeout_s=settings.query_timeout_s)
