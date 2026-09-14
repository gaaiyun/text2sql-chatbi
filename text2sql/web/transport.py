"""Pyodide 里的 HTTP 传输：Web Worker 中的同步 XMLHttpRequest。

SQL 智能体的工具循环是同步代码。主线程禁止同步 XHR，Web Worker 允许；
等待模型返回时阻塞的只是 Worker，页面照常响应。只在 Pyodide 中可用（依赖 js 模块）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


def xhr_transport(
    url: str, *, timeout_ms: int = 90_000
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def post(payload: dict[str, Any]) -> dict[str, Any]:
        from js import XMLHttpRequest  # Pyodide 提供的浏览器对象

        xhr = XMLHttpRequest.new()
        xhr.open("POST", url, False)
        xhr.timeout = timeout_ms
        xhr.setRequestHeader("Content-Type", "application/json")
        xhr.send(json.dumps(payload, ensure_ascii=False))
        text = str(xhr.responseText or "")
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {"error": text[:200] or f"HTTP {xhr.status}"}
        if xhr.status != 200 and not data.get("error"):
            data = {"error": f"模型接口返回 HTTP {xhr.status}"}
        return data

    return post
