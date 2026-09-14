"""结果剖析：识别结果形态，并从数据本身算出确定性的事实。

解读里的每个数字都应该能在这里找到出处。LLM 只负责把事实说成人话，
质量反思节点会检查它有没有引入这里之外的数字。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

_DATE_VALUE = re.compile(r"^\d{4}-\d{2}(-\d{2})?( \d{2}:\d{2}:\d{2})?$")
_TIME_NAME = re.compile(r"年份|年度|月份|日期|时间|year|month|date", re.IGNORECASE)
_NON_ADDITIVE = re.compile(r"平均|均值|比例|占比|率|%|最高|最低|最大|最小|中位")
_SHARE = re.compile(r"占比|比例|%")
MAX_SERIES = 6


def format_number(value: float | int) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _percent(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass
class ColumnProfile:
    name: str
    kind: str  # numeric | temporal | categorical | text | empty
    non_null: int
    distinct: int
    minimum: Any = None
    maximum: Any = None
    total: float | None = None
    mean: float | None = None
    top_values: list[list[Any]] = field(default_factory=list)


@dataclass
class Fact:
    key: str
    text: str
    numbers: list[float] = field(default_factory=list)


@dataclass
class ResultProfile:
    row_count: int
    truncated: bool
    shape: str  # empty | scalar | single_row | category_metric | time_series | multi_series | table
    columns: list[ColumnProfile]
    dimension: str | None = None
    series: str | None = None  # multi_series：区分各条序列的类别列（长表：对象 × 时间 × 指标）
    measures: list[str] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    null_dimension_rows: int = 0
    preview: list[dict[str, Any]] = field(default_factory=list)

    def facts_payload(self) -> dict[str, Any]:
        return {
            "row_count": self.row_count,
            "truncated": self.truncated,
            "shape": self.shape,
            "columns": [c.name for c in self.columns],
            "facts": [f.text for f in self.facts],
            "preview": self.preview,
        }

    def allowed_numbers(self) -> list[float]:
        numbers = [float(self.row_count), float(len(self.columns))]
        for fact in self.facts:
            numbers.extend(fact.numbers)
        return numbers

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _classify(name: str, values: list[Any]) -> str:
    present = [v for v in values if v is not None]
    if not present:
        return "empty"
    if all(_is_number(v) for v in present):
        if _TIME_NAME.search(name) and all(
            float(v).is_integer() and 1900 <= v <= 2100 for v in present
        ):
            return "temporal"
        return "numeric"
    if all(isinstance(v, str) and _DATE_VALUE.match(v) for v in present):
        return "temporal"
    average_length = sum(len(str(v)) for v in present) / len(present)
    return "text" if average_length > 40 else "categorical"


def _column_profile(name: str, values: list[Any]) -> ColumnProfile:
    kind = _classify(name, values)
    present = [v for v in values if v is not None]
    profile = ColumnProfile(
        name=name, kind=kind, non_null=len(present), distinct=len({str(v) for v in present})
    )
    if kind == "numeric" and present:
        numbers = [float(v) for v in present]
        profile.minimum, profile.maximum = min(numbers), max(numbers)
        profile.total = round(sum(numbers), 4)
        profile.mean = round(sum(numbers) / len(numbers), 4)
    elif present:
        profile.minimum, profile.maximum = min(present, key=str), max(present, key=str)
        # 状态里只放 JSON 原生类型：检查点序列化后元组会变成列表，两种编排器的输出才能逐项一致
        profile.top_values = [[v, n] for v, n in Counter(str(v) for v in present).most_common(5)]
    return profile


def profile_result(
    columns: list[str],
    rows: list[dict[str, Any]],
    *,
    truncated: bool = False,
    anchor_year: int | None = None,
) -> ResultProfile:
    column_profiles = [_column_profile(name, [row.get(name) for row in rows]) for name in columns]
    profile = ResultProfile(
        row_count=len(rows),
        truncated=truncated,
        shape="table",
        columns=column_profiles,
        preview=rows[:10],
    )

    if not rows:
        profile.shape = "empty"
        return profile

    kinds = {c.name: c.kind for c in column_profiles}
    measures = [c.name for c in column_profiles if c.kind == "numeric"]
    measures.sort(key=lambda name: bool(_SHARE.search(name)))  # 占比列排在原始指标之后
    dimensions = [c.name for c in column_profiles if c.kind in ("temporal", "categorical", "text")]
    profile.measures = measures

    if len(rows) == 1:
        profile.shape = "scalar" if len(columns) == 1 and measures else "single_row"
        for name in measures:
            value = rows[0][name]
            if _is_number(value):
                profile.facts.append(
                    Fact(f"value:{name}", f"{name}为 {format_number(value)}", [float(value)])
                )
        return profile

    if len(dimensions) == 1 and measures:
        dimension = dimensions[0]
        profile.dimension = dimension
        profile.null_dimension_rows = sum(1 for row in rows if row.get(dimension) is None)
        if kinds[dimension] == "temporal":
            profile.shape = "time_series"
            together = comparable_measures(profile)
            if len(together) >= 2:
                _wide_series_facts(profile, rows, dimension, together, anchor_year)
            else:
                _time_series_facts(profile, rows, dimension, measures[0], anchor_year)
        else:
            profile.shape = "category_metric"
            _category_facts(profile, rows, dimension, measures[0])
        return profile

    # 长表：一个时间列 + 一个类别列（2 到 6 个对象，每个对象至少两个时间点），例如“中国和印度的预期寿命变化”
    profiles = {c.name: c for c in column_profiles}
    if len(dimensions) == 2 and measures:
        temporal = [name for name in dimensions if kinds[name] == "temporal"]
        category = [name for name in dimensions if kinds[name] == "categorical"]
        if len(temporal) == 1 and len(category) == 1:
            groups = profiles[category[0]].distinct
            if (
                2 <= groups <= MAX_SERIES
                and len(rows) >= 2 * groups
                and profiles[temporal[0]].distinct >= 2
            ):
                profile.shape = "multi_series"
                profile.dimension, profile.series = temporal[0], category[0]
                _multi_series_facts(profile, rows, temporal[0], category[0], measures[0])
                return profile

    profile.facts.append(
        Fact(
            "table",
            f"共返回 {len(rows)} 行、{len(columns)} 列" + ("（结果已截断）" if truncated else ""),
            [len(rows), len(columns)],
        )
    )
    return profile


def _category_facts(
    profile: ResultProfile, rows: list[dict[str, Any]], dimension: str, measure: str
) -> None:
    valued = [
        (row.get(dimension), float(row[measure])) for row in rows if _is_number(row.get(measure))
    ]
    if not valued:
        return
    count_text = f"共 {len(rows)} 个{dimension}"
    if profile.truncated:
        count_text += "（结果已截断，只统计了返回的部分）"
    profile.facts.append(Fact("count", count_text, [len(rows)]))

    additive = not _NON_ADDITIVE.search(measure)
    total = sum(v for _, v in valued)
    top_label, top_value = max(valued, key=lambda item: item[1])
    text = f"「{top_label}」的{measure}最高，为 {format_number(top_value)}"
    numbers = [top_value]
    if additive and total > 0:
        share = _percent(top_value, total)
        text += f"，占合计 {format_number(total)} 的 {format_number(share)}%"
        numbers += [total, share]
    profile.facts.append(Fact("top", text, numbers))

    if additive and total > 0 and len(valued) >= 5:
        top3 = sum(sorted((v for _, v in valued), reverse=True)[:3])
        share = _percent(top3, total)
        profile.facts.append(
            Fact(
                "concentration", f"前 3 个{dimension}合计占 {format_number(share)}%", [top3, share]
            )
        )

    if len(valued) >= 3:
        low_label, low_value = min(valued, key=lambda item: item[1])
        profile.facts.append(
            Fact(
                "bottom",
                f"「{low_label}」的{measure}最低，为 {format_number(low_value)}",
                [low_value],
            )
        )


def comparable_measures(profile: ResultProfile, limit: int = MAX_SERIES) -> list[str]:
    """能画在同一根纵轴上的指标：第一个指标，加上最大值与它相差不到 10 倍的其他指标（占比列除外）。

    宽表（“中国”“美国”“印度”各占一列）的各列量级相近，适合一起画；“订单数”和“销售额”差几个数量级，
    画在一起会把小的那条压成一条直线。
    """
    if not profile.measures:
        return []
    maxima = {c.name: c.maximum for c in profile.columns}
    first = profile.measures[0]
    base = abs(float(maxima.get(first) or 0))
    chosen = [first]
    for name in profile.measures[1:]:
        value = abs(float(maxima.get(name) or 0))
        if base and value and 0.1 <= value / base <= 10 and not _SHARE.search(name):
            chosen.append(name)
    return chosen[:limit]


def _time_key(value: Any) -> str:
    return str(value) if not _is_number(value) else f"{float(value):012.2f}"


def _shown(value: Any) -> str:
    return format_number(value) if _is_number(value) else str(value)


def _multi_series_facts(
    profile: ResultProfile,
    rows: list[dict[str, Any]],
    dimension: str,
    series: str,
    measure: str,
) -> None:
    grouped: dict[str, list[tuple[Any, float]]] = {}
    for row in rows:
        if row.get(dimension) is None or not _is_number(row.get(measure)):
            continue
        grouped.setdefault(str(row.get(series)), []).append((row[dimension], float(row[measure])))
    latest: list[tuple[str, float, Any]] = []
    for name, points in grouped.items():
        points.sort(key=lambda item: _time_key(item[0]))
        if len(points) < 2:
            continue
        (first_t, first_v), (last_t, last_v) = points[0], points[-1]
        change = last_v - first_v
        profile.facts.append(
            Fact(
                f"change:{name}",
                f"{name}：{dimension}从 {_shown(first_t)} 到 {_shown(last_t)}，"
                f"{measure}由 {format_number(first_v)} 变为 {format_number(last_v)}"
                f"（{'+' if change >= 0 else '-'}{format_number(abs(change))}）",
                [first_v, last_v, abs(change)]
                + ([float(first_t), float(last_t)] if _is_number(first_t) else []),
            )
        )
        latest.append((name, last_v, last_t))
    common = {str(t) for _, _, t in latest}
    if len(latest) >= 2 and len(common) == 1:
        name, value, when = max(latest, key=lambda item: item[1])
        profile.facts.append(
            Fact(
                "latest_top",
                f"在 {_shown(when)}，{measure}最高的是「{name}」，为 {format_number(value)}",
                [value] + ([float(when)] if _is_number(when) else []),
            )
        )


def _wide_series_facts(
    profile: ResultProfile,
    rows: list[dict[str, Any]],
    dimension: str,
    measures: list[str],
    anchor_year: int | None,
) -> None:
    ordered = sorted(
        (row for row in rows if row.get(dimension) is not None),
        key=lambda row: _time_key(row[dimension]),
    )
    if len(ordered) < 2:
        return
    first, last = ordered[0], ordered[-1]
    for name in measures[:3]:
        if not (_is_number(first.get(name)) and _is_number(last.get(name))):
            continue
        first_v, last_v = float(first[name]), float(last[name])
        change = last_v - first_v
        profile.facts.append(
            Fact(
                f"change:{name}",
                f"{name}：{dimension}从 {_shown(first[dimension])} 到 {_shown(last[dimension])}，"
                f"由 {format_number(first_v)} 变为 {format_number(last_v)}"
                f"（{'+' if change >= 0 else '-'}{format_number(abs(change))}）",
                [first_v, last_v, abs(change)]
                + (
                    [float(first[dimension]), float(last[dimension])]
                    if _is_number(first[dimension])
                    else []
                ),
            )
        )
    latest = [(name, float(last[name])) for name in measures if _is_number(last.get(name))]
    if len(latest) >= 2:
        name, value = max(latest, key=lambda item: item[1])
        when = last[dimension]
        profile.facts.append(
            Fact(
                "latest_top",
                f"在 {_shown(when)}，最高的是「{name}」，为 {format_number(value)}",
                [value] + ([float(when)] if _is_number(when) else []),
            )
        )
    _partial_fact(profile, last[dimension], anchor_year)


def _partial_fact(profile: ResultProfile, last_t: Any, anchor_year: int | None) -> None:
    if _is_number(last_t):
        last_year: int | None = int(last_t)
    elif str(last_t)[:4].isdigit():
        last_year = int(str(last_t)[:4])
    else:
        last_year = None
    if anchor_year is not None and last_year == anchor_year:
        profile.facts.append(
            Fact(
                "partial",
                f"{anchor_year} 年的数据不满一年，最后一期不完整，不宜与往年直接比较",
                [float(anchor_year)],
            )
        )


def _time_series_facts(
    profile: ResultProfile,
    rows: list[dict[str, Any]],
    dimension: str,
    measure: str,
    anchor_year: int | None,
) -> None:
    points = sorted(
        (
            (row[dimension], float(row[measure]))
            for row in rows
            if row.get(dimension) is not None and _is_number(row.get(measure))
        ),
        key=lambda item: _time_key(item[0]),
    )
    if len(points) < 2:
        return
    (first_t, first_v), (last_t, last_v) = points[0], points[-1]
    change = last_v - first_v
    sign = "+" if change >= 0 else "-"
    profile.facts.append(
        Fact(
            "change",
            f"{dimension}从 {format_number(first_t) if _is_number(first_t) else first_t} 到 {format_number(last_t) if _is_number(last_t) else last_t}，"
            f"{measure}由 {format_number(first_v)} 变为 {format_number(last_v)}（{sign}{format_number(abs(change))}）",
            [first_v, last_v, abs(change)]
            + ([float(first_t), float(last_t)] if _is_number(first_t) else []),
        )
    )
    peak_t, peak_v = max(points, key=lambda item: item[1])
    profile.facts.append(
        Fact(
            "peak",
            f"{measure}在 {format_number(peak_t) if _is_number(peak_t) else peak_t} 达到最高值 {format_number(peak_v)}",
            [peak_v] + ([float(peak_t)] if _is_number(peak_t) else []),
        )
    )
    _partial_fact(profile, last_t, anchor_year)
