"""少样本检索：从已审查的 Q→SQL 对里挑出与当前问题最相似的几条。

相似度 = 字面 bigram 相似度 × 0.40 + 与召回表的重合度 × 0.30 + 结构信号相似度 × 0.30。

- “企业、公司、数量”一类词几乎出现在每个问题里，计算 bigram 前先剔除，
  否则“中标最多的企业”会因为共享“最多”“企业”而匹配到“对外投资最多的企业”；
- 只看字面会被表面措辞带偏：“有融资但没有招投标的名单”与“各城市有融资记录的企业数量”
  字面更像，但真正决定 SQL 形状的是“否定 + 存在性 + 名单”。结构信号从问题措辞和示例 SQL
  （NOT EXISTS、窗口函数、CASE 分桶）两侧提取，让形状相同的示例排在前面。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import sqlglot
from sqlglot import exp

from text2sql.semantic.parser import normalize_question

ZNJZ_EXEMPLARS_PATH = Path(__file__).resolve().parents[1] / "datasets" / "znjz" / "exemplars.jsonl"

DOMAIN_STOPWORDS = (
    "企业",
    "公司",
    "数量",
    "统计",
    "多少",
    "哪些",
    "按照",
    "各个",
    "每个",
    "有多少",
    "的",
    "了",
    "和",
    "与",
    "及",
)


def _bigrams(text: str) -> set[str]:
    cleaned = normalize_question(text)
    for word in DOMAIN_STOPWORDS:
        cleaned = cleaned.replace(word, " ")
    grams: set[str] = set()
    for chunk in re.findall(r"[一-龥a-z0-9]+", cleaned):
        grams.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
        if len(chunk) == 1:
            grams.add(chunk)
    return grams


_QUESTION_FEATURES = {
    "negation": re.compile(r"没有|从未|未参与|未获得|无融资|无招投标|无资质|并非|不是"),
    "existence": re.compile(r"有.{0,6}记录|获得过|参与过|拥有|同时"),
    "ranking": re.compile(r"最多|最高|最大|最少|最低|最早|最晚|前\d|top|排名|排行"),
    "trend": re.compile(r"每年|每月|逐年|历年|趋势|变化|按年|月度|同比|环比"),
    "share": re.compile(r"占比|比例|比重"),
    "bucket": re.compile(r"区间|分档|分段"),
    "latest": re.compile(r"最近一次|最新一次"),
    "list": re.compile(r"名单|列出|有哪些|明细"),
}
_SQL_FEATURES = {
    "negation": re.compile(r"\bNOT\s+(EXISTS|IN|LIKE)\b", re.IGNORECASE),
    "existence": re.compile(r"\bEXISTS\b", re.IGNORECASE),
    "window": re.compile(r"\bOVER\s*\(", re.IGNORECASE),
    "bucket": re.compile(r"\bCASE\s+WHEN\b.*\bGROUP\s+BY\b", re.IGNORECASE | re.DOTALL),
}


def _features(question: str, sql: str | None = None) -> frozenset[str]:
    text = normalize_question(question)
    found = {name for name, pattern in _QUESTION_FEATURES.items() if pattern.search(text)}
    if sql:
        found |= {name for name, pattern in _SQL_FEATURES.items() if pattern.search(sql)}
    return frozenset(found)


def question_similarity(first: str, second: str) -> float:
    """两个问题的字面相似度（剔除领域停用词后的 bigram Jaccard）。"""
    a, b = _bigrams(first), _bigrams(second)
    return len(a & b) / len(a | b) if a and b else 0.0


def _structure_similarity(query: frozenset[str], example: frozenset[str]) -> float:
    if not query and not example:
        return 0.5
    if not query or not example:
        return 0.0
    return len(query & example) / len(query | example)


def _tables(sql: str) -> tuple[str, ...]:
    tree = sqlglot.parse_one(sql, read="mysql")
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    return tuple(dict.fromkeys(t.name for t in tree.find_all(exp.Table) if t.name not in ctes))


@dataclass(frozen=True)
class Exemplar:
    id: str
    question: str
    sql: str
    tags: tuple[str, ...]
    tables: tuple[str, ...]


class ExemplarStore:
    def __init__(self, exemplars: Sequence[Exemplar]) -> None:
        self.exemplars = list(exemplars)
        self._bigrams = {e.id: _bigrams(e.question) for e in self.exemplars}
        self._features = {e.id: _features(e.question, e.sql) for e in self.exemplars}

    @classmethod
    def load(cls, path: Path | str) -> ExemplarStore:
        exemplars = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            exemplars.append(
                Exemplar(
                    id=raw["id"],
                    question=raw["question"],
                    sql=raw["sql"],
                    tags=tuple(raw.get("tags", [])),
                    tables=_tables(raw["sql"]),
                )
            )
        return cls(exemplars)

    def add(self, question: str, sql: str, *, prefix: str = "confirmed") -> Exemplar:
        """加入一条经人确认的 Q→SQL（例如页面上被标记为正确的回答），之后相似的问题可以检索到它。"""
        index = sum(1 for e in self.exemplars if e.id.startswith(f"{prefix}-")) + 1
        exemplar = Exemplar(
            id=f"{prefix}-{index}", question=question, sql=sql, tags=(prefix,), tables=_tables(sql)
        )
        self.exemplars.append(exemplar)
        self._bigrams[exemplar.id] = _bigrams(question)
        self._features[exemplar.id] = _features(question, sql)
        return exemplar

    def search(
        self,
        question: str,
        *,
        tables: Iterable[str] = (),
        k: int = 3,
        min_score: float = 0.12,
    ) -> list[tuple[Exemplar, float]]:
        query = _bigrams(question)
        query_features = _features(question)
        wanted = set(tables)
        if not query:
            return []
        scored = []
        for exemplar in self.exemplars:
            grams = self._bigrams[exemplar.id]
            text_score = len(query & grams) / len(query | grams) if grams else 0.0
            table_score = (
                len(wanted & set(exemplar.tables)) / len(set(exemplar.tables))
                if wanted and exemplar.tables
                else 0.0
            )
            structure_score = _structure_similarity(query_features, self._features[exemplar.id])
            score = 0.40 * text_score + 0.30 * table_score + 0.30 * structure_score
            # 文本毫无交集时，只靠表重合不足以说明问题相似
            if text_score == 0:
                continue
            if score >= min_score:
                scored.append((exemplar, round(score, 4)))
        scored.sort(key=lambda item: -item[1])
        return scored[:k]

    @staticmethod
    def render(results: Sequence[tuple[Exemplar, float]]) -> str:
        blocks = [
            f"问题：{exemplar.question}\n```sql\n{exemplar.sql}\n```" for exemplar, _ in results
        ]
        return "\n\n".join(blocks)
