"""质量反思：在结果交给用户之前做一轮可解释的自检。

检查项都是确定性的，每一项都有明确的判定依据和给用户的说明；
它不改变查询结果，只决定解读用哪个版本、以及在界面上提示哪些风险。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from text2sql.agent.profiling import ResultProfile, format_number

_NUMBER = re.compile(r"(?<![A-Za-z0-9_])-?\d+(?:,\d{3})*(?:\.\d+)?")


@dataclass
class QualityCheck:
    name: str
    status: str  # pass | warn | fail
    detail: str


@dataclass
class QualityReport:
    checks: list[QualityCheck] = field(default_factory=list)

    @property
    def score(self) -> float:
        if not self.checks:
            return 1.0
        points = sum(
            1.0 if c.status == "pass" else 0.5 if c.status == "warn" else 0.0 for c in self.checks
        )
        return round(points / len(self.checks), 3)

    @property
    def ok(self) -> bool:
        return all(c.status != "fail" for c in self.checks)

    @property
    def warnings(self) -> list[QualityCheck]:
        return [c for c in self.checks if c.status != "pass"]

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "ok": self.ok, "checks": [asdict(c) for c in self.checks]}


def extract_numbers(text: str) -> list[float]:
    return [float(match.replace(",", "")) for match in _NUMBER.findall(text or "")]


def _close(value: float, candidates: list[float]) -> bool:
    return any(abs(value - c) <= max(0.011, abs(c) * 0.005) for c in candidates)


def check_numbers(
    narrative: str, *, profile: ResultProfile, rows: list[dict[str, Any]], question: str
) -> tuple[list[str], list[str]]:
    """返回（有出处的数字，无出处的数字）。10 以内的整数多为序号或“前 3”这类说法，不做核对。"""
    allowed = profile.allowed_numbers()
    for row in rows:
        for value in row.values():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                allowed.append(float(value))
            elif isinstance(value, str):
                allowed.extend(extract_numbers(value))
    allowed.extend(extract_numbers(question))

    grounded: list[str] = []
    ungrounded: list[str] = []
    for raw in _NUMBER.findall(narrative or ""):
        value = float(raw.replace(",", ""))
        if value.is_integer() and 0 <= value <= 10:
            continue
        shown = format_number(value)
        (grounded if _close(value, allowed) else ungrounded).append(shown)
    return grounded, ungrounded


def _placeholder_columns(catalog: Any) -> dict[str, str]:
    """宽表 → 标识真实事件的列（语义层里描述为“占位行”的那一列）。"""
    mapping = {}
    for name, doc in catalog.tables.items():
        if not doc.prefer:
            continue
        for column in doc.columns.values():
            if "占位行" in column.description:
                mapping[name] = column.name
                break
    return mapping


def _referenced_tables(sql: str) -> set[str]:
    try:
        return {t.name for t in sqlglot.parse_one(sql, read="mysql").find_all(exp.Table)}
    except SqlglotError:
        return set()


def reflect(
    *,
    question: str,
    task: str | None,
    top_n: int | None,
    sql: str | None,
    rows: list[dict[str, Any]],
    truncated: bool,
    max_rows: int,
    profile: ResultProfile,
    narrative_source: str,
    ungrounded_numbers: list[str],
    assumptions: list[str],
    planner: str | None,
    catalog: Any,
) -> QualityReport:
    report = QualityReport()
    add = report.checks.append
    row_count = len(rows)

    if row_count == 0:
        add(
            QualityCheck(
                "result_present", "warn", "查询没有返回数据，已如实说明，没有生成推断性结论"
            )
        )
    else:
        add(QualityCheck("result_present", "pass", f"返回 {row_count} 行"))

    if narrative_source == "llm_rejected":
        add(
            QualityCheck(
                "numbers_grounded",
                "warn",
                f"模型解读中的数字 {'、'.join(ungrounded_numbers)} 在结果里找不到出处，已改用确定性解读",
            )
        )
    elif narrative_source == "llm":
        add(QualityCheck("numbers_grounded", "pass", "模型解读中的数字均能在查询结果中找到出处"))
    else:
        add(QualityCheck("numbers_grounded", "pass", "解读由查询结果直接生成"))

    if truncated or row_count >= max_rows:
        add(QualityCheck("truncation", "warn", f"结果达到 {max_rows} 行上限，可能不完整"))
    else:
        add(QualityCheck("truncation", "pass", "结果未截断"))

    sql_text = sql or ""
    if task == "ranking" or top_n:
        if top_n and row_count > top_n:
            add(QualityCheck("top_n", "warn", f"问题要求前 {top_n} 名，但返回了 {row_count} 行"))
        elif not re.search(r"\bORDER\s+BY\b", sql_text, re.IGNORECASE):
            add(QualityCheck("top_n", "warn", "排名类问题的 SQL 没有 ORDER BY，返回顺序不可靠"))
        else:
            add(QualityCheck("top_n", "pass", "排名结果已排序且数量符合要求"))
    else:
        add(QualityCheck("top_n", "pass", "非排名类问题"))

    if task == "trend" and profile.shape == "time_series" and profile.dimension:
        sequence = [
            row.get(profile.dimension) for row in rows if row.get(profile.dimension) is not None
        ]
        keys = [str(v) if isinstance(v, str) else f"{float(v):012.2f}" for v in sequence]
        if keys != sorted(keys):
            add(QualityCheck("time_order", "warn", "趋势结果没有按时间先后排序"))
        else:
            add(QualityCheck("time_order", "pass", "趋势结果按时间排序"))
    else:
        add(QualityCheck("time_order", "pass", "非趋势类结果"))

    placeholders = _placeholder_columns(catalog)
    risky = []
    for table in sorted(_referenced_tables(sql_text) & set(placeholders)):
        column = placeholders[table]
        if not re.search(rf"\b{column}\s+IS\s+NOT\s+NULL\b", sql_text, re.IGNORECASE):
            risky.append(f"{table}（未过滤 {column} IS NOT NULL）")
    if risky:
        add(
            QualityCheck(
                "wide_table", "warn", "使用了含占位行的宽表：" + "、".join(risky) + "，计数可能偏大"
            )
        )
    else:
        add(QualityCheck("wide_table", "pass", "没有使用含占位行的宽表，或已正确过滤"))

    if profile.null_dimension_rows:
        add(
            QualityCheck(
                "null_dimension",
                "warn",
                f"分组中有 {profile.null_dimension_rows} 个空值类别，可能是源数据缺失",
            )
        )
    else:
        add(QualityCheck("null_dimension", "pass", "分组维度没有空值"))

    if planner == "llm" and not assumptions:
        add(QualityCheck("assumptions", "warn", "模型没有说明本次统计口径，请结合 SQL 自行确认"))
    elif assumptions:
        add(QualityCheck("assumptions", "pass", f"已披露 {len(assumptions)} 条口径假设"))
    else:
        add(QualityCheck("assumptions", "pass", "按语义层默认口径统计，没有额外假设"))

    return report
