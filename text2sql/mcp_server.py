"""MCP server：让 Claude Desktop、Claude Code、Cursor 等 MCP 客户端直接使用这个数据智能体。

提供两种粒度：

- ask_database：把问题整体交给智能体，走完整链路（意图识别 → 语义层 / SQL 智能体 → 安全门 → 执行 →
  解读 → 质量检查），返回结论、SQL、结果预览和统计口径；带 thread_id 可以连续追问。
- search_schema / describe_table / get_column_values / run_readonly_sql：给会自己写 SQL 的上层智能体。
  它们复用内部 SQL 智能体的工具箱与安全门，所以经 MCP 进来的 SQL 同样只读，
  受表白名单、敏感字段、LIMIT 与代价上限约束，不会因为换了入口就绕过防线。

全部工具标注 readOnlyHint / idempotentHint，客户端可以据此免确认调用。
智能体在第一次调用工具时才构建：stdio 客户端启动时不必等演示库生成和值索引扫描。
"""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from text2sql import __version__
from text2sql.config import Settings

PREVIEW_ROWS = 50
THREAD_ID_PATTERN = r"^[A-Za-z0-9_\-]{1,64}$"
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

INSTRUCTIONS = """\
智能制造企业数据库（企业工商、融资、对外投资、招投标、资质）的问数工具。
- 优先用 ask_database 直接提问，它会选择语义层或 SQL 智能体，并返回结论、SQL、结果预览与统计口径。
- ask_database 无法回答（status=declined）时，再用 search_schema、describe_table、get_column_values
  了解表结构和真实取值，自己写一条 SELECT 交给 run_readonly_sql。
- 只支持只读查询；法定代表人等个人信息字段不可查询。回答中的数字必须来自工具返回的结果。"""


class AskOutput(BaseModel):
    status: Literal["answered", "declined", "rejected", "failed"] = Field(
        description="answered 已回答；declined 无法可靠回答；rejected 触发安全策略；failed 执行失败"
    )
    answer: str = Field(description="结论（数字均来自查询结果），未回答时为原因")
    question: str
    effective_question: str = Field(description="结合会话上下文改写后的问题，追问时与原问题不同")
    thread_id: str = Field(description="会话 ID，下次调用传回即可追问")
    planner: str | None = Field(description="semantic 语义层编译；llm SQL 智能体")
    sql: str | None = Field(description="经过安全门改写后执行的 SQL（MySQL 方言）")
    columns: list[str]
    rows: list[dict[str, Any]] = Field(description=f"结果预览，最多 {PREVIEW_ROWS} 行")
    row_count: int
    truncated: bool = Field(description="完整结果多于预览行数，或触达了查询行数上限")
    assumptions: list[str] = Field(description="统计口径与假设，引用结果时应一并说明")
    quality_score: float | None = Field(description="结果质量检查得分，0 到 1")
    warnings: list[str] = Field(description="质量检查中需要注意的项")
    suggestions: list[str] = Field(description="未回答时推荐的可回答问题")


class ToolOutput(BaseModel):
    ok: bool
    summary: str
    data: dict[str, Any]


class _AgentHolder:
    """线程安全的懒加载：工具在工作线程里执行，第一次调用时才构建智能体。"""

    def __init__(self, agent: Any | None, settings: Settings | None) -> None:
        self._agent = agent
        self._owned = agent is None
        self._settings = settings
        self._toolbox: Any | None = None
        self._lock = threading.Lock()

    @property
    def agent(self) -> Any:
        with self._lock:
            if self._agent is None:
                from text2sql.agent.graph import Text2SQLAgent

                self._agent = Text2SQLAgent.from_settings(self._settings or Settings.from_env())
            return self._agent

    @property
    def toolbox(self) -> Any:
        agent = self.agent
        with self._lock:
            if self._toolbox is None:
                from text2sql.agent.tools import AgentToolbox

                self._toolbox = AgentToolbox(
                    catalog=agent.catalog,
                    schema=agent.schema,
                    linker=agent.linker,
                    guard=agent.guard,
                    backend=agent.backend,
                    values=agent.values,
                    cost_limit=agent.settings.cost_limit_rows,
                    preview_rows=PREVIEW_ROWS,
                )
            return self._toolbox

    def close(self) -> None:
        with self._lock:
            if self._owned and self._agent is not None:
                self._agent.close()
                self._agent = None
                self._toolbox = None


def _ask_output(result: Any) -> AskOutput:
    quality = result.quality or {}
    rows = list(result.rows[:PREVIEW_ROWS])
    return AskOutput(
        status=result.status,
        answer=result.answer or result.message or "",
        question=result.question,
        effective_question=result.effective_question,
        thread_id=result.thread_id,
        planner=result.planner,
        sql=result.safe_sql,
        columns=list(result.columns),
        rows=json.loads(json.dumps(rows, ensure_ascii=False, default=str)),
        row_count=result.row_count,
        truncated=bool(result.truncated or result.row_count > len(rows)),
        assumptions=list(result.assumptions),
        quality_score=quality.get("score"),
        warnings=[c["detail"] for c in quality.get("checks", []) if c.get("status") != "pass"],
        suggestions=list(result.suggestions),
    )


def _tool_output(result: Any) -> ToolOutput:
    data = json.loads(json.dumps(result.payload, ensure_ascii=False, default=str))
    return ToolOutput(ok=result.ok, summary=result.summary, data=data)


def build_server(*, agent: Any | None = None, settings: Settings | None = None) -> MCPServer:
    holder = _AgentHolder(agent, settings)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {}
        finally:
            holder.close()

    server = MCPServer(
        name="text2sql-analysis",
        title="Text2SQL 数据分析智能体",
        description="智能制造企业数据库的自然语言问数与只读 SQL 工具",
        instructions=INSTRUCTIONS,
        version=__version__,
        lifespan=lifespan,
    )

    @server.tool(
        title="用自然语言查询数据库",
        description="用中文提问并得到带依据的结论：自动选择语义层或 SQL 智能体，SQL 经只读安全门执行，"
        "返回结论、SQL、结果预览、统计口径和质量检查。同一 thread_id 下可以追问，例如先问"
        "“广州市存续企业有多少家”，再问“那深圳呢”。",
        annotations=READ_ONLY,
    )
    def ask_database(
        question: Annotated[str, Field(min_length=1, max_length=500, description="中文问题")],
        thread_id: Annotated[
            str | None, Field(pattern=THREAD_ID_PATTERN, description="会话 ID，追问时传回")
        ] = None,
    ) -> AskOutput:
        return _ask_output(holder.agent.ask(question, thread_id=thread_id))

    @server.tool(
        title="检索相关表",
        description="按中文关键词检索相关的表和视图，返回表名、说明与命中原因。",
        annotations=READ_ONLY,
    )
    def search_schema(
        keywords: Annotated[str, Field(min_length=1, description="例如：融资轮次 企业名称")],
    ) -> ToolOutput:
        return _tool_output(holder.toolbox.execute("search_schema", {"keywords": keywords}))

    @server.tool(
        title="查看表结构",
        description="查看一张表或视图的粒度、关联关系、字段类型、口径说明和常见取值。",
        annotations=READ_ONLY,
    )
    def describe_table(
        table: Annotated[str, Field(min_length=1, description="表或视图名，例如：融资数据")],
    ) -> ToolOutput:
        return _tool_output(holder.toolbox.execute("describe_table", {"table": table}))

    @server.tool(
        title="查看字段取值",
        description="查看字段的真实取值和出现次数，可用关键词过滤。写字符串条件前先确认取值，"
        "例如经营状态的真实取值是“存续（在营、开业、在册）”而不是“存续”。",
        annotations=READ_ONLY,
    )
    def get_column_values(
        table: Annotated[str, Field(min_length=1)],
        column: Annotated[str, Field(min_length=1)],
        keyword: Annotated[str | None, Field(description="只返回包含该关键词的取值")] = None,
    ) -> ToolOutput:
        arguments = {"table": table, "column": column, "keyword": keyword}
        return _tool_output(holder.toolbox.execute("get_column_values", arguments))

    @server.tool(
        title="执行只读 SQL",
        description=f"执行一条 MySQL 方言的 SELECT，返回最多 {PREVIEW_ROWS} 行。SQL 先经过安全门："
        "只允许单条只读查询、白名单表、禁止个人信息字段、自动补 LIMIT，并预估执行代价；"
        "未通过时 ok=false，按 data.errors 与 data.hints 修改后重试。",
        annotations=READ_ONLY,
    )
    def run_readonly_sql(
        sql: Annotated[str, Field(min_length=1, max_length=8000, description="单条 SELECT 语句")],
    ) -> ToolOutput:
        return _tool_output(holder.toolbox.execute("preview_sql", {"sql": sql}))

    @server.tool(
        title="查看语义层",
        description="返回语义层概要：实体、指标、维度、筛选条件、业务规则与示例问题。",
        annotations=READ_ONLY,
    )
    def describe_semantic_layer() -> dict[str, Any]:
        return holder.agent.catalog.summary()

    @server.resource(
        "text2sql://semantic-layer",
        name="semantic-layer",
        title="语义层概要",
        description="实体、指标、维度、筛选条件、业务规则与示例问题（JSON）",
        mime_type="application/json",
    )
    def semantic_layer() -> str:
        return json.dumps(holder.agent.catalog.summary(), ensure_ascii=False, indent=2)

    @server.prompt(
        name="analyze_question",
        title="分析一个业务问题",
        description="引导模型按“先整体提问、再自己写 SQL”的顺序使用本服务的工具",
    )
    def analyze_question(question: str) -> str:
        return (
            f"请用 text2sql-analysis 的工具回答下面的问题，并说明依据。\n\n问题：{question}\n\n"
            "1. 先调用 ask_database。status 为 answered 时，基于 answer、rows 和 assumptions 作答，"
            "并写明统计口径；warnings 不为空时提醒用户。\n"
            "2. status 为 declined 时，用 search_schema、describe_table、get_column_values 了解相关表"
            "和真实取值，自己写一条 SELECT，交给 run_readonly_sql 执行；ok 为 false 时按 errors 与"
            " hints 修改后重试。\n"
            "3. 回答里的数字必须来自工具返回的结果，不要估算；数据源是合成演示库时要说明数字仅用于演示。"
        )

    return server


def main() -> None:
    """`text2sql-mcp` 入口：以 stdio 方式运行，供 MCP 客户端直接拉起。"""
    build_server().run("stdio")
