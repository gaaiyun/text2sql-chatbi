"""开源数据集：固定到具体提交的原始文件，校验 sha256 后转成带类型的 Parquet，随站点发布。

    python scripts/opendata.py build site/dist/build/<构建号>/datasets

四个数据集的许可证都允许再分发（MIT、CC0、CC BY 4.0），页面上标注来源与许可证。
转换只做三件事：统一成可直接查询的表结构、日期转成 ISO 格式、去掉图片和联系方式这类与分析无关的列。
国家名补一列中文，问“中国的人均碳排放”时结果里直接显示中文。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import re
import sqlite3
import sys
import tempfile
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[1]
COUNTRY_NAMES = ROOT / "scripts" / "data" / "countries_zh.tsv"


@dataclass(frozen=True)
class Source:
    repo: str
    commit: str
    path: str
    sha256: str
    bytes: int

    @property
    def url(self) -> str:
        return f"https://raw.githubusercontent.com/{self.repo}/{self.commit}/{self.path}"

    @property
    def filename(self) -> str:
        return Path(self.path).name


SOURCES: dict[str, tuple[Source, ...]] = {
    "northwind": (
        Source(
            "microsoft/sql-server-samples",
            "2f85f3724ee45776a5183ed34d064488a6e1dc53",
            "samples/databases/northwind-pubs/instnwnd.sql",
            "3cc62b3fca6d244a47dbde698b809331e4f85988a0685b2b370717d431e94871",
            1_049_720,
        ),
    ),
    "chinook": (
        Source(
            "lerocha/chinook-database",
            "c14389e4466cb2dac9fe97782b8dfc6113ed3ae2",
            "ChinookDatabase/DataSources/Chinook_Sqlite.sql",
            "caf31d698a4a79c628215b552dfe6575e71be052ae02b8f18e763498f55f5d44",
            595_545,
        ),
    ),
    "gapminder": (
        Source(
            "jennybc/gapminder",
            "c1205992c5f7dc87b00dd9e76d5ebff05f3a1868",
            "data-raw/08_gap-every-five-years.tsv",
            "f74275b4a2f4a7c8e3a09a752f9d6e43d1759d78d5cbfdebaaa3e1a6efc68f78",
            81_932,
        ),
        Source(
            "jennybc/gapminder",
            "003f98f4bfe596a9af5eaa8450b19cd1e570fc87",
            "data-raw/10_iso-codes.tsv",
            "31fadb2da5d4819fda246e3436e8d2a48b15fcc3ee0d4a5a0f83678ed2d0aec8",
            3_287,
        ),
    ),
    "owid_co2": (
        Source(
            "owid/co2-data",
            "382ee6c662b0ece26e111f263b44c029afad7787",
            "owid-co2-data.csv",
            "7f78e2b218ce4bb8c538bbec04fdc9a7982e8d40bff972e650df603899edd5f6",
            14_377_942,
        ),
    ),
}


@dataclass
class Table:
    name: str
    columns: list[tuple[str, str]]
    rows: list[tuple[Any, ...]] = field(default_factory=list)


# --------------------------------------------------------------------------- Northwind（T-SQL 脚本）

# 源表的列顺序与 CREATE TABLE 一致（没有列清单的 INSERT 按这个顺序取值）；类型为 None 的列不保留
_NORTHWIND: dict[str, list[tuple[str, str | None]]] = {
    "Categories": [("CategoryID", "INTEGER"), ("CategoryName", "VARCHAR"), ("Description", "VARCHAR"), ("Picture", None)],
    "Customers": [
        ("CustomerID", "VARCHAR"), ("CompanyName", "VARCHAR"), ("ContactName", None), ("ContactTitle", "VARCHAR"),
        ("Address", None), ("City", "VARCHAR"), ("Region", "VARCHAR"), ("PostalCode", None), ("Country", "VARCHAR"),
        ("Phone", None), ("Fax", None),
    ],
    "Employees": [
        ("EmployeeID", "INTEGER"), ("LastName", "VARCHAR"), ("FirstName", "VARCHAR"), ("Title", "VARCHAR"),
        ("TitleOfCourtesy", "VARCHAR"), ("BirthDate", "DATE"), ("HireDate", "DATE"), ("Address", None),
        ("City", "VARCHAR"), ("Region", "VARCHAR"), ("PostalCode", None), ("Country", "VARCHAR"), ("HomePhone", None),
        ("Extension", None), ("Photo", None), ("Notes", None), ("ReportsTo", "INTEGER"), ("PhotoPath", None),
    ],
    "Shippers": [("ShipperID", "INTEGER"), ("CompanyName", "VARCHAR"), ("Phone", None)],
    "Suppliers": [
        ("SupplierID", "INTEGER"), ("CompanyName", "VARCHAR"), ("ContactName", None), ("ContactTitle", "VARCHAR"),
        ("Address", None), ("City", "VARCHAR"), ("Region", "VARCHAR"), ("PostalCode", None), ("Country", "VARCHAR"),
        ("Phone", None), ("Fax", None), ("HomePage", None),
    ],
    "Orders": [
        ("OrderID", "INTEGER"), ("CustomerID", "VARCHAR"), ("EmployeeID", "INTEGER"), ("OrderDate", "DATE"),
        ("RequiredDate", "DATE"), ("ShippedDate", "DATE"), ("ShipVia", "INTEGER"), ("Freight", "DOUBLE"),
        ("ShipName", "VARCHAR"), ("ShipAddress", None), ("ShipCity", "VARCHAR"), ("ShipRegion", "VARCHAR"),
        ("ShipPostalCode", None), ("ShipCountry", "VARCHAR"),
    ],
    "Products": [
        ("ProductID", "INTEGER"), ("ProductName", "VARCHAR"), ("SupplierID", "INTEGER"), ("CategoryID", "INTEGER"),
        ("QuantityPerUnit", "VARCHAR"), ("UnitPrice", "DOUBLE"), ("UnitsInStock", "INTEGER"),
        ("UnitsOnOrder", "INTEGER"), ("ReorderLevel", "INTEGER"), ("Discontinued", "INTEGER"),
    ],
    "Order Details": [
        ("OrderID", "INTEGER"), ("ProductID", "INTEGER"), ("UnitPrice", "DOUBLE"), ("Quantity", "INTEGER"),
        ("Discount", "DOUBLE"),
    ],
    "Region": [("RegionID", "INTEGER"), ("RegionDescription", "VARCHAR")],
    "Territories": [("TerritoryID", "VARCHAR"), ("TerritoryDescription", "VARCHAR"), ("RegionID", "INTEGER")],
    "EmployeeTerritories": [("EmployeeID", "INTEGER"), ("TerritoryID", "VARCHAR")],
}  # fmt: skip
_NORTHWIND_RENAMES = {"Order Details": "OrderDetails"}

_INSERT = re.compile(
    r'INSERT\s+(?:INTO\s+)?(?:"(?P<quoted>[^"]+)"|(?:\[?dbo\]?\.)?\[?(?P<bare>\w+)\]?)\s*'
    r"(?:\((?P<columns>[^)]*)\))?\s*VALUES\s*\(",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_HEX = re.compile(r"0x[0-9A-Fa-f]*")
_SPACE = re.compile(r"\s*")


def parse_tsql_inserts(text: str) -> Iterator[tuple[str, list[str] | None, list[Any]]]:
    """逐条取出 INSERT 语句：(表名, 列清单或 None, 取值)。二进制字面量（图片）记为 None。"""
    position = 0
    while match := _INSERT.search(text, position):
        table = match.group("quoted") or match.group("bare")
        columns = (
            [c.strip().strip('"[]') for c in match.group("columns").split(",")]
            if match.group("columns")
            else None
        )
        values, position = _read_values(text, match.end())
        yield table, columns, values


def _read_values(text: str, position: int) -> tuple[list[Any], int]:
    values: list[Any] = []
    while True:
        position = _SPACE.match(text, position).end()
        if text[position] == ")":
            return values, position + 1
        if text.startswith("N'", position) or text[position] == "'":
            position += 2 if text[position] == "N" else 1
            parts = []
            while True:
                end = text.index("'", position)
                parts.append(text[position:end])
                if text.startswith("''", end):
                    parts.append("'")
                    position = end + 2
                    continue
                position = end + 1
                break
            values.append("".join(parts))
        elif text[position : position + 4].upper() == "NULL":
            values.append(None)
            position += 4
        elif hexed := _HEX.match(text, position):
            values.append(None)
            position = hexed.end()
        elif number := _NUMBER.match(text, position):
            raw = number.group()
            values.append(float(raw) if any(ch in raw for ch in ".eE") else int(raw))
            position = number.end()
        else:
            raise ValueError(f"无法解析的取值：{text[position : position + 40]!r}")
        position = _SPACE.match(text, position).end()
        if text[position] == ",":
            position += 1


def _mdy(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return datetime.strptime(str(value).split(" ")[0], "%m/%d/%Y").date().isoformat()


def _cast(value: Any, kind: str) -> Any:
    if value is None:
        return None
    if kind == "VARCHAR":
        return str(value).strip()
    if kind in ("INTEGER", "BIGINT"):
        return int(float(value))
    if kind == "DOUBLE":
        return float(value)
    return value


def northwind_tables(script: str) -> list[Table]:
    tables: dict[str, Table] = {}
    for source, listed, values in parse_tsql_inserts(script):
        spec = _NORTHWIND.get(source)
        if spec is None:
            continue
        names = listed or [name for name, _ in spec]
        record = dict(zip(names, values, strict=True))
        kept = [(name, kind) for name, kind in spec if kind is not None]
        name = _NORTHWIND_RENAMES.get(source, source)
        table = tables.setdefault(name, Table(name, kept))
        table.rows.append(
            tuple(
                _mdy(record.get(col)) if kind == "DATE" else _cast(record.get(col), kind)
                for col, kind in kept
            )
        )
    order = [_NORTHWIND_RENAMES.get(name, name) for name in _NORTHWIND]
    return sorted(tables.values(), key=lambda t: order.index(t.name))


# --------------------------------------------------------------------------- Chinook（SQLite 脚本）

_CHINOOK_DROP = {
    "Customer": {"Address", "PostalCode", "Phone", "Fax", "Email"},
    "Employee": {"Address", "PostalCode", "Phone", "Fax", "Email"},
    "Invoice": {"BillingAddress", "BillingPostalCode"},
}
_CHINOOK_TABLES = (
    "Artist", "Album", "Track", "Genre", "MediaType", "Playlist", "PlaylistTrack",
    "Customer", "Employee", "Invoice", "InvoiceLine",
)  # fmt: skip


def _sqlite_type(declared: str) -> str:
    upper = declared.upper()
    if "INT" in upper:
        return "INTEGER"
    if "NUMERIC" in upper or "REAL" in upper or "DOUBLE" in upper:
        return "DOUBLE"
    if "DATE" in upper:
        return "DATE"
    return "VARCHAR"


def chinook_tables(script: str) -> list[Table]:
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(script)
        present = {
            row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        tables = []
        for name in (t for t in _CHINOOK_TABLES if t in present):
            info = con.execute(f'PRAGMA table_info("{name}")').fetchall()
            kept = [
                (col, _sqlite_type(kind))
                for _, col, kind, *_ in info
                if col not in _CHINOOK_DROP.get(name, set())
            ]
            select = ", ".join(f'"{col}"' for col, _ in kept)
            rows = [
                tuple(
                    (str(v)[:10] if v is not None else None) if kind == "DATE" else _cast(v, kind)
                    for v, (_, kind) in zip(raw, kept, strict=True)
                )
                for raw in con.execute(f'SELECT {select} FROM "{name}"')
            ]
            tables.append(Table(name, kept, rows))
        return tables
    finally:
        con.close()


# --------------------------------------------------------------------------- Gapminder（TSV）

_CONTINENTS = {
    "Africa": "非洲",
    "Americas": "美洲",
    "Asia": "亚洲",
    "Europe": "欧洲",
    "Oceania": "大洋洲",
}
_GAPMINDER = [
    ("country", "VARCHAR"), ("country_zh", "VARCHAR"), ("iso_code", "VARCHAR"), ("continent", "VARCHAR"),
    ("continent_zh", "VARCHAR"), ("year", "INTEGER"), ("life_expectancy", "DOUBLE"), ("population", "BIGINT"),
    ("gdp_per_capita", "DOUBLE"),
]  # fmt: skip


def gapminder_tables(gap_tsv: str, iso_tsv: str, names: dict[str, str]) -> list[Table]:
    iso = {
        row["country"]: row["iso_alpha"]
        for row in csv.DictReader(io.StringIO(iso_tsv), delimiter="\t")
    }
    rows = []
    for row in csv.DictReader(io.StringIO(gap_tsv), delimiter="\t"):
        code = iso[row["country"]]
        rows.append(
            (
                row["country"],
                names.get(code, row["country"]),
                code,
                row["continent"],
                _CONTINENTS[row["continent"]],
                int(row["year"]),
                float(row["lifeExp"]),
                int(float(row["pop"])),
                float(row["gdpPercap"]),
            )
        )
    return [Table("gapminder", list(_GAPMINDER), rows)]


# --------------------------------------------------------------------------- Our World in Data 碳排放（CSV）

OWID_FIRST_YEAR = 1950
# 只保留总量口径的大洲、收入分组与世界合计；带 (GCP)、(excl. …) 的是同一口径的重复或变体
_OWID_REGIONS = {
    "World": "世界",
    "Africa": "非洲",
    "Asia": "亚洲",
    "Europe": "欧洲",
    "North America": "北美洲",
    "South America": "南美洲",
    "Oceania": "大洋洲",
    "European Union (27)": "欧盟（27 国）",
    "High-income countries": "高收入国家",
    "Upper-middle-income countries": "中高收入国家",
    "Lower-middle-income countries": "中低收入国家",
    "Low-income countries": "低收入国家",
    "International aviation": "国际航空",
    "International shipping": "国际航运",
}
_OWID_NO_ISO_COUNTRIES = {"Kosovo": "科索沃"}
_OWID_MEASURES = [
    ("population", "BIGINT"), ("gdp", "DOUBLE"), ("co2", "DOUBLE"), ("co2_per_capita", "DOUBLE"),
    ("co2_per_gdp", "DOUBLE"), ("coal_co2", "DOUBLE"), ("oil_co2", "DOUBLE"), ("gas_co2", "DOUBLE"),
    ("cement_co2", "DOUBLE"), ("flaring_co2", "DOUBLE"), ("other_industry_co2", "DOUBLE"),
    ("land_use_change_co2", "DOUBLE"), ("share_global_co2", "DOUBLE"), ("cumulative_co2", "DOUBLE"),
    ("total_ghg", "DOUBLE"), ("methane", "DOUBLE"), ("nitrous_oxide", "DOUBLE"),
    ("primary_energy_consumption", "DOUBLE"), ("energy_per_capita", "DOUBLE"),
]  # fmt: skip
_OWID_COUNTRY = [
    ("country", "VARCHAR"),
    ("country_zh", "VARCHAR"),
    ("iso_code", "VARCHAR"),
    ("year", "INTEGER"),
    *_OWID_MEASURES,
]
_OWID_REGION = [
    ("region", "VARCHAR"),
    ("region_zh", "VARCHAR"),
    ("year", "INTEGER"),
    *_OWID_MEASURES,
]


def owid_tables(csv_text: str, names: dict[str, str]) -> list[Table]:
    countries, regions = (
        Table("country_emissions", list(_OWID_COUNTRY)),
        Table("region_emissions", list(_OWID_REGION)),
    )
    for row in csv.DictReader(io.StringIO(csv_text)):
        year = int(row["year"])
        if year < OWID_FIRST_YEAR:
            continue
        measures = tuple(_cast(row.get(col) or None, kind) for col, kind in _OWID_MEASURES)
        name, code = row["country"], row.get("iso_code") or ""
        if code and not code.startswith("OWID"):
            countries.rows.append((name, names.get(code, name), code, year, *measures))
        elif name in _OWID_NO_ISO_COUNTRIES:
            countries.rows.append((name, _OWID_NO_ISO_COUNTRIES[name], None, year, *measures))
        elif name in _OWID_REGIONS:
            regions.rows.append((name, _OWID_REGIONS[name], year, *measures))
    return [countries, regions]


# --------------------------------------------------------------------------- 输出

_SCHEMAS = {
    "northwind": {
        _NORTHWIND_RENAMES.get(name, name): [(col, kind) for col, kind in spec if kind is not None]
        for name, spec in _NORTHWIND.items()
    },
    "gapminder": {"gapminder": list(_GAPMINDER)},
    "owid_co2": {"country_emissions": list(_OWID_COUNTRY), "region_emissions": list(_OWID_REGION)},
}


def output_schema(dataset: str) -> dict[str, list[tuple[str, str]]]:
    """转换后每张表的列与类型。Chinook 的列来自脚本本身，这里按转换规则给出。"""
    if dataset == "chinook":
        return {table.name: table.columns for table in chinook_tables(_CHINOOK_DDL)}
    return _SCHEMAS[dataset]


_CHINOOK_DDL = """
CREATE TABLE Artist (ArtistId INTEGER, Name NVARCHAR(120));
CREATE TABLE Album (AlbumId INTEGER, Title NVARCHAR(160), ArtistId INTEGER);
CREATE TABLE Track (TrackId INTEGER, Name NVARCHAR(200), AlbumId INTEGER, MediaTypeId INTEGER, GenreId INTEGER,
  Composer NVARCHAR(220), Milliseconds INTEGER, Bytes INTEGER, UnitPrice NUMERIC(10,2));
CREATE TABLE Genre (GenreId INTEGER, Name NVARCHAR(120));
CREATE TABLE MediaType (MediaTypeId INTEGER, Name NVARCHAR(120));
CREATE TABLE Playlist (PlaylistId INTEGER, Name NVARCHAR(120));
CREATE TABLE PlaylistTrack (PlaylistId INTEGER, TrackId INTEGER);
CREATE TABLE Customer (CustomerId INTEGER, FirstName NVARCHAR(40), LastName NVARCHAR(20), Company NVARCHAR(80),
  Address NVARCHAR(70), City NVARCHAR(40), State NVARCHAR(40), Country NVARCHAR(40), PostalCode NVARCHAR(10),
  Phone NVARCHAR(24), Fax NVARCHAR(24), Email NVARCHAR(60), SupportRepId INTEGER);
CREATE TABLE Employee (EmployeeId INTEGER, LastName NVARCHAR(20), FirstName NVARCHAR(20), Title NVARCHAR(30),
  ReportsTo INTEGER, BirthDate DATETIME, HireDate DATETIME, Address NVARCHAR(70), City NVARCHAR(40),
  State NVARCHAR(40), Country NVARCHAR(40), PostalCode NVARCHAR(10), Phone NVARCHAR(24), Fax NVARCHAR(24),
  Email NVARCHAR(60));
CREATE TABLE Invoice (InvoiceId INTEGER, CustomerId INTEGER, InvoiceDate DATETIME, BillingAddress NVARCHAR(70),
  BillingCity NVARCHAR(40), BillingState NVARCHAR(40), BillingCountry NVARCHAR(40), BillingPostalCode NVARCHAR(10),
  Total NUMERIC(10,2));
CREATE TABLE InvoiceLine (InvoiceLineId INTEGER, InvoiceId INTEGER, TrackId INTEGER, UnitPrice NUMERIC(10,2),
  Quantity INTEGER);
"""


def write_parquet(table: Table, path: Path) -> int:
    """经临时 CSV 交给 DuckDB 按声明的类型读入，再写 Parquet；空值用 \\N 标记，与空字符串区分。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "rows.csv"
        with staging.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            for row in table.rows:
                writer.writerow("\\N" if value is None else value for value in row)
        columns = ", ".join(f"'{name}': '{kind}'" for name, kind in table.columns)
        con = duckdb.connect()
        try:
            con.execute(
                f"COPY (SELECT * FROM read_csv('{staging.as_posix()}', header = false, nullstr = '\\N', "
                f"quote = '\"', escape = '\"', columns = {{{columns}}})) "
                f"TO '{path.as_posix()}' (FORMAT parquet, COMPRESSION zstd)"
            )
        finally:
            con.close()
    return len(table.rows)


def country_names(path: Path = COUNTRY_NAMES) -> dict[str, str]:
    with path.open(encoding="utf-8") as handle:
        return {row["iso_code"]: row["name_zh"] for row in csv.DictReader(handle, delimiter="\t")}


def fetch(source: Source, cache: Path) -> bytes:
    target = cache / source.filename
    if not (target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == source.sha256):
        print(f"[opendata] 下载 {source.url}", flush=True)
        request = urllib.request.Request(source.url, headers={"User-Agent": "text2sql-site-build"})
        with urllib.request.urlopen(request, timeout=300) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != source.sha256:
            raise SystemExit(f"校验失败：{source.url} 的 sha256 与固定值不一致")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return target.read_bytes()


def convert(dataset: str, cache: Path) -> list[Table]:
    files = [fetch(source, cache).decode("utf-8-sig") for source in SOURCES[dataset]]
    if dataset == "northwind":
        return northwind_tables(files[0])
    if dataset == "chinook":
        return chinook_tables(files[0])
    if dataset == "gapminder":
        return gapminder_tables(files[0], files[1], country_names())
    if dataset == "owid_co2":
        return owid_tables(files[0], country_names())
    raise KeyError(dataset)


def build(target: Path, cache: Path) -> dict[str, list[dict[str, Any]]]:
    """转换全部数据集，返回 {数据集: [{table, file, rows, bytes}]}，文件名相对 target。"""
    built: dict[str, list[dict[str, Any]]] = {}
    for dataset in SOURCES:
        entries = []
        for table in convert(dataset, cache):
            path = target / dataset / f"{table.name}.parquet"
            rows = write_parquet(table, path)
            entries.append(
                {
                    "table": table.name,
                    "file": f"{dataset}/{path.name}",
                    "rows": rows,
                    "bytes": path.stat().st_size,
                }
            )
        built[dataset] = entries
    return built


def main() -> None:
    parser = argparse.ArgumentParser(description="转换开源数据集为 Parquet")
    parser.add_argument("command", choices=["build"])
    parser.add_argument("target", type=Path)
    parser.add_argument(
        "--cache", type=Path, default=Path.home() / ".cache" / "text2sql-site" / "opendata"
    )
    args = parser.parse_args()
    for dataset, entries in build(args.target, args.cache).items():
        total = sum(e["bytes"] for e in entries)
        print(
            f"{dataset}: {len(entries)} 张表，{sum(e['rows'] for e in entries)} 行，{total / 1024:.0f} KB"
        )


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
