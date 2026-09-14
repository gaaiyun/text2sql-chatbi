"""语义层目录：加载、展开并按生产 DDL 校验 semantic.yaml。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp
from sqlglot.errors import ParseError

from text2sql.db.schema import PhysicalSchema

ZNJZ_SEMANTIC_PATH = Path(__file__).resolve().parents[1] / "datasets" / "znjz" / "semantic.yaml"


def sql_string(value: str) -> str:
    """把任意文本安全地写成 SQL 字符串字面量。"""
    return exp.Literal.string(value).sql("mysql")


@dataclass(frozen=True)
class ColumnDoc:
    name: str
    label: str
    role: str
    description: str = ""
    synonyms: tuple[str, ...] = ()
    value_hint: str | None = None
    scan_values: bool = False


@dataclass(frozen=True)
class TableDoc:
    name: str
    kind: str
    label: str
    description: str
    grain: str = ""
    synonyms: tuple[str, ...] = ()
    prefer: str | None = None
    columns: dict[str, ColumnDoc] = field(default_factory=dict)


@dataclass(frozen=True)
class ListColumn:
    sql: str
    label: str


@dataclass(frozen=True)
class Example:
    question: str
    category: str
    requires_llm: bool = False


@dataclass(frozen=True)
class Entity:
    key: str
    label: str
    table: str
    alias: str
    synonyms: tuple[str, ...]
    default_metric: str
    time_dimension: str | None
    joins: dict[str, str]
    list_columns: tuple[ListColumn, ...]
    list_order: str
    list_order_note: str = ""
    list_requires: tuple[str, ...] = ()
    assumption: str | None = None


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    synonyms: tuple[str, ...]
    sql: dict[str, str]
    labels: dict[str, str] = field(default_factory=dict)
    assumption: str | None = None

    def label_for(self, entity: str) -> str:
        return self.labels.get(entity, self.label)


@dataclass(frozen=True)
class Dimension:
    key: str
    label: str
    synonyms: tuple[str, ...]
    sql: str
    requires: tuple[str, ...]
    group_sql: str | None = None
    order_sql: str | None = None
    time: bool = False
    assumption: str | None = None


@dataclass(frozen=True)
class Filter:
    key: str
    label: str
    synonyms: tuple[str, ...]
    sql: str
    requires: tuple[str, ...]
    time_column: str | None = None
    assumption: str | None = None
    group: str = ""

    def render(self, time_condition: str | None = None) -> str:
        """time_condition 形如 ">= 2020"，拼进存在性子查询内部，保证时间约束作用在事件上。"""
        if "{time}" not in self.sql:
            return self.sql
        extra = (
            f" AND {self.time_column} {time_condition}"
            if time_condition and self.time_column
            else ""
        )
        return self.sql.replace("{time}", extra)


@dataclass(frozen=True)
class ValueMap:
    key: str
    label: str
    column: str
    match: str
    requires: tuple[str, ...]
    labels: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    assumption: str | None = None

    def surfaces(self) -> dict[str, str]:
        result = {label: code for code, label in self.labels.items()}
        result.update(self.aliases)
        return result

    def condition(self, code: str) -> str:
        if self.match == "prefix":
            return f"{self.column} LIKE {sql_string(code + '%')}"
        return f"{self.column} = {sql_string(code)}"

    def label_of(self, code: str) -> str:
        return self.labels.get(code, code)


def _case_sql(code_table: dict[str, str], expr: str, else_label: str = "其他") -> str:
    branches = " ".join(
        f"WHEN {sql_string(code)} THEN {sql_string(label)}" for code, label in code_table.items()
    )
    return f"CASE {expr} {branches} ELSE {sql_string(else_label)} END"


def _tuple(values: Any) -> tuple[str, ...]:
    return tuple(str(v) for v in (values or []))


def _link_rule(spec: Any) -> dict[str, Any] | None:
    """召回补充规则：问题里出现 terms 中的词时，把 table 一并交给 SQL 智能体。"""
    if not spec:
        return None
    return {
        "table": str(spec["table"]),
        "terms": _tuple(spec.get("terms")),
        "reason": str(spec.get("reason", "")),
    }


@dataclass
class SemanticCatalog:
    dataset: str
    title: str
    description: str
    anchor_year: int
    default_list_limit: int
    default_top_limit: int
    sensitive_columns: frozenset[str]
    rules: tuple[str, ...]
    aliases: dict[str, str]
    tables: dict[str, TableDoc]
    relationships: tuple[dict[str, Any], ...]
    code_tables: dict[str, dict[str, str]]
    entities: dict[str, Entity]
    metrics: dict[str, Metric]
    dimensions: dict[str, Dimension]
    filters: dict[str, Filter]
    value_maps: dict[str, ValueMap]
    count_words: tuple[str, ...]
    time_words: tuple[str, ...]
    scoped_dimension_words: dict[str, dict[str, str]]
    detail_columns: tuple[ListColumn, ...] = ()
    detail_requires: tuple[str, ...] = ()
    detail_order: str = ""
    detail_limit: int = 5
    examples: tuple[Example, ...] = ()
    # 领域词与召回规则：数据集相关的假设都写在语义层里，代码对任何数据集一视同仁
    domain_words: tuple[str, ...] = ()
    domain_topics: str = ""
    open_domain: bool = False
    link_fallback: tuple[str, ...] = ()
    link_anchor: dict[str, Any] | None = None
    link_companions: tuple[dict[str, Any], ...] = ()
    anchor_partial: bool = False  # 锚定年份的数据是否不满一年（趋势结果的最后一期需要提示）

    # ------------------------------------------------------------------ 加载

    @classmethod
    def load(cls, path: Path | str) -> SemanticCatalog:
        return cls.from_dict(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SemanticCatalog:
        code_tables = {
            k: {str(c): str(label) for c, label in (v or {}).items()}
            for k, v in (raw.get("code_tables") or {}).items()
        }

        tables = {}
        for name, spec in (raw.get("tables") or {}).items():
            columns = {
                col: ColumnDoc(
                    name=col,
                    label=str(c.get("label", col)),
                    role=str(c.get("role", "text")),
                    description=str(c.get("description", "")),
                    synonyms=_tuple(c.get("synonyms")),
                    value_hint=c.get("value_hint"),
                    scan_values=bool(c.get("scan_values", False)),
                )
                for col, c in (spec.get("columns") or {}).items()
            }
            tables[name] = TableDoc(
                name=name,
                kind=str(spec.get("kind", "table")),
                label=str(spec.get("label", name)),
                description=str(spec.get("description", "")),
                grain=str(spec.get("grain", "")),
                synonyms=_tuple(spec.get("synonyms")),
                prefer=spec.get("prefer"),
                columns=columns,
            )

        entities = {
            key: Entity(
                key=key,
                label=str(spec["label"]),
                table=str(spec["table"]),
                alias=str(spec["alias"]),
                synonyms=_tuple(spec.get("synonyms")),
                default_metric=str(spec["default_metric"]),
                time_dimension=spec.get("time_dimension"),
                joins={str(a): str(s) for a, s in (spec.get("joins") or {}).items()},
                list_columns=tuple(
                    ListColumn(sql=str(c["sql"]), label=str(c["label"]))
                    for c in spec.get("list_columns") or []
                ),
                list_order=str(spec.get("list_order", "")),
                list_order_note=str(spec.get("list_order_note", "")),
                list_requires=_tuple(spec.get("list_requires")),
                assumption=spec.get("assumption"),
            )
            for key, spec in (raw.get("entities") or {}).items()
        }

        metrics = {
            key: Metric(
                key=key,
                label=str(spec["label"]),
                synonyms=_tuple(spec.get("synonyms")),
                sql={str(e): str(s) for e, s in (spec.get("sql") or {}).items()},
                labels={str(e): str(s) for e, s in (spec.get("labels") or {}).items()},
                assumption=spec.get("assumption"),
            )
            for key, spec in (raw.get("metrics") or {}).items()
        }

        dimensions = {}
        for key, spec in (raw.get("dimensions") or {}).items():
            if "case" in spec:
                case = spec["case"]
                sql = _case_sql(code_tables.get(case["code_table"], {}), str(case["expr"]))
            else:
                sql = str(spec["sql"])
            dimensions[key] = Dimension(
                key=key,
                label=str(spec["label"]),
                synonyms=_tuple(spec.get("synonyms")),
                sql=sql,
                requires=_tuple(spec.get("requires")),
                group_sql=spec.get("group_sql"),
                order_sql=spec.get("order_sql"),
                time=bool(spec.get("time", False)),
                assumption=spec.get("assumption"),
            )

        filters = {
            key: Filter(
                key=key,
                label=str(spec["label"]),
                synonyms=_tuple(spec.get("synonyms")),
                sql=str(spec["sql"]),
                requires=_tuple(spec.get("requires")),
                time_column=spec.get("time_column"),
                assumption=spec.get("assumption"),
                group=str(spec.get("group") or key),
            )
            for key, spec in (raw.get("filters") or {}).items()
        }

        value_maps = {
            key: ValueMap(
                key=key,
                label=str(spec["label"]),
                column=str(spec["column"]),
                match=str(spec.get("match", "equals")),
                requires=_tuple(spec.get("requires")),
                labels=dict(code_tables.get(spec.get("code_table") or "", {})),
                aliases={str(s): str(c) for s, c in (spec.get("aliases") or {}).items()},
                assumption=spec.get("assumption"),
            )
            for key, spec in (raw.get("value_maps") or {}).items()
        }

        domain = raw.get("domain") or {}
        linking = raw.get("linking") or {}
        return cls(
            domain_words=_tuple(domain.get("words")),
            domain_topics=str(domain.get("topics", "")),
            open_domain=bool(domain.get("open", False)),
            link_fallback=_tuple(linking.get("fallback")),
            link_anchor=_link_rule(linking.get("anchor")),
            link_companions=tuple(
                rule for rule in map(_link_rule, linking.get("companions") or []) if rule
            ),
            dataset=str(raw.get("dataset", "")),
            title=str(raw.get("title", "")),
            description=str(raw.get("description", "")),
            anchor_year=int(raw.get("anchor_year", 2026)),
            anchor_partial=bool(raw.get("anchor_partial", False)),
            default_list_limit=int(raw.get("default_list_limit", 20)),
            default_top_limit=int(raw.get("default_top_limit", 10)),
            sensitive_columns=frozenset(str(c).lower() for c in raw.get("sensitive_columns") or []),
            rules=_tuple(raw.get("rules")),
            aliases={str(a): str(t) for a, t in (raw.get("aliases") or {}).items()},
            tables=tables,
            relationships=tuple(raw.get("relationships") or []),
            code_tables=code_tables,
            entities=entities,
            metrics=metrics,
            dimensions=dimensions,
            filters=filters,
            value_maps=value_maps,
            count_words=_tuple(raw.get("count_words")),
            time_words=_tuple(raw.get("time_words")),
            scoped_dimension_words={
                str(word): {str(e): str(d) for e, d in (mapping or {}).items()}
                for word, mapping in (raw.get("scoped_dimension_words") or {}).items()
            },
            detail_columns=tuple(
                ListColumn(sql=str(c["sql"]), label=str(c["label"]))
                for c in (raw.get("detail") or {}).get("columns") or []
            ),
            detail_requires=_tuple((raw.get("detail") or {}).get("requires")),
            detail_order=str((raw.get("detail") or {}).get("order", "")),
            detail_limit=int((raw.get("detail") or {}).get("limit", 5)),
            examples=tuple(
                Example(
                    question=str(e["question"]),
                    category=str(e.get("category", "")),
                    requires_llm=bool(e.get("requires_llm", False)),
                )
                for e in raw.get("examples") or []
            ),
        )

    # ------------------------------------------------------------------ 查询

    @property
    def whitelist(self) -> set[str]:
        return set(self.tables)

    def is_sensitive(self, table: str, column: str) -> bool:
        return column.strip('`"').lower() in self.sensitive_columns

    def metric_sql(self, metric: str, entity: str) -> str | None:
        spec = self.metrics.get(metric)
        return spec.sql.get(entity) if spec else None

    def scoped_dimension(self, word: str, entity: str) -> str | None:
        mapping = self.scoped_dimension_words.get(word)
        if not mapping:
            return None
        return mapping.get(entity, mapping.get("default"))

    def summary(self) -> dict[str, Any]:
        """对外展示的语义层概要（API / MCP / 界面共用）：业务口径和同义词，不含 SQL 片段。"""
        return {
            "dataset": self.dataset,
            "title": self.title,
            "description": self.description,
            "anchor_year": self.anchor_year,
            "entities": [
                {"key": e.key, "label": e.label, "table": e.table, "synonyms": list(e.synonyms)}
                for e in self.entities.values()
            ],
            "metrics": [
                {
                    "key": m.key,
                    "label": m.label,
                    "synonyms": list(m.synonyms),
                    "entities": [entity for entity, sql in m.sql.items() if sql],
                }
                for m in self.metrics.values()
            ],
            "dimensions": [
                {"key": d.key, "label": d.label, "synonyms": list(d.synonyms), "time": d.time}
                for d in self.dimensions.values()
            ],
            "filters": [
                {"key": f.key, "label": f.label, "synonyms": list(f.synonyms), "group": f.group}
                for f in self.filters.values()
            ],
            "value_maps": [
                {"key": v.key, "label": v.label, "values": len(v.labels)}
                for v in self.value_maps.values()
            ],
            "rules": list(self.rules),
            "examples": [
                {"question": e.question, "category": e.category, "requires_llm": e.requires_llm}
                for e in self.examples
            ],
        }

    def value_scan_targets(self) -> list[tuple[str, str]]:
        return [
            (table.name, column.name)
            for table in self.tables.values()
            for column in table.columns.values()
            if column.scan_values
        ]

    def ambiguous_surfaces(self) -> dict[str, list[str]]:
        targets: dict[str, set[str]] = defaultdict(set)
        for key, entity in self.entities.items():
            for word in entity.synonyms:
                targets[word].add(f"entity:{key}")
        for key, metric in self.metrics.items():
            for word in metric.synonyms:
                targets[word].add(f"metric:{key}")
        for word in self.count_words:
            targets[word].add("count_word")
        for key, dimension in self.dimensions.items():
            for word in dimension.synonyms:
                targets[word].add(f"dimension:{key}")
        for word in self.scoped_dimension_words:
            targets[word].add("scoped_dimension")
        for word in self.time_words:
            targets[word].add("time_word")
        for key, flt in self.filters.items():
            for word in flt.synonyms:
                targets[word].add(f"filter:{key}")
        for key, value_map in self.value_maps.items():
            for word, code in value_map.surfaces().items():
                targets[word].add(f"value:{key}:{code}")
        return {word: sorted(found) for word, found in targets.items() if len(found) > 1}

    # ------------------------------------------------------------------ 校验

    def validate(self, schema: PhysicalSchema) -> list[str]:
        problems: list[str] = []

        for name, doc in self.tables.items():
            table = schema.get(name)
            if table is None:
                problems.append(f"tables.{name}: 生产库中不存在该表或视图")
                continue
            for column in doc.columns:
                if not table.has_column(column):
                    problems.append(f"tables.{name}.{column}: 列不存在")
            if doc.prefer and doc.prefer not in self.tables:
                problems.append(f"tables.{name}.prefer: {doc.prefer} 未在语义层登记")

        linked = [("linking.fallback", t) for t in self.link_fallback]
        linked += [("linking.anchor", self.link_anchor["table"])] if self.link_anchor else []
        linked += [("linking.companions", c["table"]) for c in self.link_companions]
        for section, table_name in linked:
            if table_name not in self.tables:
                problems.append(f"{section}: {table_name} 未在语义层登记")

        for alias, table_name in self.aliases.items():
            if schema.get(table_name) is None:
                problems.append(f"aliases.{alias}: 表 {table_name} 不存在")

        def check(location: str, sql: str, wrap: str) -> None:
            try:
                tree = sqlglot.parse_one(wrap.format(sql=sql), read="mysql")
            except ParseError as exc:
                problems.append(f"{location}: 无法解析（{str(exc).splitlines()[0]}）")
                return
            for table_node in tree.find_all(exp.Table):
                if table_node.name != "__probe__" and schema.get(table_node.name) is None:
                    problems.append(f"{location}: 表 {table_node.name} 不存在")
            for column in tree.find_all(exp.Column):
                alias = column.table
                if not alias:
                    continue
                table_name = self.aliases.get(alias)
                if table_name is None:
                    problems.append(f"{location}: 未登记的别名 {alias}")
                    continue
                table = schema.get(table_name)
                if table is not None and not table.has_column(column.name):
                    problems.append(f"{location}: {table_name} 没有列 {column.name}")

        expr_wrap = "SELECT {sql} FROM __probe__"
        for key, entity in self.entities.items():
            if schema.get(entity.table) is None:
                problems.append(f"entities.{key}.table: {entity.table} 不存在")
            if self.aliases.get(entity.alias) != entity.table:
                problems.append(f"entities.{key}.alias: 别名 {entity.alias} 未指向 {entity.table}")
            if self.metric_sql(entity.default_metric, key) is None:
                problems.append(
                    f"entities.{key}.default_metric: {entity.default_metric} 没有该实体的表达式"
                )
            if entity.time_dimension and entity.time_dimension not in self.dimensions:
                problems.append(f"entities.{key}.time_dimension: {entity.time_dimension} 不存在")
            for alias, join in entity.joins.items():
                check(f"entities.{key}.joins.{alias}", join, "SELECT 1 FROM __probe__ {sql}")
            for index, column in enumerate(entity.list_columns):
                check(f"entities.{key}.list_columns[{index}]", column.sql, expr_wrap)
            if entity.list_order:
                check(
                    f"entities.{key}.list_order",
                    entity.list_order,
                    "SELECT 1 FROM __probe__ ORDER BY {sql}",
                )

        for index, column in enumerate(self.detail_columns):
            check(f"detail.columns[{index}]", column.sql, expr_wrap)
        if self.detail_order:
            check("detail.order", self.detail_order, "SELECT 1 FROM __probe__ ORDER BY {sql}")

        for key, metric in self.metrics.items():
            for entity_key, sql in metric.sql.items():
                if entity_key not in self.entities:
                    problems.append(f"metrics.{key}.sql: 未知实体 {entity_key}")
                check(f"metrics.{key}.sql[{entity_key}]", sql, expr_wrap)

        for key, dimension in self.dimensions.items():
            check(f"dimensions.{key}.sql", dimension.sql, expr_wrap)
            if dimension.group_sql:
                check(
                    f"dimensions.{key}.group_sql",
                    dimension.group_sql,
                    "SELECT 1 FROM __probe__ GROUP BY {sql}",
                )
            if dimension.order_sql:
                check(f"dimensions.{key}.order_sql", dimension.order_sql, expr_wrap)

        for key, flt in self.filters.items():
            check(
                f"filters.{key}.sql", flt.render(">= 2000"), "SELECT 1 FROM __probe__ WHERE {sql}"
            )

        for key, value_map in self.value_maps.items():
            if value_map.match not in {"prefix", "equals"}:
                problems.append(f"value_maps.{key}.match: 只能是 prefix 或 equals")
            check(f"value_maps.{key}.column", value_map.column, expr_wrap)

        for word, mapping in self.scoped_dimension_words.items():
            for entity_key, dimension in mapping.items():
                if entity_key != "default" and entity_key not in self.entities:
                    problems.append(f"scoped_dimension_words.{word}: 未知实体 {entity_key}")
                if dimension not in self.dimensions:
                    problems.append(f"scoped_dimension_words.{word}: 未知维度 {dimension}")

        for index, rel in enumerate(self.relationships):
            for side in ("from", "to"):
                table_name, _, column = str(rel.get(side, "")).partition(".")
                table = schema.get(table_name)
                if table is None or not table.has_column(column):
                    problems.append(f"relationships[{index}].{side}: {rel.get(side)} 不存在")

        return problems


@lru_cache(maxsize=1)
def load_znjz_catalog() -> SemanticCatalog:
    return SemanticCatalog.load(ZNJZ_SEMANTIC_PATH)
