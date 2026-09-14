"""LLM 客户端：OpenAI 兼容接口 + 原生 function calling。

- 瞬时错误（连接断开、超时、429、5xx）重试一次，其余错误直接抛出，不在坏请求上浪费额度；
- 服务商不支持 tools 参数时抛 ToolsNotSupportedError，由 SQL 智能体降级为单轮提示；
- 每次调用记录 token 用量与耗时，汇总进 Agent 轨迹；
- ScriptedLLM 是可编排的测试替身，测试与离线评测不访问任何外部服务。
"""

from __future__ import annotations

import itertools
import json
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from text2sql.config import LLMSettings

_TRANSIENT_ERRORS = {
    "APIConnectionError",
    "APITimeoutError",
    "RateLimitError",
    "InternalServerError",
    "ConnectError",
    "ReadTimeout",
    "Timeout",
}
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """模型调用失败。"""


class ToolsNotSupportedError(LLMError):
    """服务商或模型不支持 function calling。"""


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, other: TokenUsage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.calls += other.calls

    def to_dict(self) -> dict[str, int]:
        return {**asdict(self), "total_tokens": self.total_tokens}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str = ""
    latency_ms: float = 0.0

    def assistant_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in self.tool_calls
            ]
        return message


class LLMClient(Protocol):
    model: str

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int = 900,
    ) -> LLMResponse: ...


def _status(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _is_transient(exc: Exception) -> bool:
    return type(exc).__name__ in _TRANSIENT_ERRORS or _status(exc) in _TRANSIENT_STATUS


def _tools_unsupported(exc: Exception) -> bool:
    message = str(exc).lower()
    return _status(exc) in {400, 404, 422} and ("tool" in message or "function" in message)


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class OpenAICompatibleLLM:
    def __init__(
        self,
        settings: LLMSettings,
        *,
        client_factory: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.model = settings.model
        self._sleep = sleep
        if client_factory is None:
            from openai import OpenAI

            client_factory = OpenAI
        # SDK 自带的重试会把坏请求也重试多次，这里关掉，由下面的策略统一控制
        self._client = client_factory(
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout=settings.timeout_s,
            max_retries=0,
        )

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int = 900,
    ) -> LLMResponse:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": self.settings.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            request["tools"] = list(tools)
            request["tool_choice"] = "auto"

        started = time.perf_counter()
        for attempt in (1, 2):
            try:
                completion = self._client.chat.completions.create(**request)
                break
            except Exception as exc:  # noqa: BLE001 - SDK 异常类型随版本变化，按特征分类
                if tools and _tools_unsupported(exc):
                    raise ToolsNotSupportedError(str(exc)) from exc
                if attempt == 1 and _is_transient(exc):
                    self._sleep(1.0)
                    continue
                raise LLMError(f"模型调用失败（{type(exc).__name__}）：{exc}") from exc

        message = completion.choices[0].message
        tool_calls = [
            ToolCall(
                id=str(call.id),
                name=str(call.function.name),
                arguments=_parse_arguments(call.function.arguments),
                raw_arguments=str(call.function.arguments or ""),
            )
            for call in (getattr(message, "tool_calls", None) or [])
        ]
        usage = getattr(completion, "usage", None)
        return LLMResponse(
            content=(message.content or "").strip(),
            tool_calls=tool_calls,
            usage=TokenUsage(
                prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                calls=1,
            ),
            model=str(getattr(completion, "model", "") or self.model),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )


def parse_chat_completion(data: dict[str, Any], *, fallback_model: str = "") -> LLMResponse:
    """解析 OpenAI chat-completions 格式的 JSON（Workers AI 代理、各类兼容网关都返回这种结构）。"""
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict) or "message" not in choices[0]:
        raise LLMError("模型没有返回可用的结果")
    message = choices[0]["message"] or {}
    tool_calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw = function.get("arguments")
        raw_text = raw if isinstance(raw, str) else json.dumps(raw or {}, ensure_ascii=False)
        tool_calls.append(
            ToolCall(
                id=str(call.get("id") or f"call_{next(_call_ids)}"),
                name=str(function.get("name") or ""),
                arguments=_parse_arguments(raw_text),
                raw_arguments=raw_text,
            )
        )
    usage = data.get("usage") or {}
    return LLMResponse(
        content=str(message.get("content") or "").strip(),
        tool_calls=tool_calls,
        usage=TokenUsage(
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            calls=1,
        ),
        model=str(data.get("model") or fallback_model),
        latency_ms=float(data.get("latency_ms") or 0.0),
    )


class HTTPChatLLM:
    """经 JSON 接口调用模型：请求体是 OpenAI chat-completions 格式，传输方式由调用方注入。

    浏览器里的引擎用它经同源 Worker 调 Workers AI（Web Worker 中的同步 XHR），测试里注入假传输。
    接口返回 {"error": ...} 时抛 LLMError，由工作流转成失败结果并展示原因。
    """

    def __init__(
        self,
        transport: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        model: str = "http",
        temperature: float = 0.1,
    ) -> None:
        self._transport = transport
        self.model = model
        self.temperature = temperature

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int = 900,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "messages": list(messages),
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = list(tools)
        started = time.perf_counter()
        try:
            data = self._transport(payload)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - 传输层异常类型取决于运行环境
            raise LLMError(f"模型调用失败（{type(exc).__name__}）：{exc}") from exc
        if not isinstance(data, dict):
            raise LLMError("模型接口返回的不是 JSON 对象")
        if data.get("error"):
            raise LLMError(str(data["error"]))
        response = parse_chat_completion(data, fallback_model=self.model)
        if not response.latency_ms:
            response.latency_ms = round((time.perf_counter() - started) * 1000, 2)
        return response


class ScriptedLLM:
    """按脚本返回结果的测试替身。responses 里可以放字符串、LLMResponse 或要抛出的异常。"""

    model = "scripted"

    def __init__(
        self,
        responses: Iterable[str | LLMResponse | Exception] | None = None,
        *,
        handler: Callable[[list[dict[str, Any]], list[dict[str, Any]] | None], str | LLMResponse]
        | None = None,
    ) -> None:
        self._responses = iter(responses or [])
        self._handler = handler
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int = 900,
    ) -> LLMResponse:
        snapshot = [dict(m) for m in messages]
        self.calls.append({"messages": snapshot, "tools": list(tools) if tools else None})
        if self._handler is not None:
            outcome: Any = self._handler(snapshot, list(tools) if tools else None)
        else:
            try:
                outcome = next(self._responses)
            except StopIteration as exc:
                raise LLMError("ScriptedLLM 的脚本已用完") from exc
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, LLMResponse):
            outcome.usage = (
                outcome.usage
                if outcome.usage.calls
                else TokenUsage(prompt_tokens=100, completion_tokens=20, calls=1)
            )
            return outcome
        return LLMResponse(
            content=str(outcome),
            usage=TokenUsage(prompt_tokens=100, completion_tokens=20, calls=1),
            model=self.model,
        )


_call_ids = itertools.count(1)


def tool_call_response(name: str, arguments: dict[str, Any], *, content: str = "") -> LLMResponse:
    return tool_calls_response([(name, arguments)], content=content)


def tool_calls_response(
    calls: Sequence[tuple[str, dict[str, Any]]], *, content: str = ""
) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=[
            ToolCall(
                id=f"call_{next(_call_ids)}",
                name=n,
                arguments=dict(a),
                raw_arguments=json.dumps(a, ensure_ascii=False),
            )
            for n, a in calls
        ],
    )


_ASSUMPTION = re.compile(r"^\s*(?:口径|假设)\s*[:：]\s*(.+?)\s*$", re.MULTILINE)
_FENCED = re.compile(r"```(?:sql|mysql)?\s*\n?(.*?)```", re.IGNORECASE | re.DOTALL)
_SQL_START = re.compile(r"\b(SELECT|WITH)\b", re.IGNORECASE)


def extract_sql(text: str) -> tuple[str | None, str | None]:
    """从模型的纯文本回复里取出 SQL 与口径说明。"""
    content = text or ""
    assumption_match = _ASSUMPTION.search(content)
    assumption = assumption_match.group(1) if assumption_match else None

    fenced = _FENCED.search(content)
    if fenced:
        candidate = fenced.group(1)
    else:
        start = _SQL_START.search(content)
        if not start:
            return None, assumption
        candidate = content[start.start() :].split("\n\n", 1)[0]
    sql = candidate.strip().rstrip(";").strip()
    if not _SQL_START.match(sql):
        return None, assumption
    return sql, assumption


def build_llm(settings: LLMSettings | None, **kwargs: Any) -> OpenAICompatibleLLM | None:
    return OpenAICompatibleLLM(settings, **kwargs) if settings else None
