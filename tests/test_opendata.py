"""开源数据集的转换：T-SQL / SQLite 脚本、TSV、CSV 转成带类型的表，数据卡片与转换结果一致。

原始文件在构建站点时下载，这里只用几行手写的片段，覆盖各格式里容易出错的地方。
"""

from __future__ import annotations

import duckdb
import pytest

from scripts.opendata import (
    SOURCES,
    Table,
    chinook_tables,
    gapminder_tables,
    northwind_tables,
    output_schema,
    owid_tables,
    parse_tsql_inserts,
    write_parquet,
)
from text2sql.datasets.cards import list_cards, load_card

NORTHWIND_SCRIPT = """
set identity_insert "Categories" on
go
INSERT "Categories"("CategoryID","CategoryName","Description","Picture") VALUES(1,'Beverages','Soft drinks, coffees',0x151C2F00FFFF)
INSERT INTO "Orders"
("OrderID","CustomerID","EmployeeID","OrderDate","RequiredDate",
	"ShippedDate","ShipVia","Freight","ShipName","ShipAddress",
	"ShipCity","ShipRegion","ShipPostalCode","ShipCountry")
VALUES (10248,N'VINET',5,'7/4/1996','8/1/1996',NULL,3,32.38,
	N'Vins et alcools Chevalier',N'59 rue de l''Abbaye',N'Reims',
	NULL,N'51100',N'France')
go
INSERT "Order Details" VALUES(10248,11,14,12,0.15)
Insert Into Region Values (1,'Eastern')
"""


def test_tsql_inserts_are_parsed_with_quotes_blobs_and_nulls():
    rows = list(parse_tsql_inserts(NORTHWIND_SCRIPT))

    assert [table for table, _, _ in rows] == ["Categories", "Orders", "Order Details", "Region"]
    assert rows[0][2] == [1, "Beverages", "Soft drinks, coffees", None]
    assert rows[1][1][:3] == ["OrderID", "CustomerID", "EmployeeID"]
    assert rows[1][2][9] == "59 rue de l'Abbaye"
    assert rows[1][2][5] is None and rows[1][2][7] == 32.38
    assert rows[2] == ("Order Details", None, [10248, 11, 14, 12, 0.15])


def test_northwind_tables_rename_convert_dates_and_drop_unused_columns():
    tables = {t.name: t for t in northwind_tables(NORTHWIND_SCRIPT)}

    orders = tables["Orders"]
    record = dict(zip([c for c, _ in orders.columns], orders.rows[0], strict=True))
    assert record["OrderDate"] == "1996-07-04" and record["ShippedDate"] is None
    assert "ShipAddress" not in record
    assert [c for c, _ in tables["Categories"].columns] == [
        "CategoryID",
        "CategoryName",
        "Description",
    ]
    assert tables["OrderDetails"].rows == [(10248, 11, 14, 12, 0.15)]


CHINOOK_SCRIPT = """
CREATE TABLE [Invoice]
(
    [InvoiceId] INTEGER  NOT NULL,
    [CustomerId] INTEGER  NOT NULL,
    [InvoiceDate] DATETIME  NOT NULL,
    [BillingAddress] NVARCHAR(70),
    [BillingCity] NVARCHAR(40),
    [BillingState] NVARCHAR(40),
    [BillingCountry] NVARCHAR(40),
    [BillingPostalCode] NVARCHAR(10),
    [Total] NUMERIC(10,2)  NOT NULL
);
INSERT INTO [Invoice] VALUES (1, 2, '2021-01-01 00:00:00', 'Theodor-Heuss-Straße 34', 'Stuttgart', NULL, 'Germany', '70174', 1.98);
"""


def test_chinook_tables_keep_business_columns_only():
    (invoice,) = chinook_tables(CHINOOK_SCRIPT)

    assert invoice.name == "Invoice"
    assert [c for c, _ in invoice.columns] == [
        "InvoiceId",
        "CustomerId",
        "InvoiceDate",
        "BillingCity",
        "BillingState",
        "BillingCountry",
        "Total",
    ]
    assert invoice.rows == [(1, 2, "2021-01-01", "Stuttgart", None, "Germany", 1.98)]


def test_gapminder_gets_iso_codes_and_chinese_names():
    gap = "country\tcontinent\tyear\tlifeExp\tpop\tgdpPercap\nChina\tAsia\t2007\t72.961\t1318683096\t4959.114854\n"
    iso = "country\tiso_alpha\tiso_num\nChina\tCHN\t156\n"

    (table,) = gapminder_tables(gap, iso, {"CHN": "中国"})

    record = dict(zip([c for c, _ in table.columns], table.rows[0], strict=True))
    assert record["country_zh"] == "中国" and record["continent_zh"] == "亚洲"
    assert record["iso_code"] == "CHN" and record["population"] == 1318683096


def test_owid_rows_split_into_countries_and_regions():
    header = ",".join(
        c for c, _ in output_schema("owid_co2")["country_emissions"] if c != "country_zh"
    )
    columns = header.split(",")

    def line(**values):
        return ",".join(str(values.get(c, "")) for c in columns)

    csv_text = "\n".join(
        [
            header,
            line(country="China", iso_code="CHN", year=2024, co2=12000.5, population=1419321278.0),
            line(country="China", iso_code="CHN", year=1949, co2=80.0),
            line(country="Kosovo", year=2024, co2=8.1),
            line(country="World", year=2024, co2=38000.0),
            line(country="Asia (GCP)", year=2024, co2=21000.0),
        ]
    )

    tables = {t.name: t for t in owid_tables(csv_text, {"CHN": "中国"})}

    countries = [
        dict(zip([c for c, _ in tables["country_emissions"].columns], r, strict=True))
        for r in tables["country_emissions"].rows
    ]
    regions = [
        dict(zip([c for c, _ in tables["region_emissions"].columns], r, strict=True))
        for r in tables["region_emissions"].rows
    ]
    assert [(r["country_zh"], r["year"], r["population"]) for r in countries] == [
        ("中国", 2024, 1419321278),
        ("科索沃", 2024, None),
    ]
    assert [(r["region_zh"], r["co2"]) for r in regions] == [("世界", 38000.0)]


def test_parquet_roundtrip_keeps_types_and_nulls(tmp_path):
    table = Table(
        name="Orders",
        columns=[
            ("OrderID", "INTEGER"),
            ("OrderDate", "DATE"),
            ("Freight", "DOUBLE"),
            ("ShipRegion", "VARCHAR"),
        ],
        rows=[(1, "1996-07-04", 32.38, None), (2, "1996-07-05", 11.61, 'RJ, "Centro"')],
    )
    path = tmp_path / "orders.parquet"

    assert write_parquet(table, path) == 2

    described = duckdb.sql(f"DESCRIBE SELECT * FROM read_parquet('{path.as_posix()}')").fetchall()
    assert [(name, kind) for name, kind, *_ in described] == [
        ("OrderID", "INTEGER"),
        ("OrderDate", "DATE"),
        ("Freight", "DOUBLE"),
        ("ShipRegion", "VARCHAR"),
    ]
    assert duckdb.sql(
        f"SELECT ShipRegion FROM read_parquet('{path.as_posix()}') ORDER BY OrderID"
    ).fetchall() == [
        (None,),
        ('RJ, "Centro"',),
    ]


def test_every_open_dataset_has_pinned_sources_and_a_card():
    open_cards = {card.id for card in list_cards() if card.kind == "open"}

    assert open_cards == set(SOURCES)
    for files in SOURCES.values():
        for source in files:
            assert len(source.commit) == 40 and len(source.sha256) == 64
            assert source.url.startswith("https://raw.githubusercontent.com/")


@pytest.mark.parametrize("dataset", sorted(SOURCES))
def test_cards_only_reference_columns_the_converter_produces(dataset):
    card = load_card(dataset)
    schema = output_schema(dataset)

    assert card.license["name"] and card.source["url"] and len(card.suggestions) >= 4
    for table, spec in card.tables.items():
        assert table in schema, table
        produced = {c for c, _ in schema[table]}
        assert set(spec.get("columns") or {}) <= produced, table
    for rel in card.relationships:
        for side in ("from", "to"):
            table, column = rel[side].split(".", 1)
            assert column in {c for c, _ in schema[table]}, rel[side]
