"""图表推荐：由结果形态决定图表类型，并写明理由。

原则：保留 SQL 给出的行顺序（排序是查询语义的一部分），不在图表层重新排序；
占比列是派生指标，不作为主轴；明细表不强行画图。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from text2sql.agent.profiling import ResultProfile, comparable_measures

_SHARE_MARKERS = ("占比", "比例", "%")


@dataclass
class ChartSpec:
    type: str  # kpi | bar | barh | line
    x: str | None
    y: list[str] = field(default_factory=list)
    title: str = ""
    reason: str = ""
    series: str | None = None  # 长表按该列拆成多条序列

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _primary_measures(profile: ResultProfile, limit: int) -> list[str]:
    plain = [m for m in profile.measures if not any(marker in m for marker in _SHARE_MARKERS)]
    return (plain or profile.measures)[:limit]


def recommend_chart(profile: ResultProfile, *, title: str = "") -> ChartSpec | None:
    if profile.shape == "empty" or not profile.measures:
        return None
    if profile.shape in ("scalar", "single_row"):
        return ChartSpec(
            "kpi", None, _primary_measures(profile, 4), title, "单行结果用指标卡展示关键数值"
        )
    if profile.shape == "time_series":
        if profile.row_count <= 2:
            return ChartSpec(
                "bar",
                profile.dimension,
                _primary_measures(profile, 1),
                title,
                "只有两个时间点，用柱状图对比",
            )
        together = comparable_measures(profile)
        return ChartSpec(
            "line",
            profile.dimension,
            together,
            title,
            "时间序列用折线图展示变化趋势"
            + ("，量级相近的指标画在同一根轴上" if len(together) > 1 else ""),
        )
    if profile.shape == "multi_series":
        return ChartSpec(
            "line",
            profile.dimension,
            _primary_measures(profile, 1),
            title,
            f"按{profile.series}拆成多条折线，对比各自的变化",
            series=profile.series,
        )
    if profile.shape == "category_metric":
        labels = [str(row.get(profile.dimension)) for row in profile.preview]
        longest = max((len(label) for label in labels), default=0)
        if profile.row_count > 12 or longest > 10:
            return ChartSpec(
                "barh",
                profile.dimension,
                _primary_measures(profile, 1),
                title,
                "类别较多或名称较长，用横向条形图便于阅读",
            )
        return ChartSpec(
            "bar", profile.dimension, _primary_measures(profile, 1), title, "类别对比用柱状图"
        )
    return None
