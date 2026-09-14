"""HTTP API。

    POST /api/v1/query            提问；带 thread_id 时可以追问
    POST /api/v1/query/stream     同一流程的 SSE 流：每完成一个节点、每次工具调用推送一条事件
    GET  /api/v1/threads/{id}     会话历史（LangGraph 检查点里的逐轮记录）
    GET  /api/v1/catalog          语义层概要
    GET  /api/v1/examples         示例问题
    GET  /health                  运行状态，不需要口令
    POST /api/agent/query         v1 兼容接口，n8n 工作流仍在调用

智能体内部是同步阻塞调用（DuckDB / PyMySQL / OpenAI SDK），所以路由写成普通 def，由 FastAPI 放进线程池。
流式接口把整个 LangGraph 流放在一个后台线程里跑，通过队列交给事件循环：同步生成器如果被线程池
逐次推进，会在不同线程之间恢复，节点里依赖上下文变量的流写入器会失效。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Path, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from text2sql import __version__
from text2sql.config import Settings

logger = logging.getLogger("text2sql.api")

THREAD_ID_PATTERN = r"^[A-Za-z0-9_\-]{1,64}$"
PasswordHeader = Annotated[str | None, Header(description="配置了 APP_PASSWORD 时必填")]


class QueryRequest(BaseModel):
    question: str = Field(
        min_length=1,
        max_length=500,
        description="自然语言问题",
        examples=["广州市存续企业有多少家"],
    )
    thread_id: str | None = Field(
        default=None,
        pattern=THREAD_ID_PATTERN,
        description="会话 ID。同一 ID 下的提问共享上下文，可以追问“那深圳呢”；不传则为单轮问答",
        examples=["demo-1"],
    )


class LegacyQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    scenario: str = Field(
        default="data_insight", description="v1 场景标识，v2 起由智能体自动判断，原样返回"
    )
    password: str | None = Field(
        default=None, description="v1 访问口令，也可以放在 X-App-Password 请求头"
    )


def _sse(event: dict[str, Any]) -> str:
    data = json.dumps(event, ensure_ascii=False, default=str)
    return f"event: {event.get('type', 'message')}\ndata: {data}\n\n"


def create_app(
    settings: Settings | None = None,
    *,
    agent_factory: Callable[[Settings], Any] | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()

    def default_factory(current: Settings) -> Any:
        from text2sql.agent.graph import Text2SQLAgent

        return Text2SQLAgent.from_settings(current)

    factory = agent_factory or default_factory

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 启动时就构建智能体：演示库生成、值索引扫描都放在这里，而不是第一个请求里
        app.state.agent = await asyncio.to_thread(factory, settings)
        try:
            yield
        finally:
            app.state.agent.close()

    app = FastAPI(
        title="Text2SQL 数据分析智能体",
        version=__version__,
        description="智能制造企业数据库的自然语言问数接口：语义层编译与 SQL 智能体双路径，"
        "所有 SQL 都经过只读安全门、代价预估与结果质量检查。",
        lifespan=lifespan,
    )
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-App-Password"],
        allow_credentials=False,
    )

    @app.exception_handler(RateLimitExceeded)
    async def rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=429, content={"detail": f"请求过于频繁，限制为 {settings.rate_limit}"}
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        error_id = uuid.uuid4().hex[:12]
        logger.exception("未处理的异常 error_id=%s path=%s", error_id, request.url.path)
        return JSONResponse(
            status_code=500, content={"detail": "服务内部错误", "error_id": error_id}
        )

    def authorize(provided: str | None) -> None:
        expected = settings.app_password
        if expected and not (
            provided and hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
        ):
            raise HTTPException(status_code=401, detail="访问口令错误")

    def agent_of(request: Request) -> Any:
        return request.app.state.agent

    # ------------------------------------------------------------------ 元数据

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {"service": "text2sql-analysis", "docs": "/docs", "health": "/health"}

    @app.get("/health", tags=["meta"], summary="运行状态")
    def health(request: Request) -> dict[str, Any]:
        status = agent_of(request).status()
        return {
            "status": "ok",
            "version": __version__,
            "backend": status["backend"],
            "planners": status["planners"],
            "llm": status["llm"],
            "sql_agent": status["sql_agent"],
            "nodes": len(status["nodes"]),
        }

    @app.get("/api/v1/catalog", tags=["meta"], summary="语义层概要")
    def catalog(request: Request, x_app_password: PasswordHeader = None) -> dict[str, Any]:
        authorize(x_app_password)
        return agent_of(request).catalog.summary()

    @app.get("/api/v1/examples", tags=["meta"], summary="示例问题")
    def examples(request: Request, x_app_password: PasswordHeader = None) -> list[dict[str, Any]]:
        authorize(x_app_password)
        agent = agent_of(request)
        return [
            {
                "question": e.question,
                "category": e.category,
                "requires_llm": e.requires_llm,
                "available": not e.requires_llm or agent.sql_agent is not None,
            }
            for e in agent.catalog.examples
        ]

    # ------------------------------------------------------------------ 问答

    @app.post("/api/v1/query", tags=["query"], summary="提问")
    @limiter.limit(settings.rate_limit)
    def query(
        request: Request, body: QueryRequest, x_app_password: PasswordHeader = None
    ) -> dict[str, Any]:
        authorize(x_app_password)
        return agent_of(request).ask(body.question, thread_id=body.thread_id).to_dict()

    @app.post(
        "/api/v1/query/stream",
        tags=["query"],
        summary="流式提问（SSE）",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    @limiter.limit(settings.rate_limit)
    async def query_stream(
        request: Request, body: QueryRequest, x_app_password: PasswordHeader = None
    ) -> StreamingResponse:
        authorize(x_app_password)
        agent = agent_of(request)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        def produce() -> None:
            try:
                for event in agent.stream(body.question, thread_id=body.thread_id):
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception:  # noqa: BLE001 - 流已经开始，只能以事件的形式告知客户端
                error_id = uuid.uuid4().hex[:12]
                logger.exception("流式问答失败 error_id=%s", error_id)
                event = {"type": "error", "detail": "服务内部错误", "error_id": error_id}
                loop.call_soon_threadsafe(queue.put_nowait, event)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=produce, name="text2sql-stream", daemon=True).start()

        async def events() -> AsyncIterator[str]:
            while (event := await queue.get()) is not None:
                yield _sse(event)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/threads/{thread_id}", tags=["query"], summary="会话历史")
    def thread_history(
        request: Request,
        thread_id: Annotated[str, Path(pattern=THREAD_ID_PATTERN)],
        x_app_password: PasswordHeader = None,
    ) -> dict[str, Any]:
        authorize(x_app_password)
        return {"thread_id": thread_id, "turns": agent_of(request).history(thread_id)}

    # ------------------------------------------------------------------ v1 兼容

    @app.post("/api/agent/query", tags=["compat"], summary="v1 兼容接口")
    @limiter.limit(settings.rate_limit)
    def legacy_query(
        request: Request, body: LegacyQueryRequest, x_app_password: PasswordHeader = None
    ) -> dict[str, Any]:
        authorize(body.password or x_app_password)
        payload = agent_of(request).ask(body.question).to_dict()
        payload["scenario"] = body.scenario
        return payload

    return app
