"""规则解析器：把中文问题映射为语义层查询计划。

设计约束是“覆盖检查”：问题里每一个实义词都必须被语义层词表、数量/时间槽位或功能词解释。
只要剩下解释不了的词就放弃，交给 LLM 路径或拒答。否定词（没有、无、未、非）不在功能词表里，
所以“没有专利的企业”会因为“专利”和“没有”无法解释而被放弃，而不是悄悄变成“所有企业”。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

from text2sql.semantic.catalog import SemanticCatalog, sql_string

# --------------------------------------------------------------------------- 文本规范化

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}  # fmt: skip
_CN_NUMBER = "[零〇一二两三四五六七八九十百]+"
_CN_IN_CONTEXT = re.compile(
    rf"(前|近|最近|过去|超过|满|大于|多于|少于|不足|不到|低于|高于|top)({_CN_NUMBER})"
    rf"|({_CN_NUMBER})(年|名|家|个|条|项|位|笔|次|强|大)"
)
NAME_TOKEN = "〔企业名〕"


def cn_to_int(text: str) -> int | None:
    if not text:
        return None
    if "百" in text:
        head, _, tail = text.partition("百")
        hundreds = _CN_DIGITS.get(head, 1) if head else 1
        rest = cn_to_int(tail) if tail else 0
        return None if rest is None else hundreds * 100 + rest
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    if len(text) == 1:
        return _CN_DIGITS.get(text)
    return None


def normalize_question(text: str) -> str:
    """全角转半角、去空白、ASCII 小写，并把数量语境里的中文数字转成阿拉伯数字。"""
    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = re.sub(r"\s+", "", normalized).lower()

    def replace(match: re.Match[str]) -> str:
        if match.group(2):
            number = cn_to_int(match.group(2))
            return match.group(1) + (str(number) if number is not None else match.group(2))
        number = cn_to_int(match.group(3))
        return (str(number) if number is not None else match.group(3)) + match.group(4)

    return _CN_IN_CONTEXT.sub(replace, normalized)


# --------------------------------------------------------------------------- 计划结构


@dataclass
class AppliedFilter:
    key: str
    label: str
    sql: str
    requires: tuple[str, ...] = ()


@dataclass
class QueryPlan:
    entity: str
    mode: str  # aggregate | group | list | detail
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    filters: list[AppliedFilter] = field(default_factory=list)
    order: str | None = None  # desc | asc | time | dimension
    order_sql: str | None = None
    order_label: str | None = None
    limit: int | None = None
    share: bool = False
    detail_name: str | None = None
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Match:
    kind: str
    key: str
    surface: str
    start: int
    end: int


@dataclass
class ParseResult:
    question: str
    normalized: str
    plan: QueryPlan | None = None
    matches: list[Match] = field(default_factory=list)
    unexplained: list[str] = field(default_factory=list)
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.plan is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "normalized": self.normalized,
            "plan": self.plan.to_dict() if self.plan else None,
            "matches": [asdict(m) for m in self.matches],
            "unexplained": self.unexplained,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- 词表
# 词表按语义成组紧排，展开成一行一个词反而难以通读，所以不交给格式化工具
# fmt: off

CUE_WORDS: dict[str, tuple[str, ...]] = {
    "list": ("列出", "名单", "清单", "有哪些", "是哪些", "都有哪些", "明细", "列表", "都有谁"),
    "which_one": ("哪一家", "哪家", "哪个"),
    "which_many": ("哪些",),
    "rank_desc": ("最多", "最高", "最大", "最活跃", "排名靠前", "领先"),
    "rank_asc": ("最少", "最低", "最小"),
    "rank": ("排名", "排行", "排序"),
    "share": ("占比", "比例", "比重", "份额", "百分比"),
    "group": ("按照", "按", "各个", "各", "每个", "每一个", "不同", "分别", "分布", "构成", "分组", "结构"),
    "detail": ("基本信息", "详细信息", "详情", "概况", "资料", "画像", "简介", "工商信息"),
    "founded": ("成立",),
}

STOP_WORDS: tuple[str, ...] = (
    "请问", "请", "帮我", "帮忙", "麻烦", "一下", "统计", "查询", "查一下", "查一查", "查看", "看看", "看一下",
    "分析", "给出", "给我", "展示", "显示", "输出", "计算", "汇总", "求", "列举", "告诉我", "我想知道", "想知道",
    "了解", "获得", "拿到", "完成", "的", "了", "吗", "呢", "啊", "吧", "是", "为", "有", "在", "和", "与", "及",
    "以及", "并且", "并", "且", "或者", "或", "但", "但是", "而且", "同时", "其中", "中", "里", "里面", "之中",
    "当中", "这些", "那些", "所有", "全部", "全体", "整体", "总体", "目前", "当前", "现在", "库中", "库里",
    "数据库", "数据", "信息", "情况", "相关", "对应", "都", "共", "一共", "总共", "合计", "总计", "根据", "基于",
    "记录", "条", "家", "个", "项", "名", "位", "笔", "次", "情形", "什么", "怎么样", "如何", "怎样", "分别是",
    # 追问语气词。注意“去掉/不限/取消”不在这里：独立问题里吞掉它们会把“去掉存续”理解成“存续”
    "那么", "那", "换成", "改成", "改为", "如果是", "只看", "再看", "再按", "看下", "看",
)
# fmt: on

_ALIAS_ENTITY_SKIP = {"i"}


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


# --------------------------------------------------------------------------- 槽位

_NAME_QUOTED = re.compile(r"[“\"「『‘']([^”\"」』’']{2,60})[”\"」』’']")
_NAME_SUFFIXED = re.compile(
    r"(?:^|查询|查看|查一下|看看|了解|介绍|关于)"
    r"([一-龥A-Za-z0-9（）()·]{2,60}?(?:股份有限公司|有限责任公司|有限公司|集团有限公司|集团|研究院|研究所))"
)

_YEAR_RANGE = re.compile(r"(\d{4})年?(?:到|至|-|~|—)(\d{4})年")
_YEAR_SINCE = re.compile(r"(\d{4})年(?:以来|之后|以后|起|开始|至今|及以后)")
_YEAR_BEFORE = re.compile(r"(\d{4})年(?:之前|以前)")
_YEAR_RECENT = re.compile(r"(?:近|最近|过去)(\d{1,2})年")
_YEAR_RELATIVE = re.compile(r"今年|本年度|本年|去年|前年")
_YEAR_SINGLE = re.compile(r"(\d{4})年(?:度)?")
_AGE_OVER = re.compile(
    r"成立(?:时间)?(?:超过|满|大于|多于|不少于|已满|已经超过)(\d{1,3})年(?:以上)?|成立(\d{1,3})年以上"
)
_AGE_UNDER = re.compile(r"成立(?:时间)?(?:不足|不到|少于|未满|小于)(\d{1,3})年")
_CAPITAL = re.compile(
    r"注册资本(超过|大于|高于|多于|不低于|不少于|达到|低于|小于|少于|不足|不到|在)?"
    r"(\d+(?:\.\d+)?)(万|亿)(?:元)?(?:人民币)?(以上|及以上|以下|及以下|以内)?"
)
_ORDER_CAPITAL = re.compile(r"注册资本(最高|最大|最多|最低|最少|最小)")
_ORDER_FOUNDED = re.compile(r"成立(?:时间|日期)?(最早|最久|最晚|最新|最近)")
_TOP = re.compile(r"(?:top|排名前|排行前|前)(\d{1,3})(?:名|位|家|个|强|大|条)?")
_TOP_AFTER_RANK = re.compile(
    r"(最多|最高|最大|最活跃|最少|最低|最小)的(\d{1,3})(?:名|位|家|个|条)?"
)
_COUNT_N = re.compile(r"(\d{1,3})(?:家|个|名|条|项|位)")
_NESTED_COUNT = "先取排名靠前的企业、再在其中计数需要子查询，语义层不支持，需交给模型生成 SQL"


@dataclass
class _Slot:
    kind: str
    start: int
    end: int
    value: Any = None


class SemanticParser:
    def __init__(self, catalog: SemanticCatalog) -> None:
        self.catalog = catalog
        self.alias_entity = {entity.alias: key for key, entity in catalog.entities.items()}
        self._lexicon = self._build_lexicon()

    # ------------------------------------------------------------------ 词表构建

    def _build_lexicon(self) -> list[tuple[str, str, str]]:
        entries: dict[str, tuple[str, str]] = {}

        def add(surface: str, kind: str, key: str, *, overwrite: bool = True) -> None:
            normalized = normalize_question(surface)
            if not normalized or (not overwrite and normalized in entries):
                return
            entries[normalized] = (kind, key)

        for word in STOP_WORDS:
            add(word, "stop", word)
        for kind, words in CUE_WORDS.items():
            for word in words:
                add(word, "cue", kind)
        for key, entity in self.catalog.entities.items():
            for word in entity.synonyms:
                add(word, "entity", key)
        for word in self.catalog.count_words:
            add(word, "count", "count")
        for word in self.catalog.time_words:
            add(word, "time_word", "time")
        for word in self.catalog.scoped_dimension_words:
            add(word, "scoped", word)
        for key, dimension in self.catalog.dimensions.items():
            for word in dimension.synonyms:
                add(word, "dimension", key)
        for key, metric in self.catalog.metrics.items():
            for word in metric.synonyms:
                add(word, "metric", key)
        for key, flt in self.catalog.filters.items():
            for word in flt.synonyms:
                add(word, "filter", key)
        for key, value_map in self.catalog.value_maps.items():
            for word, code in value_map.surfaces().items():
                add(word, "value", f"{key}={code}")
        add(NAME_TOKEN, "name", "name")
        return sorted(
            ((s, k, key) for s, (k, key) in entries.items()), key=lambda item: -len(item[0])
        )

    # ------------------------------------------------------------------ 主流程

    def parse(self, question: str) -> ParseResult:
        result, slots, detail_name = self._analyze(question)
        if result.reason:
            return result
        try:
            result.plan = self._build_plan(result.matches, slots, detail_name)
        except _Decline as decline:
            result.reason = str(decline)
        return result

    def analyze(self, question: str) -> ParseResult:
        """只做词表与槽位匹配和覆盖检查，不组装计划。多轮追问改写用它比较两轮问题的成分。"""
        result, _, _ = self._analyze(question)
        return result

    def _analyze(self, question: str) -> tuple[ParseResult, list[_Slot], str | None]:
        raw = question or ""
        detail_name, raw_without_name = self._extract_name(raw)
        text = normalize_question(raw_without_name)
        result = ParseResult(question=raw, normalized=text)
        if not text:
            result.reason = "问题为空"
            return result, [], detail_name

        covered = [False] * len(text)
        slots = self._extract_slots(text, covered)
        result.matches = self._match_lexicon(text, covered)
        result.matches.extend(
            Match(slot.kind, str(slot.value), text[slot.start : slot.end], slot.start, slot.end)
            for slot in slots
        )
        result.matches.sort(key=lambda m: m.start)

        leftovers = re.findall(
            r"[一-龥a-z0-9]+", "".join(" " if covered[i] else ch for i, ch in enumerate(text))
        )
        if leftovers:
            result.unexplained = leftovers
            result.reason = f"语义层无法解释：{'、'.join(leftovers)}"
        return result, slots, detail_name

    # ------------------------------------------------------------------ 抽取

    @staticmethod
    def _extract_name(raw: str) -> tuple[str | None, str]:
        for pattern in (_NAME_QUOTED, _NAME_SUFFIXED):
            match = pattern.search(raw)
            if match:
                name = match.group(1).strip()
                if any(word in name for word in ("的", "各", "哪", "统计", "数量", "多少")):
                    continue
                start, end = match.span(1)
                if pattern is _NAME_QUOTED:
                    start, end = match.span()
                return name, raw[:start] + NAME_TOKEN + raw[end:]
        return None, raw

    def _extract_slots(self, text: str, covered: list[bool]) -> list[_Slot]:
        slots: list[_Slot] = []
        anchor = self.catalog.anchor_year

        def take(pattern: re.Pattern[str], kind: str, builder) -> None:
            for match in pattern.finditer(text):
                start, end = match.span()
                if any(covered[start:end]):
                    continue
                value = builder(match)
                if value is None:
                    continue
                for index in range(start, end):
                    covered[index] = True
                slots.append(_Slot(kind, start, end, value))

        take(_YEAR_RANGE, "time", lambda m: ("between", int(m.group(1)), int(m.group(2))))
        take(_YEAR_SINCE, "time", lambda m: (">=", int(m.group(1)), None))
        take(_YEAR_BEFORE, "time", lambda m: ("<", int(m.group(1)), None))
        take(
            _YEAR_RECENT,
            "time",
            lambda m: ("recent", int(m.group(1)), None) if int(m.group(1)) > 0 else None,
        )
        take(
            _YEAR_RELATIVE,
            "time",
            lambda m: (
                "=",
                anchor - {"今年": 0, "本年度": 0, "本年": 0, "去年": 1, "前年": 2}[m.group(0)],
                None,
            ),
        )
        take(_AGE_OVER, "age", lambda m: (">=", int(m.group(1) or m.group(2))))
        take(_AGE_UNDER, "age", lambda m: ("<", int(m.group(1))))
        take(_CAPITAL, "capital", self._capital_value)
        take(
            _ORDER_CAPITAL,
            "order_attr",
            lambda m: ("capital", "desc" if m.group(1) in "最高最大最多" else "asc"),
        )
        take(
            _ORDER_FOUNDED,
            "order_attr",
            lambda m: ("founded", "asc" if m.group(1) in ("最早", "最久") else "desc"),
        )
        take(
            _TOP_AFTER_RANK,
            "top",
            lambda m: (
                "asc" if m.group(1) in ("最少", "最低", "最小") else "desc",
                int(m.group(2)),
            ),
        )
        take(_TOP, "top", lambda m: ("desc", int(m.group(1))) if int(m.group(1)) > 0 else None)
        take(_YEAR_SINGLE, "time", lambda m: ("=", int(m.group(1)), None))
        take(_COUNT_N, "count_n", lambda m: int(m.group(1)) if int(m.group(1)) > 0 else None)
        return slots

    @staticmethod
    def _capital_value(match: re.Match[str]):
        prefix, number, unit, suffix = (
            match.group(1),
            float(match.group(2)),
            match.group(3),
            match.group(4),
        )
        value = number * (10000 if unit == "亿" else 1)
        if prefix in ("超过", "大于", "高于", "多于"):
            op = ">"
        elif prefix in ("不低于", "不少于", "达到") or suffix in ("以上", "及以上"):
            op = ">="
        elif prefix in ("低于", "小于", "少于", "不足", "不到"):
            op = "<"
        elif suffix in ("以下", "及以下", "以内"):
            op = "<="
        elif prefix == "在":
            return None
        else:
            op = "="
        return op, value

    def _match_lexicon(self, text: str, covered: list[bool]) -> list[Match]:
        matches: list[Match] = []
        for surface, kind, key in self._lexicon:
            start = text.find(surface)
            while start != -1:
                end = start + len(surface)
                if not any(covered[start:end]):
                    for index in range(start, end):
                        covered[index] = True
                    matches.append(Match(kind, key, surface, start, end))
                start = text.find(surface, start + 1)
        return matches

    # ------------------------------------------------------------------ 组装

    def _requires_entity(self, requires: tuple[str, ...]) -> set[str]:
        return {
            self.alias_entity[alias]
            for alias in requires
            if alias not in _ALIAS_ENTITY_SKIP
            and self.alias_entity.get(alias) not in (None, "enterprise")
        }

    def _build_plan(
        self, matches: list[Match], slots: list[_Slot], detail_name: str | None
    ) -> QueryPlan:
        catalog = self.catalog
        by_kind: dict[str, list[Match]] = {}
        for match in matches:
            by_kind.setdefault(match.kind, []).append(match)
        cues = {m.key for m in by_kind.get("cue", [])}

        # 1) 事件实体：维度/过滤/值/实体词/专属指标指向的事件表必须唯一
        fact: set[str] = set()
        for match in by_kind.get("entity", []):
            if match.key != "enterprise":
                fact.add(match.key)
        for match in by_kind.get("dimension", []):
            fact |= self._requires_entity(catalog.dimensions[match.key].requires)
        for match in by_kind.get("filter", []):
            fact |= self._requires_entity(catalog.filters[match.key].requires)
        for match in by_kind.get("value", []):
            fact |= self._requires_entity(catalog.value_maps[match.key.split("=", 1)[0]].requires)
        for match in by_kind.get("metric", []):
            defined = set(catalog.metrics[match.key].sql)
            if len(defined) == 1 and "enterprise" not in defined:
                fact |= defined
        if len(fact) > 1:
            labels = "、".join(catalog.entities[e].label for e in sorted(fact))
            raise _Decline(f"问题同时涉及{labels}，语义层不支持跨事件表组合")
        entity_key = next(iter(fact)) if fact else "enterprise"
        entity = catalog.entities[entity_key]
        reachable = {entity.alias, *entity.joins}
        assumptions: list[str] = []
        if entity.assumption:
            assumptions.append(entity.assumption)

        def ensure_reachable(requires: tuple[str, ...], label: str) -> None:
            missing = set(requires) - reachable
            if missing:
                raise _Decline(f"“{label}”无法与{entity.label}组合统计")

        # 2) 指标
        metrics: list[str] = []
        for match in by_kind.get("metric", []):
            metric = catalog.metrics[match.key]
            if metric.sql.get(entity_key) is None:
                raise _Decline(f"“{metric.label}”在{entity.label}下没有定义安全口径")
            if match.key not in metrics:
                metrics.append(match.key)
                if metric.assumption:
                    assumptions.append(metric.assumption)
        if by_kind.get("count") and not metrics:
            metrics.append(entity.default_metric)

        # 3) 维度（含按实体区分含义的词和泛化时间词）
        dimensions: list[str] = []

        def add_dimension(key: str) -> None:
            dimension = catalog.dimensions[key]
            ensure_reachable(dimension.requires, dimension.label)
            if key not in dimensions:
                dimensions.append(key)
                if dimension.assumption:
                    assumptions.append(dimension.assumption)

        for match in by_kind.get("dimension", []):
            add_dimension(match.key)
        for match in by_kind.get("scoped", []):
            resolved = catalog.scoped_dimension(match.key, entity_key)
            if resolved is None:
                raise _Decline(f"“{match.surface}”在{entity.label}下没有对应维度")
            add_dimension(resolved)
        if by_kind.get("time_word"):
            if not entity.time_dimension:
                raise _Decline(f"{entity.label}没有可用于趋势统计的时间字段")
            add_dimension(entity.time_dimension)
        if len(dimensions) > 2:
            raise _Decline("一次最多按两个维度分组")

        # 4) 过滤：语义过滤、值映射、时间、成立年限、注册资本、企业名称
        filters: list[AppliedFilter] = []
        filter_matches = by_kind.get("filter", [])
        time_for_filter: dict[int, str] = {}
        founded_positions = [m.start for m in matches if m.kind == "cue" and m.key == "founded"]

        time_label_for_filter: dict[int, str] = {}
        for slot in (s for s in slots if s.kind == "time"):
            op, first, second = slot.value
            condition, label, note = self._time_condition(op, first, second)
            adjacent = next(
                (
                    m
                    for m in filter_matches
                    if 0 <= m.start - slot.end <= 3 and catalog.filters[m.key].time_column
                ),
                None,
            )
            if adjacent is not None:
                time_for_filter[adjacent.start] = condition
                time_label_for_filter[adjacent.start] = label
                target_label = catalog.filters[adjacent.key].label
                if note:
                    assumptions.append(f"{note}，作用于“{target_label}”的事件时间")
                continue
            if any(0 <= pos - slot.end <= 3 for pos in founded_positions):
                dimension_key = "start_year"
            else:
                dimension_key = entity.time_dimension
            if not dimension_key:
                raise _Decline(f"{entity.label}没有时间字段，无法按“{label}”筛选")
            dimension = catalog.dimensions[dimension_key]
            ensure_reachable(dimension.requires, dimension.label)
            if op == "between":
                key_suffix = f"={min(first, second)}-{max(first, second)}"
            elif op == "recent":
                key_suffix = f">={catalog.anchor_year - first + 1}"
            else:
                key_suffix = f"{op}{first}"
            display = label + ("成立" if dimension_key == "start_year" else "")
            filters.append(
                AppliedFilter(
                    f"{dimension_key}{key_suffix}",
                    display,
                    f"{dimension.sql} {condition}",
                    dimension.requires,
                )
            )
            if note:
                assumptions.append(f"{note}，按{dimension.label}计算")

        for match in filter_matches:
            flt = catalog.filters[match.key]
            ensure_reachable(flt.requires, flt.label)
            label = time_label_for_filter.get(match.start, "") + flt.label
            filters.append(
                AppliedFilter(
                    match.key, label, flt.render(time_for_filter.get(match.start)), flt.requires
                )
            )
            if flt.assumption:
                assumptions.append(flt.assumption)

        grouped_values: dict[str, list[str]] = {}
        for match in by_kind.get("value", []):
            map_key, code = match.key.split("=", 1)
            codes = grouped_values.setdefault(map_key, [])
            if code not in codes:
                codes.append(code)
        for map_key, codes in grouped_values.items():
            value_map = catalog.value_maps[map_key]
            ensure_reachable(value_map.requires, value_map.label)
            conditions = [value_map.condition(code) for code in codes]
            sql = conditions[0] if len(conditions) == 1 else "(" + " OR ".join(conditions) + ")"
            label = f"{value_map.label}为{'或'.join(value_map.label_of(c) for c in codes)}"
            filters.append(
                AppliedFilter(f"{map_key}={'|'.join(codes)}", label, sql, value_map.requires)
            )
            if value_map.assumption:
                assumptions.append(value_map.assumption)

        for slot in (s for s in slots if s.kind == "age"):
            ensure_reachable(("e",), "成立年限")
            op, years = slot.value
            boundary = catalog.anchor_year - years
            if op == ">=":
                filters.append(
                    AppliedFilter(
                        f"age>={years}",
                        f"成立超过{years}年",
                        f"YEAR(e.`start_date`) <= {boundary}",
                        ("e",),
                    )
                )
                assumptions.append(f"“成立超过{years}年”按成立年份不晚于 {boundary} 年计算")
            else:
                filters.append(
                    AppliedFilter(
                        f"age<{years}",
                        f"成立不足{years}年",
                        f"YEAR(e.`start_date`) > {boundary}",
                        ("e",),
                    )
                )
                assumptions.append(f"“成立不足{years}年”按成立年份晚于 {boundary} 年计算")

        for slot in (s for s in slots if s.kind == "capital"):
            ensure_reachable(("e",), "注册资本")
            op, value = slot.value
            number = _format_number(value)
            filters.append(
                AppliedFilter(
                    f"capital{op}{number}",
                    f"注册资本{op}{number}万元",
                    f"e.`regist_capi_new` {op} {number}",
                    ("e",),
                )
            )
            assumptions.append("注册资本单位为万元")

        if detail_name and (metrics or dimensions or filters or entity_key != "enterprise"):
            ensure_reachable(("e",), "企业名称")
            pattern = sql_string(f"%{_escape_like(detail_name)}%")
            filters.append(
                AppliedFilter(
                    f"name~{detail_name}",
                    f"企业名称包含“{detail_name}”",
                    f"e.`name` LIKE {pattern}",
                    ("e",),
                )
            )

        # 5) 排序与数量
        top_slot = next((s for s in slots if s.kind == "top"), None)
        count_slot = next((s for s in slots if s.kind == "count_n"), None)
        order_attr = next((s for s in slots if s.kind == "order_attr"), None)
        rank_direction = None
        if top_slot:
            rank_direction = top_slot.value[0]
        elif "rank_desc" in cues or "which_one" in cues:
            rank_direction = "desc"
        elif "rank_asc" in cues:
            rank_direction = "asc"
        elif "rank" in cues:
            rank_direction = "desc"
        if "rank_asc" in cues and "which_one" in cues:
            rank_direction = "asc"

        limit = None
        if top_slot:
            limit = top_slot.value[1]
        elif count_slot:
            limit = count_slot.value
        if "which_one" in cues:
            limit = 1

        # 6) 模式
        explicit_company = any(m.key == "enterprise" for m in by_kind.get("entity", []))
        mentions_company = explicit_company or "which_one" in cues or "which_many" in cues
        if "detail" in cues and not detail_name:
            raise _Decline("询问企业详情时需要给出企业名称，例如：查询“某某科技有限公司”的基本信息")
        # “每个城市招投标最多的企业”是分组内取排名，需要窗口函数；按全局排名回答会给出看似合理的错误结果
        if dimensions and (
            order_attr is not None
            or (rank_direction and explicit_company and entity_key != "enterprise")
        ):
            raise _Decline(
                "按分组分别取排名靠前的企业需要窗口函数，语义层不支持，需交给模型生成 SQL"
            )

        if detail_name and not (metrics or dimensions or filters) and entity_key == "enterprise":
            mode = "detail"
        elif order_attr is not None:
            # “注册资本最高的10家企业中有多少家存续”：名单不输出指标，照常出名单等于把计数丢掉
            if metrics:
                raise _Decline(_NESTED_COUNT)
            mode = "list"
        elif rank_direction and not dimensions and entity_key != "enterprise" and mentions_company:
            dimensions = ["company"]
            mode = "group"
        elif dimensions:
            mode = "group"
        elif metrics:
            if rank_direction:
                raise _Decline("没有识别到排名所依据的分组维度")
            mode = "aggregate"
        elif "list" in cues or "which_many" in cues or filters:
            # 只有实体词、既没有筛选条件也没有“名单/哪些”时，意图不明确，不猜
            mode = "list"
        else:
            raise _Decline("没有识别到要统计的指标或维度")

        # “招投标最多的10家企业中有多少家获得过融资”：按企业分组后每组企业数恒为 1，
        # 出现这种组合说明问题要在排名集合里再计数，照常执行会先过滤再排名，结果看似合理但是错的
        if mode == "group" and "company" in dimensions and "enterprise_count" in metrics:
            raise _Decline(_NESTED_COUNT)
        if mode == "group" and not metrics:
            metrics = [entity.default_metric]

        plan = QueryPlan(
            entity=entity_key,
            mode=mode,
            metrics=metrics,
            dimensions=dimensions,
            filters=filters,
            detail_name=detail_name if mode == "detail" else None,
        )

        if mode == "group":
            if limit is not None or rank_direction:
                plan.order = rank_direction or "desc"
                plan.limit = limit if limit is not None else catalog.default_top_limit
                if limit is None:
                    assumptions.append(f"未指定数量，默认返回前 {catalog.default_top_limit} 名")
            elif any(catalog.dimensions[d].time for d in dimensions):
                plan.order = "time"
            elif any(catalog.dimensions[d].order_sql for d in dimensions):
                plan.order = "dimension"
            else:
                plan.order = "desc"
            if "share" in cues:
                for key in metrics:
                    expression = catalog.metric_sql(key, entity_key) or ""
                    if not re.match(r"^(ROUND\()?(COUNT|SUM)\(", expression):
                        raise _Decline(
                            f"“{catalog.metrics[key].label}”不是可加总的指标，不能计算占比"
                        )
                plan.share = True
        elif mode == "list":
            ensure_reachable(entity.list_requires, f"{entity.label}名单")
            plan.limit = limit if limit is not None else catalog.default_list_limit
            if order_attr is not None:
                attribute, direction = order_attr.value
                ensure_reachable(("e",), "排序字段")
                column = "e.`regist_capi_new`" if attribute == "capital" else "e.`start_date`"
                plan.order = direction
                plan.order_sql = f"{column} {direction.upper()}, e.`name`"
                if attribute == "capital":
                    plan.order_label = (
                        "按注册资本从高到低" if direction == "desc" else "按注册资本从低到高"
                    )
                else:
                    plan.order_label = (
                        "按成立时间从早到晚" if direction == "asc" else "按成立时间从晚到早"
                    )
            elif entity.list_order_note:
                assumptions.append(entity.list_order_note)
            if limit is None:
                assumptions.append(f"未指定数量，名单默认返回 {catalog.default_list_limit} 条")
        elif mode == "aggregate" and "share" in cues:
            raise _Decline("计算占比需要分组维度")

        plan.assumptions = list(dict.fromkeys(assumptions))
        return plan

    def _time_condition(
        self, op: str, first: int, second: int | None
    ) -> tuple[str, str, str | None]:
        anchor = self.catalog.anchor_year
        if op == "between":
            low, high = sorted((first, second or first))
            return f"BETWEEN {low} AND {high}", f"{low}–{high}年", f"“{low}到{high}年”包含首尾两年"
        if op == ">=":
            return f">= {first}", f"{first}年以来", f"“{first}年以来”包含 {first} 年"
        if op == "<":
            return f"< {first}", f"{first}年之前", f"“{first}年之前”不含 {first} 年"
        if op == "recent":
            start = anchor - first + 1
            return (
                f">= {start}",
                f"近{first}年（{start}–{anchor}）",
                f"“近{first}年”按 {start}–{anchor} 年统计（数据截至 {anchor} 年）",
            )
        return f"= {first}", f"{first}年", None


class _Decline(Exception):
    """语义层无法安全表达该问题。"""
