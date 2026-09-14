"""数据卡片：内置数据集的中文说明、来源与许可证、表间关联、业务口径和推荐问题。

上传的文件只有自动画像，内置数据集在画像之上再叠一层人写的卡片：字段的中文名让中文问题能召回英文表，
表间关联和口径（比如“销售额按明细的成交单价算，不含运费”）写进提示词，模型不必自己猜。
卡片只描述看得见的事实，不含 SQL 片段，也不会替代安全门的检查。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

CARDS_DIR = Path(__file__).resolve().parent / "cards"
ROLES = ("time", "measure", "dimension", "text")


@dataclass(frozen=True)
class DatasetCard:
    id: str
    kind: str  # open：开源数据；sample：本项目生成的示例数据
    title: str
    tagline: str
    summary: str
    source: dict[str, str]
    license: dict[str, str]
    anchor_year: int | None = None
    tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    relationships: tuple[dict[str, Any], ...] = ()
    rules: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DatasetCard:
        tables = {}
        for name, spec in (raw.get("tables") or {}).items():
            columns = {}
            for column, value in ((spec or {}).get("columns") or {}).items():
                entry = {"label": value} if isinstance(value, str) else dict(value or {})
                if entry.get("role") not in (None, *ROLES):
                    raise ValueError(f"{raw['id']}.{name}.{column} 的 role 只能是 {ROLES}")
                columns[str(column)] = entry
            tables[str(name)] = {**(spec or {}), "columns": columns}
        return cls(
            id=str(raw["id"]),
            kind=str(raw.get("kind", "open")),
            title=str(raw["title"]),
            tagline=str(raw.get("tagline", "")),
            summary=" ".join(str(raw.get("summary", "")).split()),
            source=dict(raw.get("source") or {}),
            license=dict(raw.get("license") or {}),
            anchor_year=int(raw["anchor_year"]) if raw.get("anchor_year") else None,
            tables=tables,
            relationships=tuple(raw.get("relationships") or ()),
            rules=tuple(str(rule) for rule in raw.get("rules") or ()),
            suggestions=tuple(str(q) for q in raw.get("suggestions") or ()),
        )

    def public(self) -> dict[str, Any]:
        """页面展示用：不含口径规则以外的内部字段。"""
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "tagline": self.tagline,
            "summary": self.summary,
            "source": self.source,
            "license": self.license,
            "anchor_year": self.anchor_year,
            "tables": {
                name: {"label": spec.get("label", name), "description": spec.get("description", "")}
                for name, spec in self.tables.items()
            },
            "relationships": list(self.relationships),
            "rules": list(self.rules),
            "suggestions": list(self.suggestions),
        }


@cache
def load_card(dataset_id: str) -> DatasetCard:
    path = CARDS_DIR / f"{dataset_id}.yaml"
    if not path.is_file():
        raise KeyError(f"没有名为 {dataset_id} 的数据卡片")
    return DatasetCard.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def list_cards() -> list[DatasetCard]:
    return [load_card(path.stem) for path in sorted(CARDS_DIR.glob("*.yaml"))]


def apply_card(tables: Iterable[Any], card: DatasetCard) -> None:
    """把卡片里的表名、字段中文名、说明和角色写进导入画像（原地修改）。"""
    for table in tables:
        spec = card.tables.get(table.name) or {}
        table.label = str(spec.get("label") or table.label or table.name)
        table.description = str(spec.get("description") or table.description or "")
        table.synonyms = [str(word) for word in spec.get("synonyms") or ()]
        overrides = spec.get("columns") or {}
        for column in table.columns:
            extra = overrides.get(column["name"]) or {}
            if extra.get("label"):
                column["label"] = str(extra["label"])
            if extra.get("description"):
                column["note"] = str(extra["description"])
            if extra.get("role"):
                column["role"] = str(extra["role"])
            if extra.get("synonyms"):
                column["synonyms"] = [str(word) for word in extra["synonyms"]]
