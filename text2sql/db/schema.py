"""从生产 MySQL DDL 解析物理 schema。

DDL 是演示库建表与 SQL 安全门列级校验的唯一依据：两者看到的表、视图和列完全一致，
演示库上能通过校验的 SQL，在生产库上同样引用的是真实存在的列。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import sqlglot
from sqlglot import exp

ZNJZ_DDL_PATH = Path(__file__).resolve().parents[1] / "datasets" / "znjz" / "schema.sql"

_TYPE_FAMILIES = {
    exp.DataType.Type.BIGINT: "bigint",
    exp.DataType.Type.INT: "int",
    exp.DataType.Type.SMALLINT: "int",
    exp.DataType.Type.TINYINT: "int",
    exp.DataType.Type.VARCHAR: "varchar",
    exp.DataType.Type.CHAR: "varchar",
    exp.DataType.Type.TEXT: "text",
    exp.DataType.Type.MEDIUMTEXT: "text",
    exp.DataType.Type.LONGTEXT: "text",
    exp.DataType.Type.DOUBLE: "double",
    exp.DataType.Type.FLOAT: "double",
    exp.DataType.Type.DECIMAL: "decimal",
    exp.DataType.Type.DATETIME: "datetime",
    exp.DataType.Type.TIMESTAMP: "datetime",
    exp.DataType.Type.DATE: "date",
    exp.DataType.Type.BINARY: "binary",
}

# sqlglot 优化器使用的类型名；只用于列存在性与类型推断，不需要精确长度
_SQLGLOT_TYPES = {
    "bigint": "BIGINT",
    "int": "INT",
    "varchar": "VARCHAR",
    "text": "TEXT",
    "double": "DOUBLE",
    "decimal": "DECIMAL",
    "datetime": "DATETIME",
    "date": "DATE",
    "binary": "BINARY",
    "null": "UNKNOWN",
    "unknown": "UNKNOWN",
}


def normalize_identifier(name: str) -> str:
    return name.strip().strip('`"').lower()


@dataclass(frozen=True)
class ColumnDef:
    name: str
    type: str
    raw_type: str = ""


@dataclass(frozen=True)
class TableDef:
    name: str
    kind: str  # "table" | "view"
    columns: tuple[ColumnDef, ...]
    view_sql: str | None = None
    base_tables: tuple[str, ...] = ()

    def column_names(self) -> set[str]:
        return {c.name for c in self.columns}

    def has_column(self, name: str) -> bool:
        target = normalize_identifier(name)
        return any(c.name.lower() == target for c in self.columns)

    def column(self, name: str) -> ColumnDef | None:
        target = normalize_identifier(name)
        return next((c for c in self.columns if c.name.lower() == target), None)


@dataclass
class PhysicalSchema:
    tables: dict[str, TableDef] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._index = {normalize_identifier(name): table for name, table in self.tables.items()}

    def get(self, name: str) -> TableDef | None:
        return self._index.get(normalize_identifier(name))

    def to_sqlglot_mapping(self) -> dict[str, dict[str, str]]:
        return {
            table.name: {c.name: _SQLGLOT_TYPES.get(c.type, "UNKNOWN") for c in table.columns}
            for table in self.tables.values()
        }

    @classmethod
    def from_ddl(cls, ddl: str) -> PhysicalSchema:
        creates = [s for s in sqlglot.parse(ddl, read="mysql") if isinstance(s, exp.Create)]
        tables: dict[str, TableDef] = {}
        # 导出顺序里视图可能先于它依赖的基表出现，所以先建表、后建视图
        for statement in creates:
            if str(statement.args.get("kind") or "").upper() == "TABLE":
                table = _table_from_create(statement)
                tables[table.name] = table
        for statement in creates:
            if str(statement.args.get("kind") or "").upper() == "VIEW":
                view = _view_from_create(statement, tables)
                tables[view.name] = view
        return cls(tables=tables)


def _type_family(data_type: exp.DataType | None) -> str:
    if data_type is None:
        return "unknown"
    return _TYPE_FAMILIES.get(data_type.this, "unknown")


def _table_from_create(statement: exp.Create) -> TableDef:
    schema = statement.this
    columns = tuple(
        ColumnDef(
            name=col.name,
            type=_type_family(col.args.get("kind")),
            raw_type=col.args["kind"].sql("mysql") if col.args.get("kind") else "",
        )
        for col in schema.expressions
        if isinstance(col, exp.ColumnDef)
    )
    return TableDef(name=schema.this.name, kind="table", columns=columns)


def _view_from_create(statement: exp.Create, known: dict[str, TableDef]) -> TableDef:
    select = statement.expression
    base_tables = tuple(dict.fromkeys(t.name for t in select.find_all(exp.Table)))
    lookup = {normalize_identifier(n): t for n, t in known.items()}

    columns = []
    for projection in select.expressions:
        inner = projection.unalias()
        if isinstance(inner, exp.Null):
            family, raw = "null", ""
        elif isinstance(inner, exp.Column):
            source = lookup.get(normalize_identifier(inner.table)) if inner.table else None
            if source is None and len(base_tables) == 1:
                source = lookup.get(normalize_identifier(base_tables[0]))
            base_col = source.column(inner.name) if source else None
            family = base_col.type if base_col else "unknown"
            raw = base_col.raw_type if base_col else ""
        else:
            family, raw = "unknown", ""
        columns.append(ColumnDef(name=projection.alias_or_name, type=family, raw_type=raw))

    return TableDef(
        name=statement.this.name,
        kind="view",
        columns=tuple(columns),
        view_sql=select.sql("mysql"),
        base_tables=base_tables,
    )


@lru_cache(maxsize=1)
def load_znjz_schema() -> PhysicalSchema:
    return PhysicalSchema.from_ddl(ZNJZ_DDL_PATH.read_text(encoding="utf-8"))
