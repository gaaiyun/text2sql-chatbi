"""顺序编排器：没有 LangGraph 的环境（浏览器里的 Pyodide）按同一张路由表执行同一组节点。

状态合并规则与 LangGraph 相同：history 按列表追加（对应 Annotated[list, operator.add]），
其余字段整体覆盖。与 LangGraph 编排的区别只在基础设施：会话状态存在进程内字典里，不做持久化；
事件通过回调同步发出——在 Web Worker 里调用 postMessage，页面就能实时看到每个节点完成。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from text2sql.agent.graph import (
    CONDITIONAL_EDGES,
    END_NODE,
    FIXED_EDGES,
    LOCAL_STREAM_WRITER,
    MAX_STEPS,
)

Emit = Callable[[dict[str, Any]], None]
_APPEND_KEYS = ("history",)


class SequentialRunner:
    def __init__(self, agent: Any, *, max_steps: int = MAX_STEPS) -> None:
        self.agent = agent
        self.max_steps = max_steps
        self._threads: dict[str, dict[str, Any]] = {}

    def run(self, question: str, thread_id: str, *, emit: Emit | None = None) -> dict[str, Any]:
        state = self._threads.setdefault(thread_id, {})
        state["question"] = question
        token = LOCAL_STREAM_WRITER.set(emit) if emit is not None else None
        try:
            node, steps = "understand", 0
            while node != END_NODE:
                steps += 1
                if steps > self.max_steps:
                    raise RecursionError(f"工作流超过 {self.max_steps} 步仍未结束，最后停在 {node}")
                updates = getattr(self.agent, f"_node_{node}")(state)
                self._merge(state, updates)
                if emit is not None and state.get("trace"):
                    emit({"type": "node", **state["trace"][-1]})
                node = self._next(node, state)
        finally:
            if token is not None:
                LOCAL_STREAM_WRITER.reset(token)
        return state

    def history(self, thread_id: str) -> list[dict[str, Any]]:
        return list(self._threads.get(thread_id, {}).get("history") or [])

    @staticmethod
    def _merge(state: dict[str, Any], updates: dict[str, Any]) -> None:
        for key, value in updates.items():
            if key in _APPEND_KEYS:
                state[key] = list(state.get(key) or []) + list(value or [])
            else:
                state[key] = value

    @staticmethod
    def _next(node: str, state: dict[str, Any]) -> str:
        if node in FIXED_EDGES:
            return FIXED_EDGES[node]
        allowed = CONDITIONAL_EDGES.get(node)
        if allowed is None:
            raise ValueError(f"路由表里没有节点 {node}")
        route = state.get("route")
        if route not in allowed:
            raise ValueError(f"节点 {node} 给出的路由 {route} 不在允许的目标 {allowed} 中")
        return route
