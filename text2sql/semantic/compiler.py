"""查询计划编译器：把 QueryPlan 编译为 MySQL 方言 SQL。

编译器只做机械翻译，口径全部来自语义层：关联路径由实体的 joins 决定，
事件条件（如宽表占位行）已经封装在视图和过滤表达式里，所以编译结果不会出现“忘了过滤”的错误。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from text2sql.semantic.catalog import SemanticCatalog
from text2sql.semantic.parser import QueryPlan

_PLAIN_COLUMN = re.compile(r"^[a-z]+\.`[^`]+`$")


@dataclass
class CompiledQuery:
    sql: str
    columns: list[str]
    assumptions: list[str]
    description: str


def _alias(label: str) -> str:
    return "`" + label.replace("`", "``") + "`"


def _render(
    select: list[tuple[str, str]],
    source: str,
    joins: list[str],
    where: list[str],
    group: list[str],
    order: list[str],
    limit: int | None,
) -> str:
    lines = ["SELECT"]
    lines.extend(
        f"  {expr} AS {_alias(label)}{',' if i < len(select) - 1 else ''}"
        for i, (expr, label) in enumerate(select)
    )
    lines.append(f"FROM {source}")
    lines.extend(joins)
    if where:
        lines.append("WHERE " + "\n  AND ".join(where))
    if group:
        lines.append("GROUP BY " + ", ".join(group))
    if order:
        lines.append("ORDER BY " + ", ".join(order))
    if limit is not None:
        lines.append(f"LIMIT {limit}")
    return "\n".join(lines)


def describe_plan(plan: QueryPlan, catalog: SemanticCatalog) -> str:
    entity = catalog.entities[plan.entity]
    if plan.mode == "detail":
        return f"查询名称包含“{plan.detail_name}”的企业的主档信息，以及融资、对外投资、招投标、资质记录数"
    conditions = "、".join(f.label for f in plan.filters)
    scope = f"{conditions}的{entity.label}" if conditions else entity.label
    if plan.mode == "list":
        parts = [f"列出{scope}"]
        if plan.order_label:
            parts.append(plan.order_label)
        parts.append(f"最多 {plan.limit} 条")
        return "，".join(parts)
    metrics = "、".join(catalog.metrics[m].label_for(plan.entity) for m in plan.metrics)
    if plan.mode == "aggregate":
        return f"统计{scope}：{metrics}"
    dimensions = "、".join(catalog.dimensions[d].label for d in plan.dimensions)
    text = f"按{dimensions}统计{scope}：{metrics}"
    if plan.share:
        text += "及占比"
    if plan.limit and plan.order in ("desc", "asc"):
        text += f"，取{'前' if plan.order == 'desc' else '后'} {plan.limit} 名"
    return text


def compile_plan(
    plan: QueryPlan, catalog: SemanticCatalog, *, max_rows: int = 500
) -> CompiledQuery:
    entity = catalog.entities[plan.entity]
    required: set[str] = set()
    for flt in plan.filters:
        required.update(flt.requires)
    where = [flt.sql for flt in plan.filters]
    select: list[tuple[str, str]] = []
    group: list[str] = []
    order: list[str] = []
    limit: int | None

    if plan.mode == "detail":
        select = [(c.sql, c.label) for c in catalog.detail_columns]
        required.update(catalog.detail_requires)
        pattern = plan.detail_name or ""
        escaped = (
            pattern.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_").replace("'", "''")
        )
        where.insert(0, f"e.`name` LIKE '%{escaped}%'")
        order = [catalog.detail_order] if catalog.detail_order else []
        limit = min(catalog.detail_limit, max_rows)
    elif plan.mode == "list":
        select = [(c.sql, c.label) for c in entity.list_columns]
        required.update(entity.list_requires)
        order = [plan.order_sql or entity.list_order]
        limit = min(plan.limit or catalog.default_list_limit, max_rows)
    else:
        for key in plan.dimensions:
            dimension = catalog.dimensions[key]
            select.append((dimension.sql, dimension.label))
            required.update(dimension.requires)
            if dimension.group_sql:
                group.append(dimension.group_sql)
            elif _PLAIN_COLUMN.match(dimension.sql):
                group.append(dimension.sql)
            else:
                group.append(_alias(dimension.label))
        metric_columns = []
        for key in plan.metrics:
            expression = catalog.metric_sql(key, plan.entity)
            label = catalog.metrics[key].label_for(plan.entity)
            select.append((expression, label))
            metric_columns.append((expression, label))
        if plan.share and metric_columns:
            expression, _ = metric_columns[0]
            select.append(
                (f"ROUND(100.0 * {expression} / SUM({expression}) OVER (), 2)", "占比（%）")
            )

        dimension_labels = [catalog.dimensions[d].label for d in plan.dimensions]
        if plan.order in ("desc", "asc") and metric_columns:
            order.append(f"{_alias(metric_columns[0][1])} {plan.order.upper()}")
            order.extend(f"{_alias(label)} ASC" for label in dimension_labels)
        elif plan.order == "time":
            order.extend(f"{_alias(label)} ASC" for label in dimension_labels)
        elif plan.order == "dimension":
            for key in plan.dimensions:
                dimension = catalog.dimensions[key]
                order.append(f"{dimension.order_sql or _alias(dimension.label)} ASC")
        limit = None if plan.mode == "aggregate" else min(plan.limit or max_rows, max_rows)

    required.discard(entity.alias)
    unknown = required - set(entity.joins)
    if unknown:
        raise ValueError(f"实体 {plan.entity} 无法关联别名：{sorted(unknown)}")
    joins = [join for alias, join in entity.joins.items() if alias in required]
    sql = _render(select, f"`{entity.table}` {entity.alias}", joins, where, group, order, limit)
    return CompiledQuery(
        sql=sql,
        columns=[label for _, label in select],
        assumptions=list(plan.assumptions),
        description=describe_plan(plan, catalog),
    )
