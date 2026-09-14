"""Text2SQL Agent 工作流。

11 个节点：

    understand → link → plan → guard → execute → profile → visualize → narrate → reflect → finalize
                                  ↑        │          │
                                  └─ repair ┘←─────────┘（报错 / 代价超限 / 可疑空结果，且可修复）

节点是“状态 → 更新”的函数，路由写成下方的声明式路由表；编排器可替换：

- langgraph（默认，CLI / API / MCP）：检查点按 thread_id 保存对话历史，stream() 同时输出
  节点完成事件与 SQL 智能体的每一步工具调用；
- sequential（浏览器里的 Pyodide）：LangGraph 依赖的原生扩展装不上，按同一张路由表顺序执行
  同一组节点，tests/test_orchestrators.py 用评测集核对两者结果一致。

规划器可以按需开关：只开语义层时整个流程不访问任何模型。
"""

from __future__ import annotations

import operator
import time
import uuid
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from functools import wraps
from typing import Annotated, Any, TypedDict

from text2sql.agent.charts import recommend_chart
from text2sql.agent.conversation import has_follow_up_cue, looks_like_follow_up, rewrite_follow_up
from text2sql.agent.exemplars import ZNJZ_EXEMPLARS_PATH, ExemplarStore, question_similarity
from text2sql.agent.intent import classify_intent
from text2sql.agent.linking import LinkedTable, SchemaLink, SchemaLinker
from text2sql.agent.llm import LLMClient, LLMError, TokenUsage, build_llm
from text2sql.agent.narrative import deterministic_narrative, llm_narrative
from text2sql.agent.profiling import ResultProfile, profile_result
from text2sql.agent.prompts import (
    ConversationTurn,
    PromptContext,
    single_shot_system_prompt,
    sql_agent_system_prompt,
)
from text2sql.agent.reflection import check_numbers, reflect
from text2sql.agent.repair import (
    Diagnosis,
    diagnose_cost,
    diagnose_count_per_group,
    diagnose_empty_result,
    diagnose_execution_error,
    diagnose_guard,
)
from text2sql.agent.sql_agent import SQLAgent
from text2sql.agent.tools import AgentToolbox, is_effectively_empty
from text2sql.agent.values import ValueIndex, build_value_index
from text2sql.config import Settings
from text2sql.db.backends import Backend, BackendError, build_backend
from text2sql.db.schema import PhysicalSchema, load_znjz_schema
from text2sql.semantic.catalog import SemanticCatalog, load_znjz_catalog
from text2sql.semantic.compiler import compile_plan
from text2sql.semantic.parser import SemanticParser
from text2sql.sql.guard import SQLGuard

NODE_ORDER = (
    "understand",
    "link",
    "plan",
    "guard",
    "execute",
    "repair",
    "profile",
    "visualize",
    "narrate",
    "reflect",
    "finalize",
)

STATUS_LABELS = {
    "answered": "已回答",
    "declined": "暂不支持",
    "rejected": "已拒绝",
    "failed": "失败",
}
PLANNER_LABELS = {"semantic": "语义层编译", "llm": "SQL 智能体"}
ORCHESTRATORS = ("langgraph", "sequential")

START_NODE, END_NODE = "__start__", "__end__"
# 固定边：节点完成后必然进入的下一个节点
FIXED_EDGES: dict[str, str] = {
    "link": "plan",
    "profile": "visualize",
    "visualize": "narrate",
    "narrate": "reflect",
    "reflect": "finalize",
    "finalize": END_NODE,
}
# 条件边：节点把下一步写进 state["route"]，只能走表里列出的目标
CONDITIONAL_EDGES: dict[str, tuple[str, ...]] = {
    "understand": ("link", "finalize"),
    "plan": ("guard", "finalize"),
    "guard": ("execute", "repair", "finalize"),
    "execute": ("profile", "repair", "finalize"),
    "repair": ("guard", "finalize"),
}
MAX_STEPS = 60

# 顺序编排器在运行期间把事件回调放在这里；LangGraph 编排时使用 LangGraph 自己的流写入器
LOCAL_STREAM_WRITER: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "text2sql_stream_writer", default=None
)


def _stream_writer() -> Callable[[dict[str, Any]], None]:
    local = LOCAL_STREAM_WRITER.get()
    if local is not None:
        return local
    try:
        from langgraph.config import get_stream_writer

        return get_stream_writer()
    except (ImportError, RuntimeError):
        return lambda event: None


class AgentState(TypedDict, total=False):
    question: str
    effective_question: str
    follow_up: dict[str, Any] | None
    intent: dict[str, Any]
    route: str
    status: str | None
    message: str | None
    error: str | None
    suggestions: list[str]
    planner: str | None
    llm_mode: str | None
    parse: dict[str, Any] | None
    link: dict[str, Any] | None
    exemplar_ids: list[str]
    plan: dict[str, Any] | None
    description: str | None
    assumptions: list[str]
    sql: str | None
    guard: dict[str, Any] | None
    safe_sql: str | None
    executed_sql: str | None
    diagnosis: dict[str, Any] | None
    empty_diagnosis: dict[str, Any] | None
    attempts: int
    agent_steps: list[dict[str, Any]]
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    execution_ms: float
    profile: dict[str, Any] | None
    chart: dict[str, Any] | None
    narrative: str
    narrative_source: str
    ungrounded: list[str]
    quality: dict[str, Any] | None
    usage: dict[str, int]
    trace: list[dict[str, Any]]
    history: Annotated[list[dict[str, Any]], operator.add]


PER_TURN_DEFAULTS: dict[str, Any] = {
    "follow_up": None,
    "intent": {},
    "route": "",
    "status": None,
    "message": None,
    "error": None,
    "suggestions": [],
    "planner": None,
    "llm_mode": None,
    "parse": None,
    "link": None,
    "exemplar_ids": [],
    "plan": None,
    "description": None,
    "assumptions": [],
    "sql": None,
    "guard": None,
    "safe_sql": None,
    "executed_sql": None,
    "diagnosis": None,
    "empty_diagnosis": None,
    "attempts": 0,
    "agent_steps": [],
    "columns": [],
    "rows": [],
    "row_count": 0,
    "truncated": False,
    "execution_ms": 0.0,
    "profile": None,
    "chart": None,
    "narrative": "",
    "narrative_source": "deterministic",
    "ungrounded": [],
    "quality": None,
    "usage": TokenUsage().to_dict(),
}


def _traced(name: str):
    def decorator(method):
        @wraps(method)
        def wrapper(self: Text2SQLAgent, state: AgentState) -> dict[str, Any]:
            started = time.perf_counter()
            updates, detail, status = method(self, state)
            entry = {
                "node": name,
                "status": status,
                "ms": round((time.perf_counter() - started) * 1000, 1),
                "detail": detail,
            }
            previous = [] if name == "understand" else list(state.get("trace") or [])
            updates["trace"] = previous + [entry]
            return updates

        return wrapper

    return decorator


def _merge_usage(current: dict[str, int] | None, extra: TokenUsage) -> dict[str, int]:
    usage = TokenUsage(
        **{
            k: v
            for k, v in (current or {}).items()
            if k in ("prompt_tokens", "completion_tokens", "calls")
        }
    )
    usage.add(extra)
    return usage.to_dict()


@dataclass
class AgentResult:
    question: str
    effective_question: str
    thread_id: str
    status: str
    planner: str | None = None
    message: str | None = None
    answer: str = ""
    narrative_source: str = "deterministic"
    description: str | None = None
    assumptions: list[str] = field(default_factory=list)
    follow_up: dict[str, Any] | None = None
    sql: str | None = None
    safe_sql: str | None = None
    executed_sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    chart: dict[str, Any] | None = None
    profile: dict[str, Any] | None = None
    quality: dict[str, Any] | None = None
    guard: dict[str, Any] | None = None
    diagnosis: dict[str, Any] | None = None
    agent_steps: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)
    backend: str = ""
    elapsed_ms: float = 0.0
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.status == "answered"

    def report_markdown(self, *, max_rows: int = 20) -> str:
        lines = [f"# {self.question}", ""]
        meta = [f"状态：{STATUS_LABELS.get(self.status, self.status)}"]
        if self.planner:
            meta.append(f"规划：{PLANNER_LABELS.get(self.planner, self.planner)}")
        meta += [f"数据源：{self.backend}", f"耗时：{self.elapsed_ms:.0f} ms"]
        lines += ["> " + " ｜ ".join(meta), ""]
        if self.effective_question and self.effective_question != self.question:
            lines += [f"已理解为：{self.effective_question}", ""]
        lines += ["## 结论", self.answer or self.message or "无", ""]
        if self.description or self.assumptions:
            lines.append("## 统计口径")
            if self.description:
                lines.append(self.description)
            lines += [f"- {note}" for note in self.assumptions]
            lines.append("")
        if self.safe_sql or self.sql:
            lines += ["## SQL", "```sql", self.safe_sql or self.sql or "", "```", ""]
        if self.columns:
            lines += [
                f"## 结果（共 {self.row_count} 行，展示前 {min(max_rows, self.row_count)} 行）",
                "",
            ]
            lines.append("| " + " | ".join(self.columns) + " |")
            lines.append("| " + " | ".join("---" for _ in self.columns) + " |")
            for row in self.rows[:max_rows]:
                lines.append(
                    "| "
                    + " | ".join(
                        "" if row.get(c) is None else str(row.get(c)) for c in self.columns
                    )
                    + " |"
                )
            lines.append("")
        if self.quality:
            lines.append(f"## 质量检查（得分 {self.quality['score']}）")
            for check in self.quality["checks"]:
                marker = {"pass": "[通过]", "warn": "[注意]", "fail": "[未通过]"}.get(
                    check["status"], ""
                )
                lines.append(f"- {marker} {check['detail']}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["success"] = self.success
        # 兼容 v1 接口字段
        payload["analysis"] = self.answer
        payload["report"] = self.report_markdown()
        payload["safety"] = self.guard
        payload["scenario"] = "auto"
        return payload


class Text2SQLAgent:
    def __init__(
        self,
        *,
        catalog: SemanticCatalog,
        schema: PhysicalSchema,
        backend: Backend,
        settings: Settings | None = None,
        llm: LLMClient | None = None,
        planners: tuple[str, ...] = ("semantic", "llm"),
        values: ValueIndex | None = None,
        exemplars: ExemplarStore | None = None,
        checkpointer: Any | None = None,
        orchestrator: str = "langgraph",
    ) -> None:
        if orchestrator not in ORCHESTRATORS:
            raise ValueError(
                f"orchestrator 只能是 {' / '.join(ORCHESTRATORS)}，收到：{orchestrator}"
            )
        self.catalog = catalog
        self.schema = schema
        self.backend = backend
        self.settings = settings or Settings.from_mapping({})
        self.llm = llm
        self.planners = tuple(planners)
        self.guard = SQLGuard.from_catalog(catalog, schema, max_rows=self.settings.max_rows)
        self.parser = SemanticParser(catalog)
        self.linker = SchemaLinker(catalog, schema)
        self.values = values if values is not None else build_value_index(backend, catalog)
        self.exemplars = (
            exemplars if exemplars is not None else ExemplarStore.load(ZNJZ_EXEMPLARS_PATH)
        )
        self.toolbox = AgentToolbox(
            catalog=catalog,
            schema=schema,
            linker=self.linker,
            guard=self.guard,
            backend=backend,
            values=self.values,
            cost_limit=self.settings.cost_limit_rows,
        )
        max_steps = self.settings.llm.max_agent_steps if self.settings.llm else 8
        self.sql_agent = (
            SQLAgent(llm, self.toolbox, max_steps=max_steps)
            if llm is not None and "llm" in self.planners
            else None
        )
        self.orchestrator = orchestrator
        self.checkpointer: Any | None = None
        self.graph: Any | None = None
        self.runner: Any | None = None
        if orchestrator == "langgraph":
            from langgraph.checkpoint.memory import InMemorySaver

            self.checkpointer = checkpointer or InMemorySaver()
            self.graph = self._build_graph()
        else:
            from text2sql.agent.runner import SequentialRunner

            self.runner = SequentialRunner(self, max_steps=MAX_STEPS)

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        llm: LLMClient | None = None,
        planners: tuple[str, ...] = ("semantic", "llm"),
        orchestrator: str = "langgraph",
    ) -> Text2SQLAgent:
        settings = settings or Settings.from_env()
        return cls(
            catalog=load_znjz_catalog(),
            schema=load_znjz_schema(),
            backend=build_backend(settings),
            settings=settings,
            llm=llm if llm is not None else build_llm(settings.llm),
            planners=planners,
            orchestrator=orchestrator,
        )

    # ------------------------------------------------------------------ 对外接口

    def ask(self, question: str, *, thread_id: str | None = None) -> AgentResult:
        config, thread = self._config(thread_id)
        started = time.perf_counter()
        if self.runner is not None:
            state = self.runner.run(question, thread)
        else:
            state = self.graph.invoke({"question": question}, config)
        return self._result(state, thread, started)

    def stream(self, question: str, *, thread_id: str | None = None) -> Iterator[dict[str, Any]]:
        config, thread = self._config(thread_id)
        started = time.perf_counter()
        if self.runner is not None:
            # 顺序编排没有后台线程，事件在整轮结束后按发生顺序给出；需要实时进度时用 runner.run(emit=...)
            events: list[dict[str, Any]] = []
            state = self.runner.run(question, thread, emit=events.append)
            yield from events
            yield {"type": "result", "result": self._result(state, thread, started).to_dict()}
            return
        for mode, chunk in self.graph.stream(
            {"question": question}, config, stream_mode=["updates", "custom"]
        ):
            if mode == "custom":
                yield chunk
                continue
            for update in chunk.values():
                trace = (update or {}).get("trace") or []
                if trace:
                    yield {"type": "node", **trace[-1]}
        state = self.graph.get_state(config).values
        yield {"type": "result", "result": self._result(state, thread, started).to_dict()}

    def history(self, thread_id: str) -> list[dict[str, Any]]:
        if self.runner is not None:
            return self.runner.history(thread_id)
        snapshot = self.graph.get_state({"configurable": {"thread_id": thread_id}})
        return list((snapshot.values or {}).get("history") or [])

    def status(self) -> dict[str, Any]:
        llm_info: dict[str, Any] | None = None
        if self.llm is not None:
            llm_info = (
                self.settings.llm.describe()
                if self.settings.llm
                else {"model": getattr(self.llm, "model", "custom")}
            )
        return {
            "backend": self.backend.name,
            "planners": list(self.planners),
            "llm": llm_info,
            "sql_agent": self.sql_agent is not None,
            "value_index": {
                "columns": len(self.values.columns),
                "skipped": len(self.values.skipped),
                "ms": self.values.elapsed_ms,
            },
            "exemplars": len(self.exemplars.exemplars),
            "max_rows": self.settings.max_rows,
            "max_repairs": self.settings.max_repairs,
            "orchestrator": self.orchestrator,
            "checkpointer": type(self.checkpointer).__name__ if self.checkpointer else None,
            "nodes": list(NODE_ORDER),
        }

    def close(self) -> None:
        self.backend.close()

    # ------------------------------------------------------------------ 图

    def _build_graph(self):
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(AgentState)
        for name in NODE_ORDER:
            graph.add_node(name, getattr(self, f"_node_{name}"))
        graph.add_edge(START, "understand")
        route = operator.itemgetter("route")
        for source, targets in CONDITIONAL_EDGES.items():
            graph.add_conditional_edges(source, route, {target: target for target in targets})
        for source, target in FIXED_EDGES.items():
            graph.add_edge(source, END if target == END_NODE else target)
        return graph.compile(checkpointer=self.checkpointer)

    def _config(self, thread_id: str | None) -> tuple[dict[str, Any], str]:
        thread = thread_id or f"single-{uuid.uuid4().hex}"
        return {"configurable": {"thread_id": thread}, "recursion_limit": MAX_STEPS}, thread

    # ------------------------------------------------------------------ 节点

    @_traced("understand")
    def _node_understand(self, state: AgentState):
        question = (state.get("question") or "").strip()
        updates: dict[str, Any] = dict(PER_TURN_DEFAULTS)
        updates["effective_question"] = question
        intent = classify_intent(question, self.catalog)
        detail: dict[str, Any] = {"intent": intent.kind, "task": intent.task}

        previous = self._last_answered(state.get("history") or [])
        if (
            intent.kind in ("query", "out_of_domain")
            and previous
            and looks_like_follow_up(question, self.parser)
        ):
            rewrite = rewrite_follow_up(question, previous["effective_question"], self.parser)
            if rewrite is not None and self.parser.parse(rewrite.effective_question).ok:
                updates["effective_question"] = rewrite.effective_question
                updates["follow_up"] = rewrite.to_dict()
                detail["follow_up"] = rewrite.operations
                intent = classify_intent(rewrite.effective_question, self.catalog)
            elif intent.kind == "out_of_domain":
                intent.kind = "query"  # 追问里常常没有领域词，交给带上下文的模型路径
        updates["intent"] = intent.to_dict()

        if intent.kind != "query":
            rejected = ("write_request", "injection", "personal_info")
            status = "rejected" if intent.kind in rejected else "declined"
            updates.update(status=status, message=intent.message, route="finalize")
            return updates, detail, status
        if not previous and has_follow_up_cue(question):
            updates.update(
                status="declined",
                route="finalize",
                message="这像是对上一轮的追问，但当前会话还没有上一轮问题。请补全问题，例如“深圳市存续企业有多少家”。",
            )
            return updates, detail, "declined"
        updates["route"] = "link"
        return updates, detail, "ok"

    @_traced("link")
    def _node_link(self, state: AgentState):
        question = state["effective_question"]
        link = self.linker.link(question, value_mentions=self.values.mentions(question))
        return {"link": link.to_dict()}, {"tables": link.names()}, "ok"

    @_traced("plan")
    def _node_plan(self, state: AgentState):
        question = state["effective_question"]
        reason = "未启用语义层"
        if "semantic" in self.planners:
            parsed = self.parser.parse(question)
            if parsed.ok:
                compiled = compile_plan(parsed.plan, self.catalog, max_rows=self.settings.max_rows)
                assumptions = list(compiled.assumptions)
                if state.get("follow_up"):
                    assumptions.insert(0, f"承接上一轮，已理解为：{question}")
                updates = {
                    "planner": "semantic",
                    "parse": parsed.to_dict(),
                    "plan": parsed.plan.to_dict(),
                    "sql": compiled.sql,
                    "description": compiled.description,
                    "assumptions": assumptions,
                    "route": "guard",
                }
                return updates, {"planner": "semantic", "description": compiled.description}, "ok"
            reason = parsed.reason or reason
        if self.sql_agent is not None:
            # 只有语义层真的尝试过才记录放弃原因；只走 SQL 智能体的数据集（开源数据、上传文件）没有这一步
            detail = {"semantic_declined": reason} if "semantic" in self.planners else {}
            return self._draft_with_llm(state, feedback=None, detail=detail)

        hint = (
            "配置模型（OPENAI_API_KEY）后，可由 SQL 智能体回答语义层覆盖不到的问题。"
            if self.llm is None
            else ""
        )
        updates = {
            "status": "declined",
            "message": f"语义层暂时无法回答：{reason}。{hint}".strip(),
            "suggestions": self._suggest(question),
            "route": "finalize",
        }
        return updates, {"semantic_declined": reason}, "declined"

    def _draft_with_llm(self, state: AgentState, *, feedback: str | None, detail: dict[str, Any]):
        question = state["effective_question"]
        tables = [t["name"] for t in (state.get("link") or {}).get("tables", [])]
        hits = self.exemplars.search(question, tables=tables, k=3)
        turns = [
            ConversationTurn(
                t["question"], t.get("sql"), t.get("description"), t.get("assumptions") or []
            )
            for t in (state.get("history") or [])
            if t.get("status") == "answered"
        ][-2:]
        context = PromptContext.from_catalog(
            self.catalog,
            schema_text=self.linker.render(
                SchemaLink([LinkedTable(n, 0.0) for n in tables]), values=self.values
            ),
            exemplars_text=ExemplarStore.render(hits),
            max_rows=self.settings.max_rows,
            history=turns,
            value_mentions=self.values.mentions(question),
        )
        writer = _stream_writer()
        updates: dict[str, Any] = {"planner": "llm", "exemplar_ids": [e.id for e, _ in hits]}
        try:
            draft = self.sql_agent.draft(
                question,
                system_prompt=sql_agent_system_prompt(context),
                fallback_prompt=single_shot_system_prompt(context),
                feedback=feedback,
                on_step=lambda step: writer({"type": "agent_step", **step.to_dict()}),
            )
        except LLMError as exc:
            updates.update(status="failed", error=str(exc), message=str(exc), route="finalize")
            return updates, {**detail, "planner": "llm", "error": str(exc)}, "failed"

        updates["agent_steps"] = list(state.get("agent_steps") or []) + [
            s.to_dict() for s in draft.steps
        ]
        updates["usage"] = _merge_usage(state.get("usage"), draft.usage)
        updates["llm_mode"] = draft.mode
        info = {
            **detail,
            "planner": "llm",
            "mode": draft.mode,
            "steps": len(draft.steps),
            "tokens": draft.usage.total_tokens,
        }
        if hits:
            info["exemplars"] = [e.id for e, _ in hits]
        if draft.sql is None:
            updates.update(
                status="failed",
                error=draft.error,
                message=f"SQL 智能体没有生成可用的 SQL：{draft.error}",
                route="finalize",
            )
            return updates, info, "failed"
        updates.update(
            sql=draft.sql,
            assumptions=draft.assumptions,
            description="；".join(draft.assumptions)
            if draft.assumptions
            else "由 SQL 智能体根据问题生成",
            route="guard",
        )
        return updates, info, "ok"

    def _can_repair(self, state: AgentState, diagnosis: Diagnosis) -> bool:
        return (
            diagnosis.repairable
            and state.get("planner") == "llm"
            and self.sql_agent is not None
            and int(state.get("attempts") or 0) < self.settings.max_repairs
        )

    def _terminal(self, updates: dict[str, Any], diagnosis: Diagnosis) -> None:
        status = "rejected" if not diagnosis.repairable else "failed"
        updates.update(
            status=status, message=diagnosis.message, error=diagnosis.message, route="finalize"
        )

    @_traced("guard")
    def _node_guard(self, state: AgentState):
        report = self.guard.check(state.get("sql") or "")
        updates: dict[str, Any] = {"guard": report.to_dict()}
        if report.is_safe:
            cost = diagnose_cost(
                self.backend.estimate_cost(report.safe_sql), self.settings.cost_limit_rows
            )
            if cost is None:
                updates.update(safe_sql=report.safe_sql, route="execute")
                return (
                    updates,
                    {"modifications": report.modifications, "tables": report.referenced_tables},
                    "ok",
                )
            diagnosis = cost
        else:
            diagnosis = diagnose_guard(report)
        updates["diagnosis"] = diagnosis.to_dict()
        if self._can_repair(state, diagnosis):
            updates["route"] = "repair"
        else:
            self._terminal(updates, diagnosis)
        return updates, {"code": diagnosis.code, "message": diagnosis.message}, "rejected"

    @_traced("execute")
    def _node_execute(self, state: AgentState):
        sql = state["safe_sql"] or ""
        try:
            result = self.backend.execute(sql, max_rows=self.settings.max_rows)
        except BackendError as exc:
            diagnosis = diagnose_execution_error(exc, sql)
            updates: dict[str, Any] = {"diagnosis": diagnosis.to_dict()}
            if self._can_repair(state, diagnosis):
                updates["route"] = "repair"
            else:
                self._terminal(updates, diagnosis)
            return updates, {"code": diagnosis.code, "message": diagnosis.message}, "error"

        updates = {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "truncated": result.truncated,
            "executed_sql": result.executed_sql,
            "execution_ms": result.elapsed_ms,
            "route": "profile",
        }
        detail: dict[str, Any] = {"rows": result.row_count, "ms": result.elapsed_ms}
        shape = diagnose_count_per_group(
            state["effective_question"], (state.get("intent") or {}).get("task"), result
        )
        if shape is not None and self._can_repair(state, shape):
            updates.update(diagnosis=shape.to_dict(), route="repair")
            return updates, {**detail, "code": shape.code}, "suspicious_shape"
        if is_effectively_empty(result):
            diagnosis = diagnose_empty_result(sql, self.values, self.schema)
            if diagnosis is not None:
                detail["suspicious_empty"] = diagnosis.message
                if self._can_repair(state, diagnosis):
                    updates.update(diagnosis=diagnosis.to_dict(), route="repair")
                    return updates, detail, "suspicious_empty"
                updates["empty_diagnosis"] = diagnosis.to_dict()
        return updates, detail, "ok"

    @_traced("repair")
    def _node_repair(self, state: AgentState):
        attempts = int(state.get("attempts") or 0) + 1
        diagnosis = Diagnosis(**state["diagnosis"])
        updates, detail, status = self._draft_with_llm(
            state,
            feedback=diagnosis.feedback(state.get("safe_sql") or state.get("sql") or ""),
            detail={"attempt": attempts, "reason": diagnosis.code},
        )
        updates.update(
            attempts=attempts, safe_sql=None, diagnosis=None, columns=[], rows=[], row_count=0
        )
        return updates, detail, status

    def _profile_of(self, state: AgentState) -> ResultProfile:
        return profile_result(
            state.get("columns") or [],
            state.get("rows") or [],
            truncated=bool(state.get("truncated")),
            anchor_year=self.catalog.anchor_year if self.catalog.anchor_partial else None,
        )

    @_traced("profile")
    def _node_profile(self, state: AgentState):
        profile = self._profile_of(state)
        return (
            {"profile": profile.to_dict()},
            {"shape": profile.shape, "facts": len(profile.facts)},
            "ok",
        )

    @_traced("visualize")
    def _node_visualize(self, state: AgentState):
        # 语义层的口径描述简短，适合做图表标题；SQL 智能体的口径是多条说明拼接，标题改用问题本身
        semantic = state.get("planner") == "semantic" and state.get("description")
        title = state.get("description") if semantic else state["effective_question"]
        chart = recommend_chart(self._profile_of(state), title=title)
        return (
            {"chart": chart.to_dict() if chart else None},
            {"chart": chart.type if chart else None},
            "ok",
        )

    @_traced("narrate")
    def _node_narrate(self, state: AgentState):
        profile = self._profile_of(state)
        deterministic = deterministic_narrative(profile)
        if (
            self.llm is None
            or "llm" not in self.planners
            or not self.settings.llm_narrative
            or profile.shape == "empty"
        ):
            return (
                {"narrative": deterministic, "narrative_source": "deterministic"},
                {"source": "deterministic"},
                "ok",
            )
        try:
            text, usage = llm_narrative(
                self.llm,
                question=state["effective_question"],
                description=state.get("description") or "",
                profile=profile,
            )
        except LLMError as exc:
            return (
                {"narrative": deterministic, "narrative_source": "deterministic"},
                {"source": "deterministic", "llm_error": str(exc)},
                "ok",
            )
        merged = _merge_usage(state.get("usage"), usage)
        _, ungrounded = check_numbers(
            text,
            profile=profile,
            rows=state.get("rows") or [],
            question=state["effective_question"],
        )
        if ungrounded or not text.strip():
            updates = {
                "narrative": deterministic,
                "narrative_source": "llm_rejected",
                "ungrounded": ungrounded,
                "usage": merged,
            }
            return updates, {"source": "llm_rejected", "ungrounded": ungrounded}, "fallback"
        return (
            {"narrative": text, "narrative_source": "llm", "usage": merged},
            {"source": "llm"},
            "ok",
        )

    @_traced("reflect")
    def _node_reflect(self, state: AgentState):
        intent = state.get("intent") or {}
        report = reflect(
            question=state["effective_question"],
            task=intent.get("task"),
            top_n=intent.get("top_n"),
            sql=state.get("safe_sql"),
            rows=state.get("rows") or [],
            truncated=bool(state.get("truncated")),
            max_rows=self.settings.max_rows,
            profile=self._profile_of(state),
            narrative_source=state.get("narrative_source") or "deterministic",
            ungrounded_numbers=state.get("ungrounded") or [],
            assumptions=state.get("assumptions") or [],
            planner=state.get("planner"),
            catalog=self.catalog,
        )
        return (
            {"quality": report.to_dict()},
            {"score": report.score, "warnings": [c.name for c in report.warnings]},
            "ok",
        )

    @_traced("finalize")
    def _node_finalize(self, state: AgentState):
        status = state.get("status") or ("answered" if state.get("executed_sql") else "failed")
        turn = {
            "question": state.get("question"),
            "effective_question": state.get("effective_question"),
            "status": status,
            "planner": state.get("planner"),
            "sql": state.get("safe_sql"),
            "description": state.get("description"),
            "assumptions": state.get("assumptions") or [],
        }
        return {"status": status, "history": [turn]}, {"status": status}, status

    # ------------------------------------------------------------------ 辅助

    @staticmethod
    def _last_answered(history: list[dict[str, Any]]) -> dict[str, Any] | None:
        return next((turn for turn in reversed(history) if turn.get("status") == "answered"), None)

    def _suggest(self, question: str, k: int = 3) -> list[str]:
        candidates = [
            e.question
            for e in self.catalog.examples
            if not e.requires_llm or self.sql_agent is not None
        ]
        ranked = sorted(candidates, key=lambda q: -question_similarity(question, q))
        return ranked[:k]

    def _result(self, state: dict[str, Any], thread_id: str, started: float) -> AgentResult:
        status = state.get("status") or "failed"
        answered = status == "answered"
        return AgentResult(
            question=state.get("question") or "",
            effective_question=state.get("effective_question") or state.get("question") or "",
            thread_id=thread_id,
            status=status,
            planner=state.get("planner"),
            message=state.get("message"),
            answer=(state.get("narrative") or "") if answered else (state.get("message") or ""),
            narrative_source=state.get("narrative_source") or "deterministic",
            description=state.get("description"),
            assumptions=list(state.get("assumptions") or []),
            follow_up=state.get("follow_up"),
            sql=state.get("sql"),
            safe_sql=state.get("safe_sql"),
            executed_sql=state.get("executed_sql"),
            columns=list(state.get("columns") or []),
            rows=list(state.get("rows") or []),
            row_count=int(state.get("row_count") or 0),
            truncated=bool(state.get("truncated")),
            chart=state.get("chart"),
            profile=state.get("profile"),
            quality=state.get("quality"),
            guard=state.get("guard"),
            diagnosis=state.get("diagnosis") or state.get("empty_diagnosis"),
            agent_steps=list(state.get("agent_steps") or []),
            suggestions=list(state.get("suggestions") or []),
            usage=dict(state.get("usage") or {}),
            trace=list(state.get("trace") or []),
            backend=self.backend.name,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            error=state.get("error"),
        )
