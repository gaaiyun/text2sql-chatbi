"""用户上传的数据集：导入 CSV / TSV / Parquet / JSON / Excel 到 DuckDB，自动生成表结构与语义层。

没有人写的语义层，规则解析器无从下手，这类数据集只走 SQL 智能体。自动生成的部分只写看得见的事实：
行数、字段类型、低基数字段的真实取值、疑似个人信息字段（交给安全门拦截，也不抽样取值）。

中文环境里 Excel 导出的 CSV 常是 GBK 编码，导入前先识别编码并转成 UTF-8。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from text2sql.datasets.cards import DatasetCard
from text2sql.db.schema import ColumnDef, PhysicalSchema, TableDef
from text2sql.semantic.catalog import SemanticCatalog

SUPPORTED = {
    ".csv": "csv",
    ".tsv": "csv",
    ".txt": "csv",
    ".parquet": "parquet",
    ".json": "json",
    ".jsonl": "json",
    ".ndjson": "json",
    ".xlsx": "excel",
    ".xls": "excel",
}
PERSONAL = re.compile(
    r"(姓名|名字|身份证|证件号|手机|电话|联系方式|邮箱|住址|家庭地址|银行卡|卡号|密码|"
    r"e-?mail|phone|mobile|password|passwd|id_?card|ssn)",
    re.IGNORECASE,
)
# 数值型但按类别用的字段：编号、年份、月份这类加总没有意义
CODE_LIKE = re.compile(
    r"(编号|代码|编码|序号|工号|学号|单号|邮编|年份|年度|月份|季度|^年$|^月$|^日$|"
    r"(^|_)id$|^id_|code$|zip|year|month|quarter)",
    re.IGNORECASE,
)
CAMEL_ID = re.compile(r"[a-z](Id|ID)$")  # CustomerId、DeptID 这类驼峰外键
# 能求平均、不该求和的数值：推荐问题里不拿它们做“合计”
NOT_ADDITIVE = re.compile(
    r"(单价|价格|均价|率|比例|占比|比重|评分|年龄|折扣|price|rate|ratio|percent|pct|score|discount)",
    re.IGNORECASE,
)
MONEY_LIKE = re.compile(
    r"(额|收入|营收|利润|成本|费用|金额|薪|工资|销量|revenue|sales|amount|profit|cost)",
    re.IGNORECASE,
)
MAX_SAMPLE_DISTINCT = 30
MAX_SCAN_DISTINCT = 300
_NUMERIC = ("bigint", "int", "double", "decimal")
_TYPE_FAMILIES = {
    "BIGINT": "bigint",
    "HUGEINT": "bigint",
    "UBIGINT": "bigint",
    "INTEGER": "int",
    "UINTEGER": "int",
    "SMALLINT": "int",
    "TINYINT": "int",
    "BOOLEAN": "int",
    "DOUBLE": "double",
    "FLOAT": "double",
    "REAL": "double",
    "DATE": "date",
    "VARCHAR": "varchar",
}


class UploadError(ValueError):
    """文件无法导入：格式不支持、编码无法识别、内容为空等。"""


@dataclass
class UploadedTable:
    name: str
    source: str
    rows: int
    columns: list[dict[str, Any]] = field(default_factory=list)
    label: str = ""  # 中文表名、说明与同义词来自数据卡片，上传的文件没有
    description: str = ""
    synonyms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label or self.name,
            "description": self.description,
            "source": self.source,
            "rows": self.rows,
            "columns": self.columns,
        }


def is_code_like(column: str) -> bool:
    return bool(CODE_LIKE.search(column) or CAMEL_ID.search(column))


def table_name_for(filename: str, taken: set[str]) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    cleaned = re.sub(r"[^\w一-龥]+", "_", stem).strip("_") or "上传表"
    if cleaned[0].isdigit():
        cleaned = f"t_{cleaned}"
    name, index = cleaned, 2
    while name in taken:
        name = f"{cleaned}_{index}"
        index += 1
    return name


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _to_utf8(path: Path) -> Path:
    raw = path.read_bytes()
    if not raw.strip():
        raise UploadError(f"{path.name} 是空文件")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise UploadError(f"{path.name} 的编码无法识别，请另存为 UTF-8 后再上传")
    target = path.with_name(f"{path.stem}.utf8{path.suffix}")
    # 按字节写：文本模式在 Windows 上会把 \n 再转成 \r\n，原本的 \r\n 变成 \r\r\n
    target.write_bytes(text.encode("utf-8"))
    return target


def _excel_to_csv(path: Path) -> Path:
    try:
        from python_calamine import CalamineWorkbook
    except ImportError as exc:  # pragma: no cover - 可选依赖
        raise UploadError("读取 Excel 需要 python-calamine，请改传 CSV") from exc
    import csv

    workbook = CalamineWorkbook.from_path(str(path))
    rows = workbook.get_sheet_by_index(0).to_python()
    if not rows:
        raise UploadError(f"{path.name} 的第一个工作表是空的")
    target = path.with_suffix(".sheet1.csv")
    with target.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    return target


def _family(duck_type: str) -> str:
    upper = duck_type.upper()
    if upper.startswith("DECIMAL"):
        return "decimal"
    if upper.startswith("TIMESTAMP") or upper.startswith("DATETIME"):
        return "datetime"
    return _TYPE_FAMILIES.get(upper, "varchar" if "CHAR" in upper or upper == "UUID" else "unknown")


def _role(column: str, duck_type: str, family: str, n_distinct: int, rows: int) -> str:
    if family in ("date", "datetime"):
        return "time"
    # 0/1 标记、五级评分这类取值极少的数值，按类别统计才有意义；小表不适用，8 家门店的面积也只有 8 个值
    few_values = n_distinct <= 2 or (n_distinct <= 5 and rows >= 50)
    if (
        family in _NUMERIC
        and duck_type.upper() != "BOOLEAN"
        and not is_code_like(column)
        and not few_values
    ):
        return "measure"
    if n_distinct <= max(200, rows // 20):
        return "dimension"
    return "text"


def _profile(con: duckdb.DuckDBPyConnection, name: str, source: str) -> UploadedTable:
    described = con.execute(f"DESCRIBE {_quote(name)}").fetchall()
    rows = int(con.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()[0])
    if not described or rows == 0:
        raise UploadError(f"{source} 没有数据行")
    distinct_sql = ", ".join(f"COUNT(DISTINCT {_quote(col[0])})" for col in described)
    distinct = con.execute(f"SELECT {distinct_sql} FROM {_quote(name)}").fetchone()

    columns = []
    for (column, duck_type, *_), n_distinct in zip(described, distinct, strict=True):
        family = _family(str(duck_type))
        sensitive = bool(PERSONAL.search(column))
        role = _role(column, str(duck_type), family, int(n_distinct), rows)
        samples: list[str] = []
        if (
            not sensitive
            and family in ("varchar", "int", "bigint")
            and n_distinct <= MAX_SAMPLE_DISTINCT
        ):
            values = con.execute(
                f"SELECT {_quote(column)} AS v, COUNT(*) AS n FROM {_quote(name)} WHERE {_quote(column)} IS NOT NULL "
                f"GROUP BY v ORDER BY n DESC, v LIMIT 12"
            ).fetchall()
            samples = [str(v) for v, _ in values]
        entry = {
            "name": column,
            "label": column,
            "type": str(duck_type),
            "family": family,
            "role": role,
            "distinct": int(n_distinct),
            "samples": samples,
            "sensitive": sensitive,
        }
        if role == "time":
            low, high = con.execute(
                f"SELECT MIN({_quote(column)})::DATE, MAX({_quote(column)})::DATE FROM {_quote(name)}"
            ).fetchone()
            entry["range"] = [str(low), str(high)] if low is not None else []
        columns.append(entry)
    return UploadedTable(name=name, source=source, rows=rows, columns=columns)


def import_files(
    db_path: Path | str, files: Sequence[tuple[str, Path | str]]
) -> list[UploadedTable]:
    """导入文件并返回每张表的画像。db_path 会被重建，之前的上传内容一并清空。"""
    target = Path(db_path)
    target.unlink(missing_ok=True)
    target.with_suffix(target.suffix + ".wal").unlink(missing_ok=True)

    prepared: list[tuple[str, str, Path]] = []
    for original, raw_path in files:
        path = Path(raw_path)
        kind = SUPPORTED.get(Path(original).suffix.lower())
        if kind is None:
            raise UploadError(
                f"不支持 {Path(original).suffix or original} 格式，请上传 CSV、Parquet、JSON 或 Excel"
            )
        if kind == "csv":
            path = _to_utf8(path)
        elif kind == "excel":
            path, kind = _excel_to_csv(path), "csv"
        elif path.stat().st_size == 0:
            raise UploadError(f"{original} 是空文件")
        prepared.append((original, kind, path))

    con = duckdb.connect(str(target))
    taken: set[str] = set()
    tables = []
    try:
        for original, kind, path in prepared:
            name = table_name_for(original, taken)
            taken.add(name)
            reader = {
                "csv": f"read_csv_auto({_literal(str(path))}, header = true, sample_size = -1)",
                "parquet": f"read_parquet({_literal(str(path))})",
                "json": f"read_json_auto({_literal(str(path))})",
            }[kind]
            try:
                con.execute(f"CREATE TABLE {_quote(name)} AS SELECT * FROM {reader}")
            except duckdb.Error as exc:
                raise UploadError(f"{original} 无法解析：{exc}") from exc
            tables.append(_profile(con, name, original))
    finally:
        con.close()
    return tables


def build_schema(tables: Iterable[UploadedTable]) -> PhysicalSchema:
    return PhysicalSchema(
        {
            table.name: TableDef(
                name=table.name,
                kind="table",
                columns=tuple(ColumnDef(c["name"], c["family"], c["type"]) for c in table.columns),
            )
            for table in tables
        }
    )


def _describe_column(column: dict[str, Any]) -> str:
    role = {"time": "时间", "measure": "数值", "dimension": "类别", "text": "文本"}[column["role"]]
    parts = [f"{role}，{column['type']}，{column['distinct']} 个不同取值"]
    if column.get("range"):
        parts.append(f"范围 {column['range'][0]} 至 {column['range'][1]}")
    if column["sensitive"]:
        parts.append("疑似个人信息，不可查询")
    if column.get("note"):
        parts.append(str(column["note"]))
    return "；".join(parts)


def _latest_date(tables: Sequence[UploadedTable]) -> str | None:
    dates = [c["range"][1] for t in tables for c in t.columns if c.get("range")]
    return max(dates) if dates else None


def _latest_year(tables: Sequence[UploadedTable]) -> int:
    latest = _latest_date(tables)
    return int(latest[:4]) if latest else date.today().year


def _last_year_is_partial(tables: Sequence[UploadedTable]) -> bool:
    """最新日期早于当年 12 月时，最后一年的数据不完整；没有日期列时无从判断，按完整处理。"""
    latest = _latest_date(tables)
    return bool(latest) and latest < f"{latest[:4]}-12-01"


def build_catalog(
    tables: Sequence[UploadedTable], *, title: str = "上传的数据", card: DatasetCard | None = None
) -> SemanticCatalog:
    """由导入画像生成语义层；有数据卡片时叠加中文名、表间关联、口径规则和推荐问题。"""
    sensitive = sorted({c["name"] for t in tables for c in t.columns if c["sensitive"]})
    names = [t.name for t in tables]
    rules = [
        f"只查询这些表：{'、'.join(f'`{n}`' for n in names)}。表名和字段名可能是中文或含括号，一律用反引号引用。",
        "文本字段写 WHERE 条件前先用 get_column_values 确认真实取值；数值字段可以直接求和、求平均。",
        "日期字段按年统计用 YEAR(字段)，按月统计用 DATE_FORMAT(字段, '%Y-%m')。",
    ]
    if card is None or not card.relationships:
        rules.append("表之间没有预先声明的关联关系，关联前先确认两边字段的取值能对应上。")
    if card is not None:
        rules.extend(card.rules)
    if sensitive:
        rules.append(
            f"疑似个人信息字段（{'、'.join(sensitive)}）不可查询，也不要在结果中推断个人身份。"
        )
    raw_tables = {}
    for table in tables:
        if card is None:
            description = f"上传的文件「{table.source}」，{table.rows} 行、{len(table.columns)} 列"
            grain = "每行对应上传文件中的一行"
        else:
            description = (
                f"{table.description}（{table.rows} 行）"
                if table.description
                else f"{table.rows} 行"
            )
            grain = ""
        raw_tables[table.name] = {
            "kind": "table",
            "label": table.label or table.name,
            "description": description,
            "grain": grain,
            "synonyms": list(table.synonyms),
            "columns": {
                column["name"]: {
                    "label": column.get("label") or column["name"],
                    "synonyms": list(column.get("synonyms") or []),
                    "role": column["role"],
                    "description": _describe_column(column),
                    "value_hint": f"取值如：{'、'.join(column['samples'][:8])}"
                    if column["samples"]
                    else None,
                    # 取值不太多的文本列都进值索引，问题里提到“中国”“Rock”时能认出是哪个字段的取值
                    "scan_values": column["family"] == "varchar"
                    and not column["sensitive"]
                    and column["distinct"] <= MAX_SCAN_DISTINCT,
                }
                for column in table.columns
            },
        }
    if card is None:
        overview = "用户在浏览器中上传的数据：" + "；".join(
            f"{t.name}（{t.rows} 行）" for t in tables
        )
    else:
        overview = card.summary
    return SemanticCatalog.from_dict(
        {
            "dataset": card.id if card else "upload",
            "title": card.title if card else title,
            "description": overview,
            "anchor_year": (
                card.anchor_year if card and card.anchor_year else _latest_year(tables)
            ),
            "anchor_partial": _last_year_is_partial(tables),
            "sensitive_columns": sensitive,
            "rules": rules,
            "domain": {"open": True, "topics": card.tagline if card else "上传表中的字段"},
            "linking": {"fallback": names},
            "relationships": list(card.relationships) if card else [],
            "examples": [
                {"question": q, "category": "推荐问题", "requires_llm": True}
                for q in (card.suggestions if card else ())
            ],
            "tables": raw_tables,
        }
    )


def _group_dims(table: UploadedTable) -> list[str]:
    """适合做分组的字段：2 到 30 个取值，不是编号，不是个人信息。"""
    return [
        c["name"]
        for c in table.columns
        if c["role"] == "dimension"
        and 2 <= c["distinct"] <= 30
        and not c["sensitive"]
        and not is_code_like(c["name"])
    ]


def _joined_dims(main: UploadedTable, other: UploadedTable) -> list[str]:
    """other 表里有一个与主表同名、且在 other 中取值唯一的字段时，可以关联过去按 other 的类别统计。"""
    main_names = {c["name"] for c in main.columns}
    has_key = any(
        c["name"] in main_names
        and c["role"] != "measure"
        and not c["sensitive"]
        and c["distinct"] == other.rows
        for c in other.columns
    )
    return [name for name in _group_dims(other) if name not in main_names] if has_key else []


def suggest_questions(tables: Sequence[UploadedTable], limit: int = 4) -> list[str]:
    """按行数从多到少处理：明细表通常最大，也最值得问。每条问题都点名真实字段。"""
    ordered = sorted(tables, key=lambda t: t.rows, reverse=True)
    suggestions: list[str] = []
    for index, table in enumerate(ordered):
        dims = _group_dims(table)
        additive = [
            c["name"]
            for c in table.columns
            if c["role"] == "measure" and not c["sensitive"] and not NOT_ADDITIVE.search(c["name"])
        ]
        measures = sorted(additive, key=lambda name: not MONEY_LIKE.search(name))
        times = [c["name"] for c in table.columns if c["role"] == "time" and not c["sensitive"]]
        if not measures:
            suggestions += [f"各{dim}分别有多少条记录" for dim in dims[:2]]
            continue
        measure = measures[0]
        if dims:
            suggestions.append(f"按{dims[0]}统计{measure}的合计")
        suggestions.append(f"{measure}最高的 10 条记录是哪些")
        if times:
            suggestions.append(f"按月统计{measure}的变化趋势")
        if index == 0:
            for other in ordered[1:]:
                suggestions += [
                    f"按{dim}统计{measure}的合计" for dim in _joined_dims(table, other)[:1]
                ]
        if len(dims) > 1:
            suggestions.append(f"各{dims[1]}的{measure}占比")
    return list(dict.fromkeys(suggestions))[:limit]
