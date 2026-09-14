"""多轮追问：把省略式追问改写成完整问题，再交给解析器重新走一遍。

“广州市存续企业有多少家” → “那深圳呢” → “深圳存续企业有多少家”。

不直接在上一轮的 SQL 或查询计划上打补丁：改写后的问题会重新经过覆盖检查和编译，
解析不了就放弃，由 LLM 路径带着对话上下文处理。改写结果会展示给用户（“已理解为……”），
理解错了一眼就能看出来。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from text2sql.semantic.parser import Match, SemanticParser, normalize_question

_FOLLOW_UP_START = re.compile(
    r"^(那么|那|换成|改成|改为|如果是|只看|再看|再按|去掉|不限|不看|取消)"
)
_FOLLOW_UP_END = re.compile(r"呢$")
_REMOVAL = re.compile(r"^(去掉|不限|不看|取消|不要)(.+?)(的?(条件|限制|筛选|过滤))?$")
_LOCATION_MAPS = {"city", "district"}
_INDUSTRY_MAPS = {"industry_section", "industry_division"}
_ANCHOR_KINDS = {"entity", "metric", "count"}
_GROUP_PREFIXES = ("各个", "每个", "不同", "各")
_DIMENSION_CUES = ("按照", "按", *_GROUP_PREFIXES)


@dataclass
class FollowUpRewrite:
    question: str
    previous: str
    effective_question: str
    operations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def component_group(match: Match, parser: SemanticParser) -> str | None:
    """同组成分互相替换：地区换地区、维度换维度、时间换时间。"""
    kind = match.kind
    if kind == "value":
        map_key = match.key.split("=", 1)[0]
        if map_key in _LOCATION_MAPS:
            return "location"
        if map_key in _INDUSTRY_MAPS:
            return "industry"
        return f"value:{map_key}"
    if kind == "filter":
        return "filter:" + parser.catalog.filters[match.key].group
    if kind in ("dimension", "scoped", "time_word"):
        return "dimension"
    if kind in ("metric", "count"):
        return "metric"
    if kind == "time":
        return "time"
    if kind in ("top", "count_n"):
        return "top"
    if kind in ("capital", "age", "order_attr", "entity"):
        return kind
    return None


def has_follow_up_cue(question: str) -> bool:
    """带“那……呢”“换成……”这类明确追问语气，没有上一轮时不应独立作答。"""
    text = normalize_question(question)
    return bool(text) and bool(_FOLLOW_UP_START.search(text) or _FOLLOW_UP_END.search(text))


def looks_like_follow_up(question: str, parser: SemanticParser) -> bool:
    text = normalize_question(question)
    if not text:
        return False
    if has_follow_up_cue(question):
        return True
    if parser.parse(question).ok:
        return False
    analysis = parser.analyze(question)
    return (
        len(text) <= 12
        and not analysis.unexplained
        and any(component_group(m, parser) for m in analysis.matches)
    )


def _grouped(matches: list[Match], parser: SemanticParser) -> list[tuple[str, Match]]:
    return [(group, m) for m in matches if (group := component_group(m, parser))]


def _insert(text: str, group: str, match: Match, previous: list[tuple[str, Match]]) -> str:
    if group == "dimension":
        return f"按{match.surface}{text}"
    if group == "time":
        return f"{match.surface}{text}"
    if group == "top":
        return f"{text}{match.surface}"
    anchor = next((m for g, m in previous if m.kind in _ANCHOR_KINDS), None)
    position = text.find(anchor.surface) if anchor else -1
    if position >= 0:
        return text[:position] + match.surface + text[position:]
    return match.surface + text


def rewrite_follow_up(
    question: str, previous: str, parser: SemanticParser
) -> FollowUpRewrite | None:
    text = normalize_question(question)
    effective = normalize_question(previous)
    if not text or not effective:
        return None

    removal = _REMOVAL.match(text)
    current = parser.analyze(removal.group(2) if removal else question)
    if current.unexplained:
        return None
    new_items = [(g, m) for g, m in _grouped(current.matches, parser) if g != "entity"]
    if not new_items:
        return None
    old_items = _grouped(parser.analyze(previous).matches, parser)

    operations: list[str] = []
    if removal:
        for group, _ in new_items:
            target = next((m for g, m in old_items if g == group), None)
            if target is None:
                return None
            effective = effective.replace(target.surface, "", 1)
            operations.append(f"去掉「{target.surface}」")
        return FollowUpRewrite(question, previous, effective, operations)

    for group, match in new_items:
        target = next((m for g, m in old_items if g == group), None)
        if target is not None:
            if target.surface == match.surface:
                continue
            replacement = match.surface
            # “各城市”整体是一个词，换成“年份”时保留“各”，否则分组语气丢失
            prefix = next((p for p in _GROUP_PREFIXES if target.surface.startswith(p)), "")
            if prefix and not replacement.startswith(_GROUP_PREFIXES):
                replacement = prefix + replacement
            # 反过来，前文已经有“各”而新词自带“各城市”时去掉一个，避免拼成“各各城市”
            before = effective[: effective.find(target.surface)]
            own = next((p for p in _GROUP_PREFIXES if replacement.startswith(p)), "")
            if own and before.endswith(_GROUP_PREFIXES):
                replacement = replacement[len(own) :]
            elif group == "dimension" and not own and not before.endswith(_DIMENSION_CUES):
                # “每年”换成“行业”时补上“按”，否则读成“近3年行业的融资事件数”
                replacement = "按" + replacement
            effective = effective.replace(target.surface, replacement, 1)
            operations.append(f"把「{target.surface}」换成「{replacement}」")
        else:
            effective = _insert(effective, group, match, old_items)
            operations.append(f"增加「{match.surface}」")
    if not operations:
        return None
    return FollowUpRewrite(question, previous, effective, operations)
