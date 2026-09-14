"""SQL 安全门。

在 sqlglot 构建的 AST 上逐层 fail-closed 校验，任何一层不确定就拒绝：

1. 空、超长、无法解析、多语句 → 拒绝
2. 根节点必须是 SELECT 或集合运算（UNION / INTERSECT / EXCEPT）
3. 树中任意位置出现写操作、锁、会话变量、SELECT INTO 等节点 → 拒绝
4. 函数：可识别的内置函数放行（USER()/VERSION() 等信息探测函数除外），
   无法识别的函数只放行白名单，SLEEP / BENCHMARK / LOAD_FILE 一类因此天然被挡住
5. 表：只允许白名单表和视图，CTE 名不算表；禁止跨库和系统库
6. 禁止 SELECT *；禁止引用敏感列
7. 列级校验：用 sqlglot 优化器按生产 DDL 解析每个列引用，未知列、歧义列在执行前拒绝，
   并给出相近列名，供修复节点精确修正
8. 外层 LIMIT 缺失时补齐，超过上限时收紧

安全边界不只靠这一层：MySQL 后端的会话被设置为只读，DuckDB 演示库以只读方式打开。
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, SqlglotError
from sqlglot.optimizer.qualify import qualify

from text2sql.db.schema import PhysicalSchema, normalize_identifier

SYSTEM_DATABASES = frozenset({"information_schema", "mysql", "performance_schema", "sys"})

_FORBIDDEN_NODE_NAMES = (
    "Insert",
    "Update",
    "Delete",
    "Create",
    "Drop",
    "Alter",
    "Command",
    "Merge",
    "TruncateTable",
    "Set",
    "Use",
    "Transaction",
    "Commit",
    "Rollback",
    "Lock",
    "Into",
    "Copy",
    "LoadData",
    "Pragma",
    "Grant",
    "Revoke",
    "Describe",
    "Show",
    "Kill",
    "SessionParameter",
    "Parameter",
    "Placeholder",
    "PropertyEQ",
    "Analyze",
    "Cache",
    "Uncache",
    "Refresh",
    "Execute",
    "Declare",
)
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name) for name in _FORBIDDEN_NODE_NAMES if hasattr(exp, name)
)

_FORBIDDEN_FUNCTION_NAMES = (
    "CurrentUser",
    "CurrentSchema",
    "CurrentDatabase",
    "CurrentVersion",
    "CurrentCatalog",
    "SessionUser",
)
FORBIDDEN_FUNCTIONS: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name) for name in _FORBIDDEN_FUNCTION_NAMES if hasattr(exp, name)
)

# sqlglot 不认识、但在分析查询里常见且无副作用的 MySQL 函数
SAFE_ANONYMOUS_FUNCTIONS = frozenset(
    {
        "FIELD",
        "FIND_IN_SET",
        "ELT",
        "QUARTER",
        "WEEK",
        "WEEKOFYEAR",
        "YEARWEEK",
        "DAYOFWEEK",
        "DAYOFYEAR",
        "LAST_DAY",
        "MAKEDATE",
        "PERIOD_DIFF",
        "PERIOD_ADD",
        "FORMAT",
        "TRUNCATE",
        "CEILING",
        "CHAR_LENGTH",
        "CHARACTER_LENGTH",
        "INSTR",
        "LOCATE",
        "ANY_VALUE",
        "TO_DAYS",
        "FROM_DAYS",
        "SEC_TO_TIME",
        "TIME_TO_SEC",
        "MONTHNAME",
        "DAYNAME",
        "STRCMP",
        "REVERSE",
        "BIT_COUNT",
        "CONV",
        "PERCENT_RANK",
        "CUME_DIST",
        "NTILE",
        "MEDIAN",
    }
)

# 模型最常编造的列名 → 真实列名（来自 v1 线上错误日志）
KNOWN_HALLUCINATIONS: dict[str, tuple[str, ...]] = {
    "industry_name": ("industry_code",),
    "industry": ("industry_code",),
    "company_name": ("name",),
    "enterprise_name": ("name",),
    "city_name": ("district_code",),
    "city": ("district_code",),
    "area_name": ("area_code",),
    "province": ("province_code",),
    "finance_round": ("round",),
    "round_name": ("round",),
    "registered_capital": ("regist_capi_new",),
    "capital": ("regist_capi_new",),
    "establish_date": ("start_date",),
    "found_date": ("start_date",),
    "bid_amount": ("project_bid_money",),
}

_UNRESOLVED_PATTERNS = (
    re.compile(r"Column '([^']+)' could not be resolved"),
    re.compile(r"Unknown column: ([^\s,]+)"),
)
_TRAILING_LIMIT = re.compile(r"\bLIMIT\s+(\d+)(\s*,\s*(\d+))?(\s+OFFSET\s+\d+)?\s*$", re.IGNORECASE)


@dataclass
class GuardReport:
    original: str
    safe_sql: str | None = None
    is_safe: bool = False
    error_code: str | None = None
    errors: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    modifications: list[str] = field(default_factory=list)
    referenced_tables: list[str] = field(default_factory=list)
    referenced_columns: list[str] = field(default_factory=list)

    def reject(self, code: str, message: str, hints: Iterable[str] = ()) -> GuardReport:
        self.is_safe = False
        self.safe_sql = None
        self.error_code = code
        self.errors.append(message)
        self.hints.extend(hints)
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SQLGuard:
    def __init__(
        self,
        schema: PhysicalSchema,
        *,
        allowed_tables: Iterable[str],
        sensitive_columns: Iterable[str] = (),
        max_rows: int = 500,
        max_length: int = 8000,
        database: str | None = None,
        table_notes: dict[str, str] | None = None,
    ) -> None:
        self.schema = schema
        self.allowed = {normalize_identifier(t) for t in allowed_tables}
        self.sensitive = {c.lower() for c in sensitive_columns}
        self.max_rows = max_rows
        self.max_length = max_length
        self.database = database.lower() if database else None
        self.table_notes = table_notes or {}
        self._mapping = {
            name: {column.lower(): data_type for column, data_type in columns.items()}
            for name, columns in schema.to_sqlglot_mapping().items()
            if normalize_identifier(name) in self.allowed
        }

    @classmethod
    def from_catalog(
        cls, catalog, schema: PhysicalSchema, *, max_rows: int = 500, database: str | None = "znjz"
    ) -> SQLGuard:
        notes = {
            name: f"宽表，含占位行，优先使用视图 {doc.prefer}"
            for name, doc in catalog.tables.items()
            if doc.prefer
        }
        return cls(
            schema,
            allowed_tables=catalog.whitelist,
            sensitive_columns=catalog.sensitive_columns,
            max_rows=max_rows,
            database=database,
            table_notes=notes,
        )

    # ------------------------------------------------------------------ 主流程

    def check(self, sql: str) -> GuardReport:
        report = GuardReport(original=sql or "")
        text = (sql or "").strip()
        if not text:
            return report.reject("empty", "SQL 为空")
        if len(text) > self.max_length:
            return report.reject("too_long", f"SQL 长度 {len(text)} 超过上限 {self.max_length}")

        try:
            statements = [s for s in sqlglot.parse(text, read="mysql") if s is not None]
        except (ParseError, SqlglotError) as exc:
            return report.reject("parse_error", f"SQL 无法解析：{str(exc).splitlines()[0]}")
        if not statements:
            return report.reject("parse_error", "SQL 无法解析")
        if len(statements) > 1:
            return report.reject(
                "multi_statement", f"检测到 {len(statements)} 条语句，只允许单条查询"
            )

        tree = statements[0]
        root = tree.this if isinstance(tree, exp.Subquery) and not tree.alias else tree
        if not isinstance(root, (exp.Select, exp.SetOperation)):
            return report.reject(
                "not_select", f"只允许 SELECT 查询，检测到 {type(root).__name__.upper()} 语句"
            )

        for node in tree.walk():
            if isinstance(node, FORBIDDEN_NODES):
                return report.reject(
                    "forbidden_node", f"查询中包含不允许的操作：{type(node).__name__}"
                )

        for func in tree.find_all(exp.Func):
            if isinstance(func, FORBIDDEN_FUNCTIONS):
                return report.reject(
                    "forbidden_function", f"函数 {func.sql_name()} 会暴露服务器信息，不允许使用"
                )
            if (
                isinstance(func, exp.Anonymous)
                and func.name.upper() not in SAFE_ANONYMOUS_FUNCTIONS
            ):
                return report.reject(
                    "forbidden_function",
                    f"函数 {func.name.upper()} 不在允许范围内（可能用于延时攻击、读取文件或探测服务器）",
                )

        tables = self._check_tables(tree, report)
        if tables is None:
            return report
        report.referenced_tables = sorted(tables)

        for select in tree.find_all(exp.Select):
            for projection in select.expressions:
                if isinstance(projection, exp.Star) or (
                    isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
                ):
                    return report.reject("select_star", "不允许 SELECT *，请列出需要的字段")

        for column in tree.find_all(exp.Column):
            if column.name.lower() in self.sensitive:
                return report.reject(
                    "sensitive_column", f"字段 {column.name} 属于个人信息，禁止查询"
                )

        if not self._check_columns(tree, tables, report):
            return report

        safe = self._enforce_limit(text, tree, root, report)
        if safe is None:
            return report
        report.safe_sql = safe
        report.is_safe = True
        return report

    # ------------------------------------------------------------------ 表

    def _check_tables(self, tree: exp.Expression, report: GuardReport) -> set[str] | None:
        cte_names = {normalize_identifier(cte.alias_or_name) for cte in tree.find_all(exp.CTE)}
        physical: set[str] = set()
        disallowed: list[str] = []
        for table in tree.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                report.reject("forbidden_node", "不允许使用表函数或动态表")
                return None
            database = (table.db or table.catalog or "").lower()
            if database and (database in SYSTEM_DATABASES or database != self.database):
                report.reject("cross_database", f"不允许跨库访问：{table.db}.{table.name}")
                return None
            key = normalize_identifier(table.name)
            if not database and key in cte_names:
                continue
            if key not in self.allowed:
                disallowed.append(table.name)
                continue
            canonical = self.schema.get(table.name)
            physical.add(canonical.name if canonical else table.name)
        if disallowed:
            report.reject(
                "table_not_allowed", f"引用了白名单之外的表：{', '.join(sorted(set(disallowed)))}"
            )
            return None
        return physical

    # ------------------------------------------------------------------ 列

    def _check_columns(self, tree: exp.Expression, tables: set[str], report: GuardReport) -> bool:
        probe = tree.copy()
        for column in probe.find_all(exp.Column):
            if isinstance(column.this, exp.Identifier):
                column.this.set("this", column.this.this.lower())
        for table in probe.find_all(exp.Table):
            canonical = self.schema.get(table.name)
            if canonical is not None:
                table.this.set("this", canonical.name)

        try:
            qualified = qualify(
                probe,
                schema=self._mapping,
                dialect="mysql",
                validate_qualify_columns=True,
                quote_identifiers=False,
                identify=False,
            )
        except OptimizeError as exc:
            self._reject_column(str(exc), tables, report)
            return False
        except SqlglotError as exc:
            # 列级校验是精确度辅助，不是安全边界；优化器自身不支持的写法交给数据库报错
            report.warnings.append(f"列级校验未完成：{str(exc).splitlines()[0]}")
            return True

        sources: dict[str, str] = {}
        for table in qualified.find_all(exp.Table):
            canonical = self.schema.get(table.name)
            if canonical is not None:
                sources[table.alias_or_name] = canonical.name
        referenced = {
            f"{sources[column.table]}.{column.name}"
            for column in qualified.find_all(exp.Column)
            if column.table in sources
        }
        report.referenced_columns = sorted(referenced)
        return True

    def _reject_column(self, message: str, tables: set[str], report: GuardReport) -> None:
        name = next((m.group(1) for p in _UNRESOLVED_PATTERNS if (m := p.search(message))), None)
        if name is None:
            report.reject("unknown_column", f"字段无法解析：{message.splitlines()[0]}")
            return
        lowered = name.lower()
        owners = sorted(
            t for t in tables if (d := self.schema.get(t)) is not None and d.has_column(lowered)
        )
        if len(owners) >= 2:
            report.reject(
                "ambiguous_column",
                f"字段 {name} 同时存在于 {'、'.join(owners)}，请用表别名限定（例如 e.{name}）",
            )
            return

        hints: list[str] = []
        # 提示里绝不出现敏感列，否则等于把个人信息字段名教给模型
        candidates = sorted(
            {
                c.name
                for t in tables
                if (d := self.schema.get(t))
                for c in d.columns
                if c.name.lower() not in self.sensitive
            }
        )
        for suggestion in KNOWN_HALLUCINATIONS.get(lowered, ()):
            if suggestion in candidates:
                hints.append(f"{name} 不存在，对应的真实字段是 {suggestion}")
        for close in difflib.get_close_matches(lowered, candidates, n=3, cutoff=0.6):
            if not any(close in h for h in hints):
                hints.append(f"相近字段：{close}")
        elsewhere = sorted(
            t.name
            for t in self.schema.tables.values()
            if normalize_identifier(t.name) in self.allowed
            and t.name not in tables
            and t.has_column(lowered)
        )
        for other in elsewhere:
            note = self.table_notes.get(other)
            hints.append(f"{name} 只存在于表 {other}" + (f"（{note}）" if note else ""))
        where = "、".join(sorted(tables)) or "所引用的表"
        report.reject("unknown_column", f"字段 {name} 在 {where} 中不存在", hints)

    # ------------------------------------------------------------------ LIMIT

    def _enforce_limit(
        self, text: str, tree: exp.Expression, root: exp.Expression, report: GuardReport
    ) -> str | None:
        clean = text.rstrip().rstrip(";").rstrip()
        limit = root.args.get("limit")
        has_comments = any(node.comments for node in tree.walk())

        if limit is None:
            if self._is_single_row_aggregate(root):
                return clean
            report.modifications.append(f"补充 LIMIT {self.max_rows}")
            if not has_comments:
                separator = "\n" if "\n" in clean else " "
                candidate = f"{clean}{separator}LIMIT {self.max_rows}"
                if self._outer_limit(candidate) == self.max_rows:
                    return candidate
            return self._regenerate(tree, root, self.max_rows)

        value = limit.expression
        if not (isinstance(value, exp.Literal) and value.is_int):
            report.reject("invalid_limit", "LIMIT 必须是整数常量")
            return None
        requested = int(value.this)
        if requested <= self.max_rows:
            return clean

        report.modifications.append(f"LIMIT {requested} 超过上限，收紧为 {self.max_rows}")
        match = _TRAILING_LIMIT.search(clean)
        if match and not has_comments:
            if match.group(3):  # MySQL LIMIT offset, count
                start, end = match.span(3)
            else:
                start, end = match.span(1)
            candidate = clean[:start] + str(self.max_rows) + clean[end:]
            if self._outer_limit(candidate) == self.max_rows:
                return candidate
        return self._regenerate(tree, root, self.max_rows)

    @staticmethod
    def _is_single_row_aggregate(root: exp.Expression) -> bool:
        """没有 GROUP BY、每个输出列都是聚合且不含窗口函数的查询最多返回一行。"""
        if not isinstance(root, exp.Select) or root.args.get("group") or not root.expressions:
            return False
        for projection in root.expressions:
            inner = projection.unalias()
            if inner.find(exp.Window) or not inner.find(exp.AggFunc):
                return False
        return True

    @staticmethod
    def _outer_limit(sql: str) -> int | None:
        try:
            parsed = sqlglot.parse_one(sql, read="mysql")
        except SqlglotError:
            return None
        node = parsed.this if isinstance(parsed, exp.Subquery) and not parsed.alias else parsed
        limit = node.args.get("limit")
        if limit is None or not isinstance(limit.expression, exp.Literal):
            return None
        return int(limit.expression.this)

    @staticmethod
    def _regenerate(tree: exp.Expression, root: exp.Expression, max_rows: int) -> str:
        rewritten = tree.copy()
        target = (
            rewritten.this
            if isinstance(rewritten, exp.Subquery) and not rewritten.alias
            else rewritten
        )
        target.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        return target.sql(dialect="mysql")
