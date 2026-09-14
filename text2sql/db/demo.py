"""合成演示库。

按生产 DDL 在 DuckDB 中建出同名的 6 张表和 5 个视图，用固定随机种子生成数据：

- 类别标签（经营状态、融资轮次、行政区划代码、GB/T 4754 行业代码等）与大致比例参考真实库的聚合统计，
  企业名称、项目标题、编号等全部合成，不含任何真实企业或个人信息；
- 刻意保留真实库里会让 Text2SQL 静默出错的“陷阱”：宽表中无事件企业的占位行、
  `status` 的长文本取值、带 `.0` 后缀的地区代码、稀疏金额字段。
"""

from __future__ import annotations

import json
import math
import os
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
from sqlglot import exp

from text2sql.db.backends import transpile
from text2sql.db.schema import TableDef, load_znjz_schema

DEMO_VERSION = "2026.09.14-1"
DEMO_SEED = 20260914
DEMO_ENTERPRISES = 5000
ANCHOR_DATE = datetime(2026, 6, 30)

# ---------------------------------------------------------------------------
# 参考分布（聚合统计，非行级数据）
# ---------------------------------------------------------------------------
# 分布表按“多列紧排”手工对齐，便于和统计口径逐项核对，不交给格式化工具展开
# fmt: off

# (状态文本, 状态码, 权重)。少数类别相对真实库上调，让分布类问题有可看的结果
STATUSES = [
    ("存续（在营、开业、在册）", 1, 9300),
    ("注销", 2, 250),
    ("注销企业", 2, 150),
    ("已注销", 2, 50),
    ("已吊销", 3, 60),
    ("迁出", 5, 50),
    ("撤销", 4, 40),
    ("仍注册", 1, 40),
    ("已告解散", 2, 40),
    ("核准設立", 19, 20),
]

ECON_KINDS = [
    ("有限责任公司（自然人独资）", "1151.0", 6322),
    ("有限责任公司（自然人投资或控股）", "1130.0", 6168),
    ("有限责任公司", "1100.0", 2224),
    ("有限责任公司（法人独资）", "1152.0", 1163),
    ("其他有限责任公司", "1190.0", 838),
    ("有限责任公司（自然人投资或控股的法人独资）", "1152.0", 200),
    ("有限责任公司（港澳台投资、非独资）", "6120.0", 73),
    ("股份有限公司（非上市、自然人投资或控股）", "1223.0", 62),
    ("个人独资企业", "4540.0", 61),
    ("有限合伙企业", "4533.0", 55),
    ("有限责任公司（外商投资、非独资）", "5120.0", 25),
    ("其他股份有限公司（非上市）", "1229.0", 24),
    ("有限责任公司（外商投资企业与内资合资）", "1122.0", 19),
]

CITY_NAMES = {
    "4401": "广州市", "4402": "韶关市", "4403": "深圳市", "4404": "珠海市", "4405": "汕头市",
    "4406": "佛山市", "4407": "江门市", "4408": "湛江市", "4409": "茂名市", "4412": "肇庆市",
    "4413": "惠州市", "4414": "梅州市", "4415": "汕尾市", "4416": "河源市", "4417": "阳江市",
    "4418": "清远市", "4419": "东莞市", "4420": "中山市", "4451": "潮州市", "4452": "揭阳市",
    "4453": "云浮市", "4400": "广东省", "1101": "北京市", "3301": "杭州市", "3402": "芜湖市",
    "3501": "福州市", "3608": "吉安市", "3701": "济南市", "5101": "成都市",
}

# (GB/T 2260 区县代码, 名称, 企业数权重)
DISTRICTS = [
    ("440106", "天河区", 2755), ("440112", "黄埔区", 1122), ("440305", "南山区", 1015),
    ("440113", "番禺区", 1010), ("440111", "白云区", 882), ("440307", "龙岗区", 815),
    ("441900", "东莞市", 804), ("440306", "宝安区", 776), ("440304", "福田区", 711),
    ("440115", "南沙区", 646), ("440309", "龙华区", 574), ("440118", "增城区", 538),
    ("440402", "香洲区", 500), ("440605", "南海区", 487), ("442000", "中山市", 458),
    ("440105", "海珠区", 324), ("441302", "惠城区", 318), ("440114", "花都区", 315),
    ("440104", "越秀区", 280), ("440606", "顺德区", 265), ("440303", "罗湖区", 261),
    ("440604", "禅城区", 244), ("440311", "光明区", 194), ("440103", "荔湾区", 155),
    ("441303", "惠阳区", 121), ("440117", "从化区", 121), ("440403", "斗门区", 108),
    ("440404", "金湾区", 104), ("440310", "坪山区", 86), ("440803", "霞山区", 73),
    ("440507", "龙湖区", 64), ("440902", "茂南区", 60), ("441202", "端州区", 54),
    ("441322", "博罗县", 54), ("441602", "源城区", 53), ("440607", "三水区", 53),
    ("440308", "盐田区", 52), ("440703", "蓬江区", 50), ("440802", "赤坎区", 50),
    ("441402", "梅江区", 46), ("440904", "电白区", 45), ("445202", "榕城区", 39),
    ("441802", "清城区", 36), ("445103", "潮安区", 32), ("440511", "金平区", 32),
    ("441702", "江城区", 30), ("441403", "梅县区", 28), ("440515", "澄海区", 28),
    ("440203", "武江区", 27), ("441284", "四会市", 25), ("441323", "惠东县", 25),
    ("440705", "新会区", 23), ("445102", "湘桥区", 22), ("440608", "高明区", 20),
    ("440783", "开平市", 19), ("440204", "浈江区", 19), ("441204", "高要区", 18),
    ("441203", "鼎湖区", 17),
]

# (GB/T 4754-2017 行业代码, 企业数权重)。制造业与批发业在真实库中极少，这里少量补充以便演示门类过滤
INDUSTRIES = [
    ("M7519", 1877), ("M7320", 1554), ("I6599", 1535), ("E5090", 754), ("M7499", 725),
    ("I6519", 715), ("I6560", 710), ("I6531", 664), ("I6513", 505), ("M7310", 425),
    ("E5012", 378), ("I6500", 365), ("E4999", 364), ("I6511", 348), ("E4790", 307),
    ("M7513", 300), ("M7510", 293), ("I6490", 292), ("I6510", 242), ("E5011", 229),
    ("M7300", 214), ("M7490", 212), ("M7481", 208), ("E4910", 198), ("E5010", 195),
    ("M7512", 173), ("M7515", 170), ("E4899", 163), ("M7484", 159), ("M7492", 152),
    ("E4990", 138), ("I6429", 131), ("E4700", 130), ("E4920", 128), ("M7530", 119),
    ("M7400", 112), ("E5000", 111), ("I6532", 103), ("E4710", 101), ("M7514", 92),
    ("E4900", 81), ("M7340", 80), ("E4800", 78), ("I6590", 65), ("E4890", 63),
    ("I6550", 63), ("L7119", 61), ("M7350", 59), ("I6450", 59), ("M7330", 57),
    ("L7113", 54), ("I6520", 51), ("M7511", 49), ("I6439", 47), ("E4891", 46),
    ("E5013", 45), ("I6540", 45), ("M7516", 44), ("E4813", 44), ("I6400", 39),
    ("C3491", 60), ("C3499", 35), ("C4011", 30), ("C3990", 20), ("F5176", 30),
]

SECTION_KEYWORDS = {
    "I": ["信息科技", "软件科技", "数据科技", "网络科技", "智能科技", "物联网科技", "云计算科技"],
    "M": ["科技", "技术服务", "检测技术", "工程技术", "自动化科技", "智能装备科技", "新材料科技"],
    "E": ["建设工程", "机电工程", "建筑工程", "智能化工程", "安装工程", "装饰工程"],
    "L": ["设备租赁", "商务服务"],
    "C": ["智能装备", "机器人", "电子科技", "精密机械", "自动化设备"],
    "F": ["贸易", "供应链管理"],
}

SCOPE_KEYWORDS = {
    "I": ["软件开发", "信息系统集成服务", "物联网技术服务", "数据处理和存储支持服务", "人工智能应用软件开发", "网络与信息安全软件开发"],
    "M": ["技术服务、技术开发、技术咨询", "工程和技术研究和试验发展", "智能控制系统集成", "检验检测服务", "新材料技术研发"],
    "E": ["建设工程施工", "电气安装服务", "建筑智能化系统设计", "消防设施工程施工", "机电设备安装"],
    "L": ["机械设备租赁", "建筑工程机械与设备租赁", "企业管理咨询"],
    "C": ["工业机器人制造", "智能基础制造装备制造", "工业自动控制系统装置制造", "电子元器件制造"],
    "F": ["计算机软硬件及辅助设备批发", "电子产品销售", "机械设备销售"],
}

BRAND_CHARS = "云智创联拓恒瑞达鼎越维凌捷睿衡启航星泽博远峰源盛宏安新铭骏朗弘煜霆锐昊旭晟驰铂"

ROUNDS = [
    ("股权投资", 0.0, 83), ("天使轮", 1011.0, 30), ("A轮", 1030.0, 24), ("新三板", 1110.0, 22),
    ("被收购", 1400.0, 19), ("Pre-A轮", 1020.0, 18), ("战略投资", 1200.0, 12), ("IPO", 1100.0, 11),
    ("B轮", 1050.0, 10), ("新三板定增", 1111.0, 8), ("股权转让", 1300.0, 7), ("主板定向增发", 1120.0, 7),
    ("A+轮", 1031.0, 5), ("B+轮", 1051.0, 4), ("种子轮", 1001.0, 3), ("Pre-B轮", 1040.0, 2),
    ("Pre-IPO", 1090.0, 1), ("C轮", 1060.0, 1),
]
FIN_YEARS = {
    2011: 1, 2012: 2, 2013: 6, 2014: 7, 2015: 21, 2016: 23, 2017: 28, 2018: 20, 2019: 23,
    2020: 30, 2021: 28, 2022: 25, 2023: 16, 2024: 18, 2025: 11, 2026: 2,
}
EVENTS_PER_FINANCED = [(1, 70), (2, 18), (3, 16), (4, 6), (5, 5), (6, 6), (8, 1), (10, 2)]
FUND_SUFFIXES = ["资本", "创投", "投资", "基金", "产业投资"]

INVEST_STATUSES = [
    ("存续（在营、开业、在册）", 5497), ("注销", 788), ("注销企业", 307), ("在营（开业）企业", 78),
    ("吊销，未注销", 28), ("开业", 16), ("已吊销", 12), ("吊销", 9), ("吊销，已注销", 8),
    ("迁出", 6), ("责令关闭", 3), ("已注销", 2), ("登记成立", 2),
]
INVESTMENTS_PER_INVESTOR = [
    (1, 50), (2, 15), (3, 10), (4, 7), (5, 5), (6, 3), (7, 2), (8, 2), (10, 2), (12, 1), (15, 1), (20, 1), (30, 1),
]
TARGET_KEYWORDS = ["科技", "实业", "商贸", "咨询", "文化传媒", "健康管理", "旅游", "新能源", "电子商务"]

BID_YEARS = {
    2008: 133, 2009: 200, 2010: 316, 2011: 320, 2012: 970, 2013: 2749, 2014: 4436, 2015: 7739,
    2016: 13281, 2017: 20121, 2018: 19595, 2019: 30560, 2020: 63139, 2021: 82336, 2022: 84437,
    2023: 76051, 2024: 79990, 2025: 76201, 2026: 13885,
}
BID_ROLES = [(30.0, 218179), (51.0, 175408), (40.0, 66682), (20.0, 55868), (50.0, 38367), (10.0, 22186)]
BID_NOTICES = [
    ((30.0, 3001.0), 443100), ((20.0, 2002.0), 59271), ((20.0, 2001.0), 16063), ((20.0, 2008.0), 11513),
    ((20.0, 2009.0), 10007), ((20.0, 2007.0), 5692), ((20.0, 2004.0), 5522), ((30.0, 3004.0), 4299),
    ((20.0, 2003.0), 2950), ((30.0, 3002.0), 2687), ((10.0, 1001.0), 1145), ((10.0, 1003.0), 737),
]
BID_AREAS = [
    ("4401", 118843), ("4403", 90639), ("4402", 31966), ("4406", 20195), ("4404", 15188),
    ("4413", 14119), ("4419", 14013), ("4420", 11624), ("4412", 11360), ("4409", 11312),
    ("4408", 11207), ("4407", 9466), ("4405", 8146), ("3402", 8049), ("4453", 6479),
    ("4400", 6334), ("4418", 6201), (None, 5841), ("4414", 5761), ("4415", 5644),
    ("4417", 5504), ("4451", 5248), ("4416", 4655), ("1101", 4312), ("4452", 3996),
    ("3301", 3907), ("3501", 3545), ("5101", 3357), ("3608", 3065), ("3701", 2711),
]
BUYERS = [
    "人民医院", "教育局", "政务服务数据管理局", "城市建设投资集团有限公司", "轨道交通有限公司", "供电局",
    "水务集团有限公司", "高新技术产业开发区管理委员会", "职业技术学院", "公安局", "交通运输局",
    "卫生健康委员会", "图书馆", "住房和城乡建设局", "中心小学",
]
PROJECTS = {
    "I": ["智慧园区综合管理平台建设项目", "政务云平台运维服务项目", "数据中心机房设备采购项目", "信息化系统运维服务项目",
          "网络安全等级保护测评服务", "视频监控系统升级改造项目", "数字孪生城市平台项目", "业务系统软件开发服务"],
    "M": ["检验检测设备采购项目", "科技成果转化服务项目", "工程技术咨询服务", "实验室仪器设备采购项目",
          "节能技术改造项目", "环境监测服务项目"],
    "E": ["楼宇智能化工程", "机电安装工程", "室内装修改造工程", "智慧工地系统建设项目", "消防设施改造工程", "道路照明工程"],
    "C": ["工业机器人设备采购项目", "自动化生产线改造项目", "智能仓储设备采购项目"],
    "L": ["工程机械设备租赁服务项目", "办公设备租赁服务项目"],
    "F": ["办公设备框架协议采购项目", "计算机设备采购项目"],
}
NOTICE_SUFFIXES = {
    30.0: ["中标（成交）结果公告", "中标候选人公示", "成交公告"],
    20.0: ["招标公告", "采购公告", "竞争性磋商公告"],
    10.0: ["采购意向公开", "资格预审公告"],
}
# 真实库 project_bid_money 的分位点（单位以源数据为准），(累积概率, 金额)
BID_MONEY_KNOTS = [(0.0, 0.05), (0.10, 0.2), (0.25, 0.72), (0.50, 8.0), (0.75, 97.0), (0.90, 473.0), (0.99, 19592.0), (1.0, 120000.0)]
CAPITAL_KNOTS = [(0.0, 1.0), (0.05, 3.0), (0.25, 50.0), (0.50, 100.0), (0.75, 500.0), (0.90, 1008.0), (0.95, 3000.0), (0.99, 10000.0), (1.0, 80000.0)]
NICE_CAPITALS = [1, 3, 5, 10, 20, 30, 50, 100, 200, 300, 500, 800, 1000, 2000, 3000, 5000, 10000, 20000, 50000]

QUAL_TYPES = [
    (110002.0, 9189), (110001.0, 2995), (110013.0, 1289), (110010.0, 607), (110045.0, 101),
    (110005.0, 55), (110033.0, 52), (110044.0, 43), (110028.0, 42), (110012.0, 31), (110047.0, 30),
]
QUAL_LEVELS = [(1.0, 9902), (2.0, 3824), (3.0, 711), (4.0, 49)]
QUAL_YEARS = {
    2013: 14, 2014: 31, 2015: 47, 2016: 81, 2017: 162, 2018: 609, 2019: 776, 2020: 999,
    2021: 1228, 2022: 2143, 2023: 3118, 2024: 2375, 2025: 2880, 2026: 3,
}

# 各行业门类参与各类事件的概率，让演示数据里的行业差异可解释
BID_PROBABILITY = {"E": 0.45, "I": 0.25, "M": 0.12, "C": 0.30, "L": 0.20, "F": 0.15}
FIN_PROBABILITY = {"I": 0.035, "M": 0.020, "C": 0.060, "E": 0.004, "L": 0.005, "F": 0.010}
QUAL_PROBABILITY = {"M": 0.12, "I": 0.10, "E": 0.03, "C": 0.20, "L": 0.02, "F": 0.02}
# fmt: on


# ---------------------------------------------------------------------------
# 采样工具
# ---------------------------------------------------------------------------


def _pick(rng: random.Random, items: Sequence[Any], weights: Sequence[float]) -> Any:
    return rng.choices(items, weights=weights, k=1)[0]


def _pick_weighted(rng: random.Random, pairs: Sequence[tuple[Any, float]]) -> Any:
    return _pick(rng, [p[0] for p in pairs], [p[1] for p in pairs])


def _from_knots(rng: random.Random, knots: Sequence[tuple[float, float]]) -> float:
    """在分位点之间做对数插值，得到与真实分布形状相近的长尾数值。"""
    u = rng.random()
    for (p0, v0), (p1, v1) in zip(knots, knots[1:], strict=False):
        if u <= p1:
            ratio = 0.0 if p1 == p0 else (u - p0) / (p1 - p0)
            return math.exp(math.log(v0) + ratio * (math.log(v1) - math.log(v0)))
    return knots[-1][1]


def _date_in_year(rng: random.Random, year: int, *, not_before: datetime | None = None) -> datetime:
    start = datetime(year, 1, 1)
    end = min(datetime(year, 12, 31), ANCHOR_DATE)
    if not_before is not None and not_before > start:
        start = not_before
    if start > end:
        # 事件年份早于成立日期时，宁可落在成立当天，也不能生成“成立前的事件”
        return min(start, ANCHOR_DATE)
    offset = rng.randint(0, (end - start).days)
    return start + timedelta(days=offset)


def _year_on_or_after(rng: random.Random, year_weights: dict[int, float], founded: datetime) -> int:
    """按真实年度分布抽样，但只在成立年份之后抽，避免把年轻企业的事件全部挤到最近一年。"""
    candidates = [(y, w) for y, w in year_weights.items() if y >= founded.year]
    if not candidates:
        return min(max(founded.year, max(year_weights)), ANCHOR_DATE.year)
    return _pick_weighted(rng, candidates)


def _maturity(founded: datetime) -> float:
    """成立时间越短，参与招投标、获得资质的概率越低。"""
    age = ANCHOR_DATE.year - founded.year
    if age >= 8:
        return 1.0
    if age >= 5:
        return 0.7
    if age >= 3:
        return 0.35
    return 0.1


def _brand(rng: random.Random) -> str:
    return "".join(rng.sample(BRAND_CHARS, 2))


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _hex(rng: random.Random, length: int) -> str:
    return f"{rng.getrandbits(length * 4):0{length}x}"


def _alnum(rng: random.Random, length: int) -> str:
    alphabet = "0123456789ABCDEFGHJKLMNPQRTUWXY"
    return "".join(rng.choice(alphabet) for _ in range(length))


# ---------------------------------------------------------------------------
# 实体生成
# ---------------------------------------------------------------------------


@dataclass
class _Enterprise:
    eid: str
    name: str
    district: str
    section: str
    start: datetime
    capital: float | None
    row: dict[str, Any]
    industry_row: dict[str, Any]


def _capital(rng: random.Random) -> float:
    value = _from_knots(rng, CAPITAL_KNOTS)
    if rng.random() < 0.75:
        value = min(NICE_CAPITALS, key=lambda nice: abs(math.log(nice) - math.log(value)))
    return round(value, 2)


def _company_name(
    rng: random.Random, city: str, section: str, econ_kind: str, used: set[str]
) -> str:
    keywords = SECTION_KEYWORDS[section]
    if "股份" in econ_kind:
        suffix = "股份有限公司"
    elif econ_kind == "有限合伙企业":
        suffix = "企业（有限合伙）"
    elif econ_kind == "个人独资企业":
        suffix = "经营部"
    else:
        suffix = "有限公司"
    for _ in range(50):
        brand = _brand(rng)
        keyword = rng.choice(keywords)
        style = rng.random()
        if style < 0.7:
            name = f"{city}{brand}{keyword}{suffix}"
        elif style < 0.85:
            name = f"{brand}{keyword}（{city.rstrip('市')}）{suffix}"
        else:
            name = f"{city.rstrip('市')}{brand}{keyword}{suffix}"
        if name not in used:
            used.add(name)
            return name
    raise RuntimeError("合成企业名称冲突过多")


def _make_enterprises(rng: random.Random, count: int) -> list[_Enterprise]:
    used: set[str] = set()
    district_codes = [d[0] for d in DISTRICTS]
    district_names = {d[0]: d[1] for d in DISTRICTS}
    district_weights = [d[2] for d in DISTRICTS]
    start_years = list(range(1985, 2027))
    # 成立年份权重：真实库成立年份逐年上升、2025 年达到峰值
    start_weights = [
        max(1.0, 3000 * math.exp(0.23 * (y - 2025))) if y < 2026 else 737 for y in start_years
    ]

    enterprises: list[_Enterprise] = []
    for index in range(count):
        district = _pick(rng, district_codes, district_weights)
        city = CITY_NAMES[district[:4]]
        industry = _pick_weighted(rng, INDUSTRIES)
        section = industry[0]
        status, status_code, _ = _pick(rng, STATUSES, [s[2] for s in STATUSES])
        econ_kind, econ_code, _ = _pick(rng, ECON_KINDS, [e[2] for e in ECON_KINDS])
        name = _company_name(rng, city, section, econ_kind, used)
        start = _date_in_year(rng, _pick(rng, start_years, start_weights))
        capital = None if rng.random() < 0.001 else _capital(rng)
        currency = _pick(rng, ["CNY", "HKD", "USD"], [9970, 20, 10])
        unit = {"CNY": "万元人民币", "HKD": "万港元", "USD": "万美元"}[currency]
        eid = _uuid(rng)

        logout_date = logout_reason = revoke_date = revoke_reason = None
        if "注销" in status or status in {"已告解散", "撤销"}:
            logout_date = min(ANCHOR_DATE, start + timedelta(days=rng.randint(400, 3000)))
            logout_reason = rng.choice(["决议解散", "经营期限届满", "其他原因"])
        if "吊销" in status:
            revoke_date = min(ANCHOR_DATE, start + timedelta(days=rng.randint(700, 3500)))
            revoke_reason = "不按照规定接受年度检验的，依法吊销营业执照。"

        credit_no = f"91{district}{_alnum(rng, 9)}{_alnum(rng, 1)}"
        scope = "；".join(
            rng.sample(SCOPE_KEYWORDS[section], k=min(3, len(SCOPE_KEYWORDS[section])))
        )
        row = {
            "id": 100000 + index * 7 + rng.randint(0, 6),
            "eid": eid,
            "name": name,
            "format_name": name,
            "credit_no": credit_no,
            "reg_no": f"{district}{rng.randint(10**8, 10**9 - 1)}",
            "org_no": credit_no[8:17],
            "status": status,
            "new_status_code": status_code,
            "type_new": 1.0,
            "category_new": 115601.0,
            "regist_capi": None if capital is None else f"{capital:g}{unit}",
            "regist_capi_new": capital,
            "actual_capi": None
            if capital is None or rng.random() < 0.7
            else round(capital * rng.uniform(0.1, 1.0), 2),
            "currency_unit": None if capital is None else currency,
            "start_date": start,
            "check_date": min(ANCHOR_DATE, start + timedelta(days=rng.randint(0, 3000))),
            "term_start": start.strftime("%Y-%m-%d"),
            "term_end": None if rng.random() < 0.97 else "5000-01-01",
            "belong_org": f"{city}{'' if district_names[district] == city else district_names[district]}市场监督管理局",
            "province_code": "440000",
            "district_code": district,
            # 敏感列：真实库为法定代表人姓名；演示库只放脱敏占位，SQL 安全门也禁止查询
            "oper_name": rng.choice("陈李张黄王林刘吴何郑") + "**",
            "oper_type": "P",
            "oper_name_id": _hex(rng, 32),
            "scope": scope,
            "collegues_num": None if rng.random() < 0.99 else f"{rng.randint(1, 80)}人",
            "logo_url": None,
            "url": None,
            "econ_kind": econ_kind,
            "econ_kind_code": econ_code,
            "title_code": "999A",
            "revoke_date": revoke_date,
            "revoke_reason": revoke_reason,
            "logout_date": logout_date,
            "logout_reason": logout_reason,
            "revoked_certificates": None,
            "created_time": start,
            "row_update_time": datetime(2025, 8, 1) + timedelta(days=rng.randint(0, 330)),
        }
        industry_code = None if rng.random() < 0.001 else industry
        industry_row = dict(row)
        industry_row.update(
            {
                "industry_code": industry_code,
                "ci_start_date": None if rng.random() < 0.9 else datetime(2023, 3, 8),
                "ci_create_time": None
                if industry_code is None
                else start + timedelta(days=rng.randint(0, 900)),
            }
        )
        enterprises.append(
            _Enterprise(eid, name, district, section, start, capital, row, industry_row)
        )
    return enterprises


def _financing_rows(rng: random.Random, enterprises: list[_Enterprise]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ent in enterprises:
        probability = FIN_PROBABILITY[ent.section] * (1.4 if ent.start.year >= 2012 else 0.5)
        if rng.random() >= probability:
            rows.append(dict(ent.row))
            continue
        for _ in range(_pick_weighted(rng, EVENTS_PER_FINANCED)):
            year = _year_on_or_after(rng, FIN_YEARS, ent.start)
            round_date = _date_in_year(rng, year, not_before=ent.start)
            round_name, round_type, _ = _pick(rng, ROUNDS, [r[2] for r in ROUNDS])
            amount = None
            if rng.random() < 0.41:
                amount = round(
                    _from_knots(
                        rng, [(0, 1e6), (0.1, 5.9e6), (0.5, 2.38e7), (0.9, 8.36e8), (1.0, 3e9)]
                    ),
                    -5,
                )
            estimated = (
                amount
                if amount is not None
                else (
                    0.0
                    if rng.random() < 0.12
                    else round(_from_knots(rng, [(0, 5e5), (0.5, 6e6), (0.9, 3e8), (1.0, 1e9)]), -5)
                )
            )
            investors = "，".join(
                _brand(rng) + rng.choice(FUND_SUFFIXES) for _ in range(rng.randint(1, 3))
            )
            row = dict(ent.row)
            row.update(
                {
                    "cf_id": 60000 + len(rows) * 13 + rng.randint(0, 12),
                    "cf_eid": ent.eid,
                    "ename": ent.name,
                    "round": round_name,
                    "round_type": round_type,
                    "round_date": round_date,
                    "amount": amount,
                    "currency": _pick(rng, ["CNY", None, "USD"], [200, 65, 2]),
                    "investors": investors,
                    "investors_json": json.dumps(
                        [{"text": n, "type": "investor"} for n in investors.split("，")],
                        ensure_ascii=False,
                    ),
                    "precise": 0.0,
                    "estimated_amount": estimated,
                    "pre_money": None,
                    "post_money": None,
                    "publish_date": None
                    if rng.random() < 0.02
                    else round_date + timedelta(days=rng.randint(0, 20)),
                    "newstitle": None,
                    "newslink": None,
                }
            )
            rows.append(row)
    return rows


def _investment_rows(rng: random.Random, enterprises: list[_Enterprise]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    statuses = [s[0] for s in INVEST_STATUSES]
    status_weights = [s[1] for s in INVEST_STATUSES]
    target_cities = list(CITY_NAMES.values())
    for ent in enterprises:
        capital = ent.capital or 0
        probability = 0.35 if capital >= 1000 else 0.15 if capital >= 100 else 0.06
        if rng.random() >= probability:
            rows.append({"name": ent.name})
            continue
        for _ in range(_pick_weighted(rng, INVESTMENTS_PER_INVESTOR)):
            if rng.random() < 0.2:
                target = rng.choice(enterprises)
                invest_eid, invest_name = target.eid, target.name
            else:
                invest_eid = _uuid(rng)
                invest_name = (
                    rng.choice(target_cities).rstrip("市")
                    + _brand(rng)
                    + rng.choice(TARGET_KEYWORDS)
                    + "有限公司"
                )
            stock = (
                1.0
                if rng.random() < 0.35
                else round(
                    _from_knots(
                        rng, [(0, 0.001), (0.15, 0.07), (0.5, 0.4), (0.85, 0.9), (1.0, 0.99)]
                    ),
                    4,
                )
            )
            should = round(
                _from_knots(rng, [(0, 0.5), (0.1, 5), (0.5, 150), (0.9, 2370), (1.0, 50000)]), 2
            )
            target_district = rng.choice(DISTRICTS)[0]
            rows.append(
                {
                    "name": ent.name,
                    "cinv_id": 40000 + len(rows) * 3 + rng.randint(0, 2),
                    "cinv_eid": ent.eid,
                    "cinv_name": ent.name,
                    "invest_eid": invest_eid,
                    "invest_name": invest_name,
                    "invest_credit_no": f"91{target_district}{_alnum(rng, 10)}",
                    "invest_reg_no": f"{target_district}{rng.randint(10**8, 10**9 - 1)}",
                    "invest_status": _pick(rng, statuses, status_weights),
                    "invest_oper_name": rng.choice("陈李张黄王林刘吴何郑") + "**",
                    "invest_regist_capi": f"{should * rng.choice([1, 2, 5, 10]):g}万元人民币",
                    "invest_start_date": _date_in_year(rng, rng.randint(2000, 2026)),
                    "stock_percent": stock,
                    "should_capi_conv": should,
                    "invest_quote_status": 0.0,
                    "cinv_belong_org": "市场监督管理局",
                    "belong_org_code": f"{target_district}.0",
                    "stock_num": 0.0,
                    "should_capi": should,
                    "currency_code": _pick(rng, ["CNY", None, "USD"], [95, 4, 1]),
                    "real_capi": round(should * rng.uniform(0.2, 1.0), 2)
                    if rng.random() < 0.18
                    else 0.0,
                    "should_con_date": None
                    if rng.random() < 0.25
                    else _date_in_year(rng, rng.randint(2026, 2050)),
                    "cinv_is_history": 1.0 if rng.random() < 0.2 else 0.0,
                    "cinv_u_tags": "0.0",
                }
            )
    return rows


def _area_code(rng: random.Random, prefix: str | None) -> str | None:
    if prefix is None:
        return None
    districts = [d[0] for d in DISTRICTS if d[0].startswith(prefix)]
    code = rng.choice(districts) if districts and rng.random() < 0.4 else f"{prefix}00"
    # 真实库的 area_code 从浮点导入，统一带 ".0" 后缀，等值过滤 '440300' 会静默返回 0 行
    return f"{code}.0"


def _bidding_rows(rng: random.Random, enterprises: list[_Enterprise]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    roles = [r[0] for r in BID_ROLES]
    role_weights = [r[1] for r in BID_ROLES]
    notices = [n[0] for n in BID_NOTICES]
    notice_weights = [n[1] for n in BID_NOTICES]
    areas = [a[0] for a in BID_AREAS]
    area_weights = [a[1] for a in BID_AREAS]
    for ent in enterprises:
        probability = (
            BID_PROBABILITY[ent.section]
            * _maturity(ent.start)
            * (1.3 if (ent.capital or 0) >= 500 else 1.0)
        )
        if rng.random() >= probability:
            rows.append({"name": ent.name})
            continue
        count = max(1, min(1200, int(round(rng.lognormvariate(math.log(6), 1.9)))))
        for _ in range(count):
            year = _year_on_or_after(rng, BID_YEARS, ent.start)
            main, sub = _pick(rng, notices, notice_weights)
            prefix = ent.district[:4] if rng.random() < 0.55 else _pick(rng, areas, area_weights)
            city = CITY_NAMES.get(prefix, "") if prefix else ""
            title = f"{city}{rng.choice(BUYERS)}{rng.choice(PROJECTS[ent.section])}{rng.choice(NOTICE_SUFFIXES[main])}"
            area = _area_code(rng, prefix) if rng.random() > 0.01 else None
            rows.append(
                {
                    "name": ent.name,
                    "cbid_id": 1_000_000 + len(rows) * 11 + rng.randint(0, 10),
                    "cbid_eid": ent.eid,
                    "u_id": _hex(rng, 24),
                    "role1": _pick(rng, roles, role_weights),
                    "title": title,
                    "publish_time": _date_in_year(rng, year, not_before=ent.start),
                    "area_code": area,
                    "notice_type_main": main,
                    "notice_type_sub": sub,
                    "project_number": None
                    if rng.random() < 0.21
                    else f"{(area or '440000.0')[:6]}-{year}-{rng.randint(10000, 99999)}",
                    "project_bid_money": round(_from_knots(rng, BID_MONEY_KNOTS), 4)
                    if rng.random() < 0.28
                    else None,
                }
            )
    return rows


def _district_label(rng: random.Random, code: str) -> str | None:
    city = code[:4]
    if city == "4401":
        options = ["广东省", None, "广州", "广州市", "黄埔区", "天河区", "广东省广州市天河区"]
    elif city == "4403":
        options = [
            "广东省",
            None,
            "深圳市",
            "广东深圳",
            "深圳市/南山区",
            "深圳市南山区",
            "深圳市/宝安区",
        ]
    else:
        options = ["广东省", None, "广东", CITY_NAMES.get(city)]
    return rng.choice(options)


def _qualification_rows(rng: random.Random, enterprises: list[_Enterprise]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    types = [t[0] for t in QUAL_TYPES]
    type_weights = [t[1] for t in QUAL_TYPES]
    levels = [lv[0] for lv in QUAL_LEVELS]
    level_weights = [lv[1] for lv in QUAL_LEVELS]
    for ent in enterprises:
        if rng.random() >= QUAL_PROBABILITY[ent.section] * _maturity(ent.start):
            rows.append({"name": ent.name})
            continue
        count = max(1, min(41, int(round(rng.triangular(1, 41, 5)))))
        for _ in range(count):
            year = _year_on_or_after(rng, QUAL_YEARS, ent.start)
            published = _date_in_year(rng, year, not_before=ent.start)
            # 真实库中年度名单类资质占多数，有效期多为一年，所以约八成已过期
            valid_end = published + timedelta(days=365 * rng.choice([1, 1, 1, 1, 2, 3]))
            if rng.random() < 0.002:
                state, state_code = "撤销", 5.0
            elif valid_end < ANCHOR_DATE:
                state, state_code = "过期", 3.0
            else:
                state, state_code = "有效", 1.0
            code = ent.district if rng.random() < 0.85 else rng.choice(DISTRICTS)[0]
            rows.append(
                {
                    "name": ent.name,
                    "ct_id": 9_000_000 + len(rows) * 5 + rng.randint(0, 4),
                    "ct_eid": ent.eid,
                    "_id": _hex(rng, 32),
                    "ct_name": ent.name,
                    "ct_type": _pick(rng, types, type_weights),
                    "ct_level": _pick(rng, levels, level_weights),
                    "ct_year": float(published.year),
                    "ct_publish_date": None if rng.random() < 0.02 else published,
                    "ct_district": _district_label(rng, code),
                    "ct_district_code": f"{code}.0",
                    "ct_check_date": None,
                    "ct_end_date": None,
                    "ct_valid_end": valid_end.strftime("%Y-%m-%d"),
                    "ct_valid_start": published.strftime("%Y-%m-%d"),
                    "ct_state": state,
                    "ct_state_code": state_code,
                    "ct_is_history": 1.0 if state != "有效" else 0.0,
                    "ct_u_tags": "0.0",
                }
            )
    return rows


# ---------------------------------------------------------------------------
# 写入 DuckDB
# ---------------------------------------------------------------------------

_ARROW_TYPES = {
    "bigint": pa.int64(),
    "int": pa.int64(),
    "varchar": pa.string(),
    "text": pa.string(),
    "double": pa.float64(),
    "decimal": pa.float64(),
    "datetime": pa.timestamp("us"),
    "date": pa.date32(),
    "binary": pa.binary(),
}


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _duckdb_type(column) -> str:
    return exp.DataType.build(column.raw_type, dialect="mysql").sql("duckdb")


def _write_table(
    con: duckdb.DuckDBPyConnection, table: TableDef, rows: list[dict[str, Any]]
) -> None:
    definitions = ", ".join(f"{_quote(c.name)} {_duckdb_type(c)}" for c in table.columns)
    con.execute(f"CREATE TABLE {_quote(table.name)} ({definitions})")
    for index, row in enumerate(rows, start=1):
        row["import_id"] = index
    staging = pa.table(
        {
            c.name: pa.array([row.get(c.name) for row in rows], type=_ARROW_TYPES[c.type])
            for c in table.columns
        }
    )
    con.register("_staging", staging)
    try:
        con.execute(f"INSERT INTO {_quote(table.name)} SELECT * FROM _staging")
    finally:
        con.unregister("_staging")


def build_demo_database(
    path: Path | str, *, seed: int = DEMO_SEED, enterprises: int = DEMO_ENTERPRISES
) -> Path:
    """生成演示库。先写临时文件再原子替换，避免并发进程读到半成品。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.building-{os.getpid()}")
    for leftover in (temp, temp.with_name(temp.name + ".wal")):
        if leftover.exists():
            leftover.unlink()

    rng = random.Random(seed)
    schema = load_znjz_schema()
    companies = _make_enterprises(rng, enterprises)
    table_rows = {
        "企业基本信息": [dict(c.row) for c in companies],
        "企业基本信息_行业代码": [dict(c.industry_row) for c in companies],
        "企业融资信息": _financing_rows(rng, companies),
        "企业投资股东信息": _investment_rows(rng, companies),
        "招投标信息": _bidding_rows(rng, companies),
        "商标资质信息": _qualification_rows(rng, companies),
    }

    con = duckdb.connect(str(temp))
    try:
        for table in schema.tables.values():
            if table.kind == "table":
                _write_table(con, table, table_rows[table.name])
        for view in schema.tables.values():
            if view.kind == "view":
                con.execute(
                    f"CREATE VIEW {_quote(view.name)} AS {transpile(view.view_sql, to='duckdb')}"
                )
        con.execute("CREATE TABLE _t2s_meta (key VARCHAR, value VARCHAR)")
        con.executemany(
            "INSERT INTO _t2s_meta VALUES (?, ?)",
            [
                ("version", DEMO_VERSION),
                ("seed", str(seed)),
                ("enterprises", str(enterprises)),
                ("anchor_date", ANCHOR_DATE.date().isoformat()),
            ],
        )
        con.execute("CHECKPOINT")
    finally:
        con.close()
    os.replace(temp, target)
    return target


def read_demo_version(path: Path | str) -> str | None:
    try:
        with duckdb.connect(str(path), read_only=True) as con:
            row = con.execute("SELECT value FROM _t2s_meta WHERE key = 'version'").fetchone()
            return row[0] if row else None
    except duckdb.Error:
        return None


def ensure_demo_database(path: Path | str) -> Path:
    target = Path(path)
    if target.exists() and read_demo_version(target) == DEMO_VERSION:
        return target
    return build_demo_database(target)
