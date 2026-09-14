"""SQL 智能体的工具箱。

五个只读工具 + submit_sql。每个工具自行校验参数、从不抛异常，把错误作为结构化结果返回给模型；
preview_sql 与最终执行走同一道安全门、同一个代价估算和同一套空结果诊断，
模型在试运行阶段就能看到“返回 0 行，且取值不存在”这类线索，从而自己修正。
"""

from __future__ import annotations

import difflib
import inspect
from dataclasses import dataclass
from typing import Any

from text2sql.agent.linking import LinkedTable, SchemaLink, SchemaLinker
from text2sql.agent.repair import diagnose_cost, diagnose_empty_result, diagnose_execution_error
from text2sql.db.backends import BackendError, QueryResult
from text2sql.db.schema import PhysicalSchema
from text2sql.semantic.catalog import SemanticCatalog, sql_string
from text2sql.sql.guard import SQLGuard


def _function(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOL_SPECS: list[dict[str, Any]] = [
    _function(
        "search_schema",
        "按关键词检索与问题相关的表和字段，返回表名、说明和命中原因。",
        {"keywords": {"type": "string", "description": "中文关键词，如“融资轮次 企业名称”"}},
        ["keywords"],
    ),
    _function(
        "describe_table",
        "查看一张表或视图的粒度、关联关系、字段类型、口径说明和真实取值。",
        {"table": {"type": "string", "description": "表或视图名，如 招投标"}},
        ["table"],
    ),
    _function(
        "get_column_values",
        "查看字段的真实取值及出现次数，可用关键词过滤。写 WHERE 字符串条件前先确认取值。",
        {
            "table": {"type": "string"},
            "column": {"type": "string"},
            "keyword": {"type": "string", "description": "可选，只返回包含该关键词的取值"},
        },
        ["table", "column"],
    ),
    _function(
        "validate_sql",
        "检查 SQL 是否安全、表和字段是否存在，不执行。返回错误与修复线索。",
        {"sql": {"type": "string"}},
        ["sql"],
    ),
    _function(
        "preview_sql",
        "执行 SQL，返回总行数、每一列的取值概况（文本列的不同取值、数值列的最小最大值）和前 5 行，"
        "用于确认条件确实命中数据。返回 0 行时会附带取值诊断。确认无误后直接提交，不要重复试运行同一条 SQL。",
        {"sql": {"type": "string"}},
        ["sql"],
    ),
    _function(
        "submit_sql",
        "提交最终 SQL 与统计口径说明。调用后本轮结束。",
        {
            "sql": {"type": "string", "description": "最终的单条 SELECT 语句"},
            "assumptions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "中文口径说明，每条一句",
            },
        },
        ["sql"],
    ),
]


@dataclass
class ToolResult:
    ok: bool
    payload: dict[str, Any]
    summary: str


def column_summary(
    columns: list[str], rows: list[dict[str, Any]], *, limit: int = 8
) -> dict[str, dict[str, Any]]:
    """试运行结果的逐列概况。只看前几行时，模型会为了确认“印度的数据在不在”反复试运行；有了概况一次就能确认。"""
    summary: dict[str, dict[str, Any]] = {}
    for column in columns:
        values = [row.get(column) for row in rows]
        present = [v for v in values if v is not None]
        entry: dict[str, Any] = {"nulls": len(values) - len(present)}
        if present and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in present
        ):
            entry.update(min=min(present), max=max(present))
        else:
            distinct = list(dict.fromkeys(str(v) for v in present))
            entry.update(distinct=len(distinct), values=distinct[:limit])
        summary[column] = entry
    return summary


def is_effectively_empty(result: QueryResult) -> bool:
    """0 行，或只有一行且所有值都是 0 / 空（COUNT、SUM 在没有命中时的形态）。"""
    if result.row_count == 0:
        return True
    if result.row_count == 1:
        return all(value in (None, 0, 0.0) for value in result.rows[0].values())
    return False


class AgentToolbox:
    names = ("search_schema", "describe_table", "get_column_values", "validate_sql", "preview_sql")

    def __init__(
        self,
        *,
        catalog: SemanticCatalog,
        schema: PhysicalSchema,
        linker: SchemaLinker,
        guard: SQLGuard,
        backend: Any,
        values: Any | None = None,
        cost_limit: int = 50_000_000,
        preview_rows: int = 5,
        profile_rows: int = 500,
    ) -> None:
        self.catalog = catalog
        self.schema = schema
        self.linker = linker
        self.guard = guard
        self.backend = backend
        self.values = values
        self.cost_limit = cost_limit
        self.preview_rows = preview_rows
        self.profile_rows = max(profile_rows, preview_rows)

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name not in self.names:
            return ToolResult(False, {"error": f"未知工具：{name}"}, f"调用了不存在的工具 {name}")
        handler = getattr(self, f"_{name}")
        accepted = set(inspect.signature(handler).parameters)
        clean = {k: v for k, v in (arguments or {}).items() if k in accepted}
        try:
            return handler(**clean)
        except TypeError as exc:
            return ToolResult(False, {"error": f"参数错误：{exc}"}, f"{name} 参数错误")
        except Exception as exc:  # noqa: BLE001 - 工具失败要回给模型，而不是中断整个流程
            return ToolResult(False, {"error": f"{type(exc).__name__}: {exc}"}, f"{name} 执行失败")

    # ------------------------------------------------------------------ 工具实现

    def _search_schema(self, keywords: str) -> ToolResult:
        link = self.linker.link(str(keywords), top_k=4)
        tables = [
            {
                "name": t.name,
                "label": self.catalog.tables[t.name].label,
                "description": self.catalog.tables[t.name].description,
                "reasons": t.reasons[:4],
            }
            for t in link.tables
        ]
        return ToolResult(
            True, {"tables": tables}, f"找到相关表：{'、'.join(t['name'] for t in tables)}"
        )

    def _describe_table(self, table: str) -> ToolResult:
        name = str(table).strip().strip('`"')
        if name not in self.catalog.tables:
            return ToolResult(
                False,
                {"error": f"表 {name} 不存在", "available": list(self.catalog.tables)},
                f"表 {name} 不存在",
            )
        text = self.linker.render(SchemaLink([LinkedTable(name, 0.0)]), values=self.values)
        return ToolResult(True, {"table": name, "description": text}, f"查看表 {name} 的结构")

    def _get_column_values(self, table: str, column: str, keyword: str | None = None) -> ToolResult:
        table_name = str(table).strip().strip('`"')
        column_name = str(column).strip().strip('`"')
        physical = self.schema.get(table_name)
        if physical is None or table_name not in self.catalog.tables:
            return ToolResult(
                False, {"error": f"表 {table_name} 不存在"}, f"表 {table_name} 不存在"
            )
        if self.catalog.is_sensitive(table_name, column_name):
            return ToolResult(
                False,
                {"error": f"字段 {column_name} 属于个人信息，不能查询取值"},
                "拒绝查询敏感字段",
            )
        definition = physical.column(column_name)
        if definition is None:
            candidates = [
                c.name
                for c in physical.columns
                if not self.catalog.is_sensitive(table_name, c.name)
            ]
            close = difflib.get_close_matches(column_name.lower(), candidates, n=3, cutoff=0.5)
            return ToolResult(
                False,
                {"error": f"字段 {column_name} 在 {table_name} 中不存在", "close_matches": close},
                "字段不存在",
            )

        quoted_column = "`" + definition.name.replace("`", "``") + "`"
        quoted_table = "`" + physical.name.replace("`", "``") + "`"
        if keyword:
            core = str(keyword).replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
            sql = (
                f"SELECT {quoted_column} AS value, COUNT(*) AS n FROM {quoted_table} "
                f"WHERE {quoted_column} LIKE {sql_string('%' + core + '%')} "
                f"GROUP BY {quoted_column} ORDER BY n DESC LIMIT 20"
            )
            result = self.backend.execute(sql, max_rows=20)
            values = [[str(row["value"]), int(row["n"])] for row in result.rows]
            complete = result.row_count < 20
        else:
            entry = (
                self.values.get(physical.name, definition.name) if self.values is not None else None
            )
            if entry is not None:
                values = [[str(v), int(n)] for v, n in entry.values[:20]]
                complete = entry.complete and len(entry.values) <= 20
            else:
                rows = self.backend.distinct_values(physical.name, definition.name, limit=21)
                values = [[str(v), int(n)] for v, n in rows[:20]]
                complete = len(rows) <= 20
        shown = "、".join(v for v, _ in values[:5]) or "无匹配取值"
        return ToolResult(
            True,
            {
                "table": physical.name,
                "column": definition.name,
                "values": values,
                "complete": complete,
            },
            f"{physical.name}.{definition.name} 取值：{shown}",
        )

    def _validate_sql(self, sql: str) -> ToolResult:
        report = self.guard.check(str(sql))
        payload = {
            "ok": report.is_safe,
            "errors": report.errors,
            "hints": report.hints,
            "modifications": report.modifications,
            "safe_sql": report.safe_sql,
        }
        summary = (
            "安全检查通过" if report.is_safe else f"安全检查未通过：{'；'.join(report.errors)}"
        )
        return ToolResult(report.is_safe, payload, summary)

    def _preview_sql(self, sql: str) -> ToolResult:
        report = self.guard.check(str(sql))
        if not report.is_safe:
            return ToolResult(
                False,
                {"errors": report.errors, "hints": report.hints},
                f"安全检查未通过：{'；'.join(report.errors)}",
            )
        cost = diagnose_cost(self.backend.estimate_cost(report.safe_sql), self.cost_limit)
        if cost is not None:
            return ToolResult(
                False, {"error": cost.message, "hints": cost.hints}, "预计代价过高，未执行"
            )
        try:
            result = self.backend.execute(report.safe_sql, max_rows=self.profile_rows)
        except BackendError as exc:
            diagnosis = diagnose_execution_error(exc, report.safe_sql)
            return ToolResult(
                False,
                {"error": diagnosis.message, "hints": diagnosis.hints},
                f"执行失败：{diagnosis.message}",
            )

        shown = result.rows[: self.preview_rows]
        payload: dict[str, Any] = {
            "columns": result.columns,
            "row_count": result.row_count,
            "truncated": result.truncated,
            "column_summary": column_summary(result.columns, result.rows),
            "rows": shown,
        }
        summary = f"试运行返回 {result.row_count} 行" + (
            f"（超过 {self.profile_rows} 行，已截断）" if result.truncated else ""
        )
        if result.row_count > len(shown):
            summary += f"，展示前 {len(shown)} 行"
        if is_effectively_empty(result):
            diagnosis = diagnose_empty_result(report.safe_sql, self.values, self.schema)
            if diagnosis is not None:
                payload["warning"] = diagnosis.message
                payload["hints"] = diagnosis.hints
                summary += "，条件取值可能写错"
        return ToolResult(True, payload, summary)
