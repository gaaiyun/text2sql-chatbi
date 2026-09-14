"""把解析过程翻译成人能读懂的说明：词语匹配、查询计划、编译出的 SQL，或者放弃的原因。

解析调试台（Streamlit 页面与网页）共用。匹配结果带字符位置，页面可以把问题原文逐段标注，
直观看到“覆盖检查”——每个字要么被语义层解释，要么是功能词，剩下的就是放弃的理由。
"""

from __future__ import annotations

import ast
from typing import Any

from text2sql.semantic.catalog import SemanticCatalog
from text2sql.semantic.compiler import compile_plan
from text2sql.semantic.parser import Match, SemanticParser
from text2sql.sql.guard import SQLGuard

KIND_LABELS = {
    "entity": "实体",
    "metric": "指标",
    "count": "计数",
    "dimension": "维度",
    "scoped": "维度",
    "filter": "筛选",
    "value": "取值",
    "cue": "句式",
    "time_word": "时间维度",
    "name": "企业名",
    "time": "时间范围",
    "age": "成立年限",
    "capital": "注册资本",
    "order_attr": "排序",
    "top": "前 N 名",
    "count_n": "数量",
    "stop": "功能词",
}
_TIME_TEMPLATES = {
    "between": "{0}–{1} 年",
    ">=": "{0} 年及以后",
    "<": "{0} 年以前",
    "recent": "最近 {0} 年",
    "=": "{0} 年",
}


def describe_match(match: Match, catalog: SemanticCatalog) -> str:
    """键名换成语义层里的中文名，槽位值换成可读的条件。"""
    kind, key = match.kind, match.key
    sections: dict[str, dict[str, Any]] = {
        "entity": catalog.entities,
        "metric": catalog.metrics,
        "dimension": catalog.dimensions,
        "filter": catalog.filters,
    }
    if kind in sections and key in sections[kind]:
        return sections[kind][key].label
    if kind == "value":
        map_key, code = key.split("=", 1)
        value_map = catalog.value_maps[map_key]
        return f"{value_map.label} = {value_map.label_of(code)}（{code}）"
    if kind in ("stop", "cue", "count", "scoped", "time_word", "name"):
        return match.surface if kind == "stop" else key
    try:
        value = ast.literal_eval(key)
    except (ValueError, SyntaxError):
        return key
    if kind == "time":
        op, first, second = value
        return _TIME_TEMPLATES.get(op, "{0}").format(first, second)
    if kind == "top":
        return f"{'前' if value[0] == 'desc' else '后'} {value[1]} 名"
    if kind == "count_n":
        return f"{value} 个"
    if kind == "order_attr":
        attribute = "注册资本" if value[0] == "capital" else "成立时间"
        return f"按{attribute}{'从高到低' if value[1] == 'desc' else '从低到高'}"
    if kind == "age":
        return f"成立年限 {value[0]} {value[1]} 年"
    if kind == "capital":
        return f"注册资本 {value[0]} {value[1]:g} 万元"
    return key


def explain_question(
    question: str,
    *,
    parser: SemanticParser,
    catalog: SemanticCatalog,
    guard: SQLGuard,
    max_rows: int = 500,
) -> dict[str, Any]:
    result = parser.parse(question)
    payload: dict[str, Any] = {
        "question": question,
        "normalized": result.normalized,
        "matches": [
            {
                "surface": m.surface,
                "kind": m.kind,
                "kind_label": KIND_LABELS.get(m.kind, m.kind),
                "meaning": describe_match(m, catalog),
                "start": m.start,
                "end": m.end,
            }
            for m in result.matches
        ],
        "unexplained": list(result.unexplained),
        "declined": None,
        "plan": None,
        "description": None,
        "assumptions": [],
        "sql": None,
        "guard": None,
    }
    if result.plan is None:
        payload["declined"] = result.reason or "语义层无法解释这个问题"
        return payload

    compiled = compile_plan(result.plan, catalog, max_rows=max_rows)
    report = guard.check(compiled.sql)
    payload.update(
        plan=result.plan.to_dict(),
        description=compiled.description,
        assumptions=list(compiled.assumptions),
        sql=report.safe_sql or compiled.sql,
        guard={
            "is_safe": report.is_safe,
            "errors": list(report.errors),
            "modifications": list(report.modifications),
            "tables": list(report.referenced_tables),
        },
    )
    return payload
