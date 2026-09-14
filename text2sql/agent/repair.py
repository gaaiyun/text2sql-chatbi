"""失败诊断：把安全门拒绝、执行错误、代价超限和可疑空结果翻译成模型能据以修正的反馈。

只有“模型写错了”的问题才回送修复；触碰安全策略的请求（写操作、敏感列、危险函数、跨库）
直接终止，不给模型第二次尝试绕过的机会。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from text2sql.db.backends import BackendError, CostEstimate, QueryTimeoutError
from text2sql.db.schema import PhysicalSchema
from text2sql.sql.guard import GuardReport

NON_REPAIRABLE_GUARD_CODES = frozenset(
    {
        "not_select",
        "forbidden_node",
        "forbidden_function",
        "cross_database",
        "sensitive_column",
        "too_long",
        "empty",
    }
)

_UNKNOWN_COLUMN = (
    re.compile(r'Referenced column "([^"]+)" not found'),
    re.compile(r"Unknown column '([^']+)'"),
    re.compile(r'column "([^"]+)" does not exist', re.IGNORECASE),
)
_UNKNOWN_TABLE = (
    re.compile(r"Table with name ([^\s!]+) does not exist"),
    re.compile(r"Table '([^']+)' doesn't exist"),
)


@dataclass
class Diagnosis:
    code: str
    message: str
    hints: list[str] = field(default_factory=list)
    repairable: bool = True

    def feedback(self, sql: str) -> str:
        lines = ["上一次提交的 SQL 没有通过：", "```sql", sql, "```", f"原因：{self.message}"]
        if self.hints:
            lines.append("线索：")
            lines.extend(f"- {hint}" for hint in self.hints)
        lines.append("请修正后重新提交。")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def diagnose_guard(report: GuardReport) -> Diagnosis:
    return Diagnosis(
        code=report.error_code or "guard_rejected",
        message="；".join(report.errors) or "SQL 未通过安全检查",
        hints=list(report.hints),
        repairable=report.error_code not in NON_REPAIRABLE_GUARD_CODES,
    )


def diagnose_execution_error(exc: Exception, sql: str) -> Diagnosis:
    message = str(exc)
    if isinstance(exc, QueryTimeoutError):
        return Diagnosis(
            "timeout",
            message,
            [
                "先在子查询中按 eid 聚合再关联，缩小中间结果",
                "为大表增加时间或地区等过滤条件",
                "检查是否遗漏关联条件导致笛卡尔积",
            ],
        )
    for pattern in _UNKNOWN_COLUMN:
        match = pattern.search(message)
        if match:
            return Diagnosis(
                "execution_unknown_column",
                f"数据库报告字段 {match.group(1)} 不存在",
                ["只能使用表结构中列出的字段"],
            )
    for pattern in _UNKNOWN_TABLE:
        match = pattern.search(message)
        if match:
            return Diagnosis(
                "execution_unknown_table",
                f"数据库报告表 {match.group(1)} 不存在",
                ["只能使用相关表中列出的表或视图"],
            )
    lowered = message.lower()
    if (
        "conversion error" in lowered
        or "incorrect" in lowered
        or ("type" in lowered and "mismatch" in lowered)
    ):
        return Diagnosis(
            "type_error",
            message[:400],
            [
                "代码类字段（如 area_code、district_code）是字符串，比较时加引号",
                "年份等数值不要与字符串直接比较",
            ],
        )
    if "syntax" in lowered or "parser error" in lowered:
        return Diagnosis("syntax_error", message[:400], ["检查括号、逗号与反引号是否成对"])
    if isinstance(exc, BackendError) and "无法连接" in message:
        return Diagnosis("connection_error", message, [], repairable=False)
    return Diagnosis("execution_error", message[:400])


def diagnose_cost(estimate: CostEstimate, limit: int) -> Diagnosis | None:
    if estimate.max_cardinality is None or estimate.max_cardinality <= limit:
        return None
    return Diagnosis(
        "cost_exceeded",
        f"执行计划预计处理约 {estimate.max_cardinality:,} 行，超过上限 {limit:,} 行",
        [
            "多表关联前先在子查询中按 eid 聚合",
            "为排序和关联增加过滤条件",
            "检查是否遗漏关联条件导致笛卡尔积",
        ],
    )


_COUNT_QUESTION = re.compile(r"(多少家|多少个|多少条|多少项|多少人|几家|几个|总数|数量)")


def diagnose_count_per_group(question: str, task: str | None, result: Any) -> Diagnosis | None:
    """问“有多少家”却返回多行、每行都是 1：计数写在了 GROUP BY 里面，而不是对分组结果再计数。"""
    if task != "aggregate" or not _COUNT_QUESTION.search(question or ""):
        return None
    if result.row_count <= 1 or len(result.columns) != 1:
        return None
    values = [row.get(result.columns[0]) for row in result.rows]
    if not all(value == 1 for value in values):
        return None
    return Diagnosis(
        "count_per_group",
        f"问题问的是总数，但结果按分组返回了 {result.row_count} 行、每行都是 1",
        ["把按分组筛选的查询放进子查询，外层再 SELECT COUNT(*)，只返回一行"],
    )


def _literal_values(node: exp.Expression) -> list[Any]:
    return [node.this] if isinstance(node, exp.Literal) else []


def _comparisons(tree: exp.Expression):
    for node in tree.find_all(exp.EQ, exp.Like, exp.In):
        if isinstance(node, exp.In):
            column = node.this
            literals = [v for item in node.expressions for v in _literal_values(item)]
        else:
            left, right = node.left, node.right
            column, literal_node = (left, right) if isinstance(left, exp.Column) else (right, left)
            literals = _literal_values(literal_node)
        if isinstance(column, exp.Column) and literals:
            yield node, column, literals


def diagnose_empty_result(sql: str, values: Any, schema: PhysicalSchema) -> Diagnosis | None:
    """结果为 0 行且过滤条件里的字面值不在列的真实取值中 → 很可能是值写错了，而不是真的没有数据。"""
    if values is None:
        return None
    try:
        tree = sqlglot.parse_one(sql, read="mysql")
    except SqlglotError:
        return None

    sources = {t.alias_or_name: t.name for t in tree.find_all(exp.Table)}
    physical = list(dict.fromkeys(sources.values()))
    problems: list[str] = []
    hints: list[str] = []
    for node, column, literals in _comparisons(tree):
        if column.table:
            table = sources.get(column.table)
        else:
            owners = [t for t in physical if (d := schema.get(t)) and d.has_column(column.name)]
            table = owners[0] if len(owners) == 1 else None
        if not table:
            continue
        entry = values.get(table, column.name)
        if entry is None:
            continue
        for literal in literals:
            text = str(literal)
            verdict = (
                values.matches_like(table, column.name, text)
                if isinstance(node, exp.Like)
                else values.contains(table, column.name, text)
            )
            if verdict is not False:
                continue
            operator = "LIKE" if isinstance(node, exp.Like) else "="
            problems.append(f"{table}.{column.name} {operator} '{text}'")
            real = [str(v) for v, _ in entry.values]
            hints.append(
                f"{table}.{column.name} 的真实取值包括：" + "、".join(f"'{v}'" for v in real[:8])
            )
            core = text.strip("%")
            containing = [v for v in real if core and core in v]
            if containing and not isinstance(node, exp.Like):
                hints.append(
                    f"真实取值是长文本，可以改用 {column.name} LIKE '{core}%' 或 LIKE '%{core}%'"
                )
            close = difflib.get_close_matches(core, real, n=3, cutoff=0.3)
            if close:
                hints.append("可能想用：" + "、".join(f"'{v}'" for v in close))
    if not problems:
        return None
    return Diagnosis(
        "suspicious_empty",
        "查询执行成功但结果为空（0 行或计数为 0），且以下条件的取值在库中不存在："
        + "；".join(problems),
        list(dict.fromkeys(hints)),
    )
