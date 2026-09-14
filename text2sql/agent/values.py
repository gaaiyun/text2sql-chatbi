"""值域索引：从当前连接扫描低基数列的真实取值。

生产 NL2SQL 最危险的错误不报错：`WHERE status = '存续'` 合法、能执行、返回 0 行。
这里在进程启动时直接用正在服务的数据库扫一遍取值（而不是离线生成后随仓库提交），
所以不存在“数据变了、索引没更新”的漂移窗口。

扫描到的取值数超过上限时，索引标记为不完整：不完整的列只能证明“某值存在”，不能证明“某值不存在”。
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from text2sql.semantic.catalog import SemanticCatalog
from text2sql.semantic.parser import normalize_question


def _display(value: Any) -> str:
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return str(value)


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass
class ColumnValues:
    table: str
    column: str
    label: str
    values: list[tuple[Any, int]]
    complete: bool
    value_hint: str | None = None

    @property
    def numeric(self) -> bool:
        return bool(self.values) and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v, _ in self.values
        )


@dataclass
class ValueIndex:
    columns: dict[tuple[str, str], ColumnValues] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def get(self, table: str, column: str) -> ColumnValues | None:
        key = (table.strip('`"'), column.strip('`"').lower())
        return next(
            (c for (t, col), c in self.columns.items() if t == key[0] and col.lower() == key[1]),
            None,
        )

    def known(self, table: str, column: str) -> set[str] | None:
        entry = self.get(table, column)
        return None if entry is None else {_display(v) for v, _ in entry.values}

    def contains(self, table: str, column: str, literal: Any) -> bool | None:
        entry = self.get(table, column)
        if entry is None:
            return None
        if entry.numeric:
            target = _as_number(literal)
            found = target is not None and any(
                abs(float(v) - target) < 1e-9 for v, _ in entry.values
            )
        else:
            found = any(str(v) == str(literal) for v, _ in entry.values)
        if found:
            return True
        return False if entry.complete else None

    def matches_like(self, table: str, column: str, pattern: str) -> bool | None:
        entry = self.get(table, column)
        if entry is None:
            return None
        regex = re.compile(
            "^"
            + "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern)
            + "$",
            re.DOTALL,
        )
        if any(regex.match(_display(v)) for v, _ in entry.values):
            return True
        return False if entry.complete else None

    def mentions(self, question: str) -> list[tuple[str, str, str]]:
        text = normalize_question(question)
        found = []
        for (table, column), entry in self.columns.items():
            for value, _ in entry.values:
                shown = _display(value)
                if isinstance(value, str) and len(shown) >= 2 and normalize_question(shown) in text:
                    found.append((table, column, shown))
        return found

    def hints_for(
        self, tables: Iterable[str], *, per_column: int = 12, max_values: int = 30
    ) -> list[str]:
        """提示词里的取值清单。取值多于 max_values 的列（国家名、商品名）只用于识别问题里的取值，不逐个列出。"""
        wanted = list(dict.fromkeys(tables))
        lines = []
        for table in wanted:
            for (t, column), entry in self.columns.items():
                if t != table or not entry.values or len(entry.values) > max_values:
                    continue
                shown = "、".join(
                    (_display(v) if entry.numeric else f"'{_display(v)}'") + f"({n})"
                    for v, n in entry.values[:per_column]
                )
                more = "" if entry.complete and len(entry.values) <= per_column else "……"
                note = f"；{entry.value_hint}" if entry.value_hint else ""
                lines.append(f"- `{table}`.{column}（{entry.label}）取值：{shown}{more}{note}")
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": {
                f"{t}.{c}": {
                    "values": [[_display(v), n] for v, n in e.values],
                    "complete": e.complete,
                }
                for (t, c), e in self.columns.items()
            },
            "skipped": self.skipped,
            "elapsed_ms": self.elapsed_ms,
        }


def build_value_index(
    backend: Any,
    catalog: SemanticCatalog,
    *,
    per_column_limit: int = 30,
    time_budget_s: float = 10.0,
) -> ValueIndex:
    index = ValueIndex()
    started = time.perf_counter()
    for table, column in catalog.value_scan_targets():
        if time.perf_counter() - started >= time_budget_s:
            index.skipped.append(f"{table}.{column}：超出扫描时间预算，未纳入索引")
            continue
        try:
            rows = backend.distinct_values(table, column, limit=per_column_limit + 1)
        except Exception as exc:  # noqa: BLE001 - 扫描失败只影响提示质量，不影响启动
            index.skipped.append(f"{table}.{column}：{exc}")
            continue
        doc = catalog.tables[table].columns[column]
        index.columns[(table, column)] = ColumnValues(
            table=table,
            column=column,
            label=doc.label,
            values=list(rows[:per_column_limit]),
            complete=len(rows) <= per_column_limit,
            value_hint=doc.value_hint,
        )
    index.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    return index
