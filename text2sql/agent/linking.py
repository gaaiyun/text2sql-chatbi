"""Schema 召回：为 LLM 路径挑出与问题相关的表和字段，并渲染成紧凑的提示词片段。

十来张表的规模不需要向量库。召回按“同义词精确命中 > 字符 bigram 相似度”打分；
宽表与其视图同时命中时优先视图，只有问题用到宽表独有的列（如 role1、should_capi_conv）才保留宽表。
v1 把 55 KB 的 schema 文档截断到前 12,000 字符整体塞进提示词，后半部分的视图根本进不去；
这里只渲染相关表，并补上粒度、关联键和真实取值。

兜底表、主表、伴随表这类与数据集相关的规则写在语义层的 linking 段里，没有配置时对所有表一视同仁。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from text2sql.db.schema import PhysicalSchema
from text2sql.semantic.catalog import SemanticCatalog
from text2sql.semantic.parser import normalize_question

_TYPE_DISPLAY = {
    "bigint": "BIGINT",
    "int": "INT",
    "varchar": "VARCHAR",
    "text": "TEXT",
    "double": "DOUBLE",
    "decimal": "DECIMAL",
    "datetime": "DATETIME",
    "date": "DATE",
}


def _bigrams(text: str) -> set[str]:
    cleaned = "".join(re.findall(r"[一-龥a-z0-9]", text))
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def _strip_unit(label: str) -> str:
    return re.sub(r"[（(].*?[）)]", "", label)


@dataclass
class LinkedTable:
    name: str
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class SchemaLink:
    tables: list[LinkedTable]

    def names(self) -> list[str]:
        return [t.name for t in self.tables]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tables": [
                {"name": t.name, "score": round(t.score, 2), "reasons": t.reasons}
                for t in self.tables
            ]
        }


class SchemaLinker:
    def __init__(self, catalog: SemanticCatalog, schema: PhysicalSchema) -> None:
        self.catalog = catalog
        self.schema = schema
        self._table_surfaces: dict[str, dict[str, float]] = {}
        self._column_surfaces: dict[str, dict[str, str]] = {}
        self._bigrams: dict[str, set[str]] = {}
        for name, doc in catalog.tables.items():
            table_surfaces = {
                normalize_question(s): 3.0 for s in (name, doc.label, *doc.synonyms) if s
            }
            column_surfaces: dict[str, str] = {}
            texts = [doc.label, doc.description, doc.grain, *doc.synonyms]
            for column in doc.columns.values():
                if catalog.is_sensitive(name, column.name):
                    continue
                for surface in (_strip_unit(column.label), *column.synonyms):
                    normalized = normalize_question(surface)
                    if len(normalized) >= 2:
                        column_surfaces.setdefault(normalized, column.name)
                if len(column.name) >= 4 and column.name.isascii():
                    column_surfaces.setdefault(column.name.lower(), column.name)
                texts.append(column.label)
            self._table_surfaces[name] = table_surfaces
            self._column_surfaces[name] = column_surfaces
            self._bigrams[name] = _bigrams(normalize_question("".join(texts)))

    # ------------------------------------------------------------------ 召回

    def link(
        self,
        question: str,
        *,
        top_k: int = 4,
        value_mentions: list[tuple[str, str, str]] | None = None,
    ) -> SchemaLink:
        text = normalize_question(question)
        question_bigrams = _bigrams(text)
        scored: dict[str, LinkedTable] = {}
        column_hits: dict[str, set[str]] = {}

        for name in self.catalog.tables:
            linked = LinkedTable(name=name, score=0.0)
            hits: set[str] = set()
            for surface, weight in self._table_surfaces[name].items():
                if surface and surface in text:
                    linked.score += weight
                    linked.reasons.append(f"命中表描述「{surface}」")
            for surface, column in self._column_surfaces[name].items():
                if surface in text:
                    linked.score += 1.5
                    hits.add(column)
                    linked.reasons.append(f"命中字段「{surface}」→ {column}")
            if question_bigrams:
                overlap = len(question_bigrams & self._bigrams[name]) / len(question_bigrams)
                linked.score += 2.0 * overlap
            for table, column, value in value_mentions or []:
                if table == name:
                    linked.score += 2.5
                    hits.add(column)
                    linked.reasons.append(f"问题中的「{value}」是 {column} 的真实取值")
            scored[name] = linked
            column_hits[name] = hits

        anchor = self.catalog.link_anchor
        anchor_table = self.schema.get(anchor["table"]) if anchor else None
        # 宽表只在命中视图和主表都没有的列时保留
        for name, doc in self.catalog.tables.items():
            if not doc.prefer or scored[name].score <= 0:
                continue
            view = self.schema.get(doc.prefer)
            exclusive = {
                column
                for column in column_hits[name]
                if not (view and view.has_column(column))
                and not (anchor_table and anchor_table.has_column(column))
            }
            if exclusive:
                scored[name].reasons.append(f"用到宽表独有字段：{'、'.join(sorted(exclusive))}")
            else:
                scored[name].score = 0.0

        kept = sorted((t for t in scored.values() if t.score >= 1.0), key=lambda t: -t.score)[
            :top_k
        ]
        if not kept:
            fallback = self.catalog.link_fallback or tuple(self.catalog.tables)
            return SchemaLink(
                tables=[
                    LinkedTable(name=n, score=0.0, reasons=["未命中具体表，提供全部分析表"])
                    for n in fallback
                ]
            )

        names = {t.name for t in kept}
        related = {str(rel["from"]).split(".")[0] for rel in self.catalog.relationships}
        if (
            anchor
            and anchor["table"] not in names
            and names & related
            and any(term in text for term in anchor["terms"])
        ):
            kept.append(LinkedTable(anchor["table"], 0.0, [anchor["reason"]]))
            names.add(anchor["table"])
        for companion in self.catalog.link_companions:
            if companion["table"] not in names and any(term in text for term in companion["terms"]):
                kept.append(LinkedTable(companion["table"], 0.0, [companion["reason"]]))
                names.add(companion["table"])
        return SchemaLink(tables=kept)

    # ------------------------------------------------------------------ 渲染

    def render(self, link: SchemaLink, *, values: Any | None = None) -> str:
        blocks = []
        for linked in link.tables:
            doc = self.catalog.tables[linked.name]
            physical = self.schema.get(linked.name)
            kind = "视图" if doc.kind == "view" else "表"
            lines = [f"### `{doc.name}`（{kind}）{doc.label}"]
            if doc.grain:
                lines.append(f"- 粒度：{doc.grain}")
            if doc.description:
                lines.append(f"- 说明：{doc.description}")
            for rel in self.catalog.relationships:
                if str(rel["from"]).startswith(f"{doc.name}."):
                    table, column = str(rel["from"]).split(".", 1)
                    target_table, target_column = str(rel["to"]).split(".", 1)
                    note = f"（{rel['note']}）" if rel.get("note") else ""
                    lines.append(
                        f"- 关联：`{table}`.{column} → `{target_table}`.{target_column}{note}"
                    )
            lines.append("- 字段：")
            for column in doc.columns.values():
                if self.catalog.is_sensitive(doc.name, column.name):
                    continue
                definition = physical.column(column.name) if physical else None
                data_type = _TYPE_DISPLAY.get(definition.type, "") if definition else ""
                extras = [column.description] if column.description else []
                if column.value_hint:
                    extras.append(column.value_hint)
                suffix = f"（{'；'.join(extras)}）" if extras else ""
                lines.append(f"  - {column.name} {data_type}：{column.label}{suffix}")
            if values is not None:
                hints = values.hints_for([doc.name])
                if hints:
                    lines.append("- 真实取值（出现次数）：")
                    lines.extend("  " + hint for hint in hints)
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)
