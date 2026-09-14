"""浏览器端引擎：Web Worker 里的 Pyodide 调用这里。

与服务端相比只换了三处基础设施，节点、路由、安全门、评测都是同一份代码：
- 编排器用顺序执行（LangGraph 的原生依赖在 WebAssembly 里装不上），结果由测试核对一致；
- 数据库是随站点分发的合成演示库，DuckDB 以 WebAssembly 运行，只读；
- 模型经同源 Worker 转发到 Cloudflare Workers AI，页面不持有密钥。

演示库上两种规划方式各用一个智能体，共享数据库连接、值索引与示例库：
- semantic：只用语义层，零模型调用；
- auto：语义层优先，放弃的问题交给 SQL 智能体（结果解读仍由查询结果直接生成，节省免费额度）。

另有一个工作区智能体（workspace），承载内置开源数据集或用户上传的文件，只走 SQL 智能体；
同一时间只保留一个工作区数据集，切换时替换。

所有方法返回 JSON 字符串；事件回调接收 JSON 字符串，Worker 直接 postMessage 给页面。
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from text2sql import __version__
from text2sql.agent.exemplars import ZNJZ_EXEMPLARS_PATH, ExemplarStore
from text2sql.agent.graph import NODE_ORDER, Text2SQLAgent
from text2sql.agent.llm import HTTPChatLLM
from text2sql.agent.values import build_value_index
from text2sql.config import Settings
from text2sql.db.backends import DuckDBBackend
from text2sql.db.schema import load_znjz_schema
from text2sql.semantic.catalog import load_znjz_catalog
from text2sql.semantic.explain import explain_question

Emit = Callable[[str], Any]
Transport = Callable[[dict[str, Any]], dict[str, Any]]


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


class WebEngine:
    def __init__(
        self,
        db_path: str,
        *,
        llm_transport: Transport | None = None,
        llm_model: str = "workers-ai",
        upload_dir: str = "/uploads",
    ) -> None:
        started = time.perf_counter()
        catalog = load_znjz_catalog()
        schema = load_znjz_schema()
        self.backend = DuckDBBackend(db_path)
        self.upload_dir = Path(upload_dir)
        self._upload_backend: DuckDBBackend | None = None
        self._settings = Settings.from_mapping(
            {"T2S_DEMO_DB_PATH": db_path, "T2S_LLM_NARRATIVE": "false"}
        )
        self._llm = HTTPChatLLM(llm_transport, model=llm_model) if llm_transport else None
        shared = {
            "catalog": catalog,
            "schema": schema,
            "backend": self.backend,
            "settings": self._settings,
            "values": build_value_index(self.backend, catalog),
            "exemplars": ExemplarStore.load(ZNJZ_EXEMPLARS_PATH),
            "orchestrator": "sequential",
        }
        self.agents: dict[str, Text2SQLAgent] = {
            "semantic": Text2SQLAgent(**shared, llm=None, planners=("semantic",))
        }
        if self._llm is not None:
            self.agents["auto"] = Text2SQLAgent(
                **shared, llm=self._llm, planners=("semantic", "llm")
            )
        self.agent = self.agents["semantic"]
        self.boot_ms = round((time.perf_counter() - started) * 1000, 1)

    def load_upload(self, files: Any) -> str:
        """导入用户上传的文件，作为工作区数据集（mode="workspace"），语义层由画像自动生成。

        files 为 [[原始文件名, 文件路径], ...] 或它的 JSON 字符串；再次调用会替换之前的工作区数据集。
        """
        return self._load_workspace(files, card=None)

    def load_dataset(self, dataset_id: str, files: Any) -> str:
        """载入内置数据集（开源数据或示例数据）：导入画像之上叠加数据卡片。"""
        from text2sql.datasets.cards import load_card

        try:
            card = load_card(dataset_id)
        except KeyError as exc:
            return _dumps({"error": str(exc.args[0])})
        return self._load_workspace(files, card=card)

    def _load_workspace(self, files: Any, *, card: Any) -> str:
        from text2sql.datasets.cards import apply_card
        from text2sql.datasets.upload import (
            MAX_SCAN_DISTINCT,
            UploadError,
            build_catalog,
            build_schema,
            import_files,
            suggest_questions,
        )

        entries = json.loads(files) if isinstance(files, str) else [list(f) for f in files]
        self._close_workspace()
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        db_path = self.upload_dir / "workspace.duckdb"
        try:
            tables = import_files(db_path, [(str(name), Path(path)) for name, path in entries])
        except UploadError as exc:
            return _dumps({"error": str(exc)})

        if card is not None:
            apply_card(tables, card)
        catalog = build_catalog(tables, card=card)
        backend = DuckDBBackend(db_path)
        self._upload_backend = backend
        self.agents["workspace"] = Text2SQLAgent(
            catalog=catalog,
            schema=build_schema(tables),
            backend=backend,
            settings=self._settings,
            llm=self._llm,
            planners=("llm",),
            values=build_value_index(backend, catalog, per_column_limit=MAX_SCAN_DISTINCT),
            exemplars=ExemplarStore([]),
            orchestrator="sequential",
        )
        return _dumps(
            {
                "dataset": card.public() if card is not None else None,
                "tables": [table.to_dict() for table in tables],
                "suggestions": list(card.suggestions) if card else suggest_questions(tables),
                "sensitive": sorted(catalog.sensitive_columns),
                "anchor_year": catalog.anchor_year,
                "model": self._llm is not None,
            }
        )

    def _close_workspace(self) -> None:
        self.agents.pop("workspace", None)
        if self._upload_backend is not None:
            self._upload_backend.close()
            self._upload_backend = None

    def info(self) -> str:
        import duckdb
        import sqlglot

        status = self.agent.status()
        return _dumps(
            {
                "version": __version__,
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "duckdb": duckdb.__version__,
                "sqlglot": sqlglot.__version__,
                "orchestrator": status["orchestrator"],
                "nodes": list(NODE_ORDER),
                "modes": list(self.agents),
                "model": self.agents["auto"].llm.model if "auto" in self.agents else None,
                "value_index": status["value_index"],
                "boot_ms": self.boot_ms,
            }
        )

    def ask(
        self, question: str, thread_id: str, emit: Emit | None = None, mode: str = "semantic"
    ) -> str:
        agent = self.agents.get(mode) or self.agent
        started = time.perf_counter()
        callback = (lambda event: emit(_dumps(event))) if emit is not None else None
        state = agent.runner.run(question, thread_id, emit=callback)
        result = agent._result(state, thread_id, started).to_dict()
        if result["status"] == "answered" and not result["suggestions"]:
            # 回答之后给出可以接着问的问题：从推荐问题里按相似度挑，去掉刚问过的
            asked = {question, result["effective_question"]}
            related = [
                q for q in agent._suggest(result["effective_question"], k=5) if q not in asked
            ]
            result["suggestions"] = related[:3]
        return _dumps(result)

    def run_sql(self, sql: str, mode: str = "semantic") -> str:
        """执行页面上手工修改过的 SQL：与智能体生成的 SQL 走同一道安全门、代价预估和图表推荐。"""
        from text2sql.agent.charts import recommend_chart
        from text2sql.agent.narrative import deterministic_narrative
        from text2sql.agent.profiling import profile_result
        from text2sql.agent.repair import diagnose_cost, diagnose_execution_error
        from text2sql.db.backends import BackendError

        agent = self.agents.get(mode) or self.agent
        started = time.perf_counter()
        report = agent.guard.check(str(sql))
        payload: dict[str, Any] = {
            "sql": sql,
            "guard": report.to_dict(),
            "backend": agent.backend.name,
        }
        if not report.is_safe:
            return _dumps({**payload, "status": "rejected", "message": "；".join(report.errors)})
        cost = diagnose_cost(
            agent.backend.estimate_cost(report.safe_sql), agent.settings.cost_limit_rows
        )
        if cost is not None:
            return _dumps({**payload, "status": "rejected", "message": cost.message})
        try:
            result = agent.backend.execute(report.safe_sql, max_rows=agent.settings.max_rows)
        except BackendError as exc:
            diagnosis = diagnose_execution_error(exc, report.safe_sql)
            return _dumps({**payload, "status": "failed", "message": diagnosis.message})
        profile = profile_result(
            result.columns,
            result.rows,
            truncated=result.truncated,
            anchor_year=agent.catalog.anchor_year if agent.catalog.anchor_partial else None,
        )
        chart = recommend_chart(profile)
        return _dumps(
            {
                **payload,
                "status": "answered",
                "safe_sql": report.safe_sql,
                "executed_sql": result.executed_sql,
                "columns": result.columns,
                "rows": result.rows,
                "row_count": result.row_count,
                "truncated": result.truncated,
                "chart": chart.to_dict() if chart else None,
                "answer": deterministic_narrative(profile),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        )

    def remember(self, question: str, sql: str, mode: str = "semantic") -> str:
        """把页面上确认正确的问答加入该数据集的示例库，相似问题的提示词里会带上它。"""
        agent = self.agents.get(mode) or self.agent
        report = agent.guard.check(str(sql))
        if not report.is_safe:
            return _dumps({"error": "；".join(report.errors) or "SQL 未通过安全检查"})
        agent.exemplars.add(str(question), report.safe_sql)
        return _dumps(
            {
                "ok": True,
                "exemplars": sum(
                    1 for e in agent.exemplars.exemplars if e.id.startswith("confirmed-")
                ),
            }
        )

    def reset(self, thread_id: str) -> None:
        for agent in self.agents.values():
            agent.runner._threads.pop(thread_id, None)

    def explain(self, question: str) -> str:
        agent = self.agent
        return _dumps(
            explain_question(
                question,
                parser=agent.parser,
                catalog=agent.catalog,
                guard=agent.guard,
                max_rows=agent.settings.max_rows,
            )
        )

    def evaluate(self, emit: Emit | None = None) -> str:
        from text2sql.evaluation.runner import load_benchmark, run_evaluation

        def on_item(outcome: Any) -> None:
            if emit is not None:
                emit(_dumps(asdict(outcome)))

        summary, outcomes = run_evaluation(
            self.agent,
            load_benchmark(),
            backend=self.backend,
            mode="semantic",
            on_item=on_item,
        )
        return _dumps({"summary": summary.to_dict(), "outcomes": [asdict(o) for o in outcomes]})

    def close(self) -> None:
        self._close_workspace()
        self.backend.close()
