from __future__ import annotations

import pytest

from text2sql.agent.charts import recommend_chart
from text2sql.agent.profiling import profile_result


def test_empty_result():
    profile = profile_result(["城市", "企业数量"], [])

    assert profile.shape == "empty"
    assert profile.facts == []
    assert recommend_chart(profile) is None


def test_scalar_result_becomes_kpi():
    profile = profile_result(["企业数量"], [{"企业数量": 147}])

    assert profile.shape == "scalar"
    assert profile.facts[0].text == "企业数量为 147"
    assert profile.facts[0].numbers == [147]
    chart = recommend_chart(profile)
    assert chart.type == "kpi" and chart.y == ["企业数量"]


def test_category_metric_facts_include_top_share_and_concentration():
    rows = [
        {"城市": "广州市", "企业数量": 50},
        {"城市": "深圳市", "企业数量": 30},
        {"城市": "佛山市", "企业数量": 10},
        {"城市": "东莞市", "企业数量": 6},
        {"城市": "珠海市", "企业数量": 4},
    ]
    profile = profile_result(["城市", "企业数量"], rows)
    texts = [f.text for f in profile.facts]

    assert profile.shape == "category_metric"
    assert profile.dimension == "城市" and profile.measures == ["企业数量"]
    assert "共 5 个城市" in texts[0]
    assert "「广州市」的企业数量最高，为 50，占合计 100 的 50%" in texts
    assert "前 3 个城市合计占 90%" in texts
    assert any("「珠海市」" in t and "最低" in t for t in texts)


def test_non_additive_measures_do_not_get_shares():
    rows = [
        {"行业门类": "制造业", "平均注册资本（万元）": 800.5},
        {"行业门类": "建筑业", "平均注册资本（万元）": 300.0},
    ]
    texts = [f.text for f in profile_result(["行业门类", "平均注册资本（万元）"], rows).facts]

    assert not any("占" in t for t in texts)


def test_time_series_facts_and_partial_period_note():
    rows = [
        {"年份": 2023, "融资事件数": 20},
        {"年份": 2024, "融资事件数": 69},
        {"年份": 2025, "融资事件数": 103},
        {"年份": 2026, "融资事件数": 37},
    ]
    profile = profile_result(["年份", "融资事件数"], rows, anchor_year=2026)
    texts = [f.text for f in profile.facts]

    assert profile.shape == "time_series"
    assert "年份从 2023 到 2026，融资事件数由 20 变为 37（+17）" in texts
    assert "融资事件数在 2025 达到最高值 103" in texts
    assert any("2026" in t and "不完整" in t for t in texts)
    chart = recommend_chart(profile)
    assert chart.type == "line" and chart.x == "年份"


def test_long_format_series_get_one_line_per_series():
    rows = [
        {"国家": country, "年份": year, "预期寿命": base + delta}
        for country, base in (("中国", 44), ("印度", 37))
        for year, delta in ((1952, 0), (1982, 20), (2007, 29))
    ]

    profile = profile_result(["国家", "年份", "预期寿命"], rows)
    texts = [f.text for f in profile.facts]

    assert profile.shape == "multi_series"
    assert profile.dimension == "年份" and profile.series == "国家"
    assert "中国：年份从 1952 到 2007，预期寿命由 44 变为 73（+29）" in texts
    assert "在 2007，预期寿命最高的是「中国」，为 73" in texts
    chart = recommend_chart(profile)
    assert (chart.type, chart.x, chart.y, chart.series) == ("line", "年份", ["预期寿命"], "国家")


def test_wide_series_with_comparable_measures_are_described_together():
    rows = [
        {"年份": 2000, "中国": 3643.8, "美国": 6010.1, "印度": 978.2},
        {"年份": 2012, "中国": 9900.3, "美国": 5361.4, "印度": 1975.5},
        {"年份": 2024, "中国": 12289.0, "美国": 4904.1, "印度": 3193.5},
    ]

    profile = profile_result(["年份", "中国", "美国", "印度"], rows)
    texts = [f.text for f in profile.facts]

    assert profile.shape == "time_series"
    assert "中国：年份从 2000 到 2024，由 3643.8 变为 12289（+8645.2）" in texts
    assert "美国：年份从 2000 到 2024，由 6010.1 变为 4904.1（-1106）" in texts
    assert "在 2024，最高的是「中国」，为 12289" in texts
    assert recommend_chart(profile).y == ["中国", "美国", "印度"]


def test_measures_of_different_scale_are_not_drawn_on_one_axis():
    rows = [
        {"年份": 2023, "订单数": 120, "销售额": 560000.0},
        {"年份": 2024, "订单数": 150, "销售额": 610000.0},
        {"年份": 2025, "订单数": 170, "销售额": 700000.0},
    ]

    profile = profile_result(["年份", "订单数", "销售额"], rows)

    assert recommend_chart(profile).y == ["订单数"]
    assert "年份从 2023 到 2025，订单数由 120 变为 170（+50）" in [f.text for f in profile.facts]


def test_detail_rows_with_a_date_column_are_not_series():
    rows = [
        {"企业名称": name, "成立日期": day, "注册资本": capital}
        for name, day, capital in (("甲", "2020-01-01", 1.0), ("乙", "2021-01-01", 2.0))
    ]

    assert profile_result(["企业名称", "成立日期", "注册资本"], rows).shape == "table"


@pytest.mark.parametrize(
    "values",
    [["2025-01", "2025-02", "2025-03"], ["2024-01-01", "2024-02-01", "2024-03-01"]],
)
def test_date_strings_are_temporal(values):
    rows = [{"月份": v, "公告数": i + 1} for i, v in enumerate(values)]
    assert profile_result(["月份", "公告数"], rows).shape == "time_series"


def test_many_or_long_categories_use_horizontal_bars():
    rows = [
        {"企业名称": f"广州市测试企业第{i}号科技有限公司", "招投标记录数": 100 - i}
        for i in range(15)
    ]
    chart = recommend_chart(profile_result(["企业名称", "招投标记录数"], rows))

    assert chart.type == "barh"
    assert "横向" in chart.reason


def test_short_category_lists_use_vertical_bars():
    rows = [{"融资轮次": r, "融资事件数": n} for r, n in [("天使轮", 30), ("A轮", 24), ("B轮", 10)]]
    assert recommend_chart(profile_result(["融资轮次", "融资事件数"], rows)).type == "bar"


def test_share_column_is_not_used_as_primary_measure():
    rows = [
        {"企业类型": "有限责任公司", "企业数量": 46, "占比（%）": 60.0},
        {"企业类型": "个人独资企业", "企业数量": 30, "占比（%）": 40.0},
    ]
    profile = profile_result(["企业类型", "企业数量", "占比（%）"], rows)

    assert profile.measures[0] == "企业数量"
    assert recommend_chart(profile).y == ["企业数量"]


def test_null_dimension_values_are_counted():
    rows = [{"行业代码": None, "企业数量": 2}, {"行业代码": "I6510", "企业数量": 40}]
    profile = profile_result(["行业代码", "企业数量"], rows)

    assert profile.null_dimension_rows == 1


def test_wide_detail_table_has_no_chart():
    rows = [
        {"企业名称": "甲", "成立日期": "2020-01-01", "注册资本（万元）": 100.0, "经营状态": "存续"},
        {"企业名称": "乙", "成立日期": "2021-01-01", "注册资本（万元）": 50.0, "经营状态": "注销"},
    ]
    profile = profile_result(["企业名称", "成立日期", "注册资本（万元）", "经营状态"], rows)

    assert profile.shape == "table"
    assert recommend_chart(profile) is None


def test_truncation_is_reported_in_facts():
    rows = [{"企业名称": f"企业{i}", "记录数": i} for i in range(20)]
    profile = profile_result(["企业名称", "记录数"], rows, truncated=True)

    assert "结果已截断" in profile.facts[0].text


def test_facts_payload_is_compact_and_serializable():
    import json

    rows = [{"城市": "广州市", "企业数量": 50}, {"城市": "深圳市", "企业数量": 30}]
    payload = profile_result(["城市", "企业数量"], rows).facts_payload()

    json.dumps(payload, ensure_ascii=False)
    assert payload["row_count"] == 2
    assert payload["facts"]
    assert payload["preview"] == rows
