from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from text2sql.agent.llm import (
    HTTPChatLLM,
    LLMError,
    LLMResponse,
    OpenAICompatibleLLM,
    ScriptedLLM,
    ToolCall,
    ToolsNotSupportedError,
    build_llm,
    extract_sql,
    parse_chat_completion,
    tool_call_response,
)
from text2sql.config import LLMSettings

SETTINGS = LLMSettings(
    provider="openai_compatible",
    base_url="https://relay.example/v1",
    api_key="k",
    model="grok-4.5",
    timeout_s=30,
)


def _completion(content="", tool_calls=None, prompt_tokens=120, completion_tokens=30):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
        model="grok-4.5",
    )


class _FakeClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _llm(outcomes, sleeps=None):
    client = _FakeClient(outcomes)
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return client

    llm = OpenAICompatibleLLM(
        SETTINGS,
        client_factory=factory,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )
    return llm, client, captured


def test_client_is_configured_from_settings_without_sdk_retries():
    _, _, captured = _llm([_completion("ok")])

    assert captured == {
        "base_url": "https://relay.example/v1",
        "api_key": "k",
        "timeout": 30,
        "max_retries": 0,
    }


def test_chat_sends_tools_and_parses_tool_calls_and_usage():
    call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="preview_sql", arguments=json.dumps({"sql": "SELECT 1"})),
    )
    llm, client, _ = _llm([_completion(tool_calls=[call])])

    response = llm.chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "preview_sql"}}],
    )

    assert client.requests[0]["tool_choice"] == "auto"
    assert client.requests[0]["model"] == "grok-4.5"
    assert response.tool_calls == [
        ToolCall(
            id="call_1",
            name="preview_sql",
            arguments={"sql": "SELECT 1"},
            raw_arguments='{"sql": "SELECT 1"}',
        )
    ]
    assert (
        response.usage.prompt_tokens == 120
        and response.usage.completion_tokens == 30
        and response.usage.calls == 1
    )


def test_invalid_tool_arguments_do_not_crash():
    call = SimpleNamespace(
        id="c", function=SimpleNamespace(name="submit_sql", arguments="{not json")
    )
    llm, _, _ = _llm([_completion(tool_calls=[call])])

    response = llm.chat([{"role": "user", "content": "x"}], tools=[{}])

    assert response.tool_calls[0].arguments == {}
    assert response.tool_calls[0].raw_arguments == "{not json"


class APIConnectionError(Exception):
    pass


class BadRequestError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def test_transient_errors_are_retried_once():
    sleeps: list[float] = []
    llm, client, _ = _llm([APIConnectionError("reset"), _completion("ok")], sleeps=sleeps)

    assert llm.chat([{"role": "user", "content": "x"}]).content == "ok"
    assert len(client.requests) == 2
    assert sleeps == [1.0]


def test_repeated_transient_failure_raises_llm_error():
    llm, _, _ = _llm([APIConnectionError("reset"), APIConnectionError("reset again")])

    with pytest.raises(LLMError, match="reset again"):
        llm.chat([{"role": "user", "content": "x"}])


def test_provider_without_tool_support_is_detected():
    llm, _, _ = _llm([BadRequestError("tools is not supported by this model")])

    with pytest.raises(ToolsNotSupportedError):
        llm.chat([{"role": "user", "content": "x"}], tools=[{}])


def test_non_transient_errors_are_not_retried():
    llm, client, _ = _llm([BadRequestError("invalid api key", status_code=401)])

    with pytest.raises(LLMError, match="invalid api key"):
        llm.chat([{"role": "user", "content": "x"}])
    assert len(client.requests) == 1


def test_assistant_message_round_trips_tool_calls():
    response = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="c1", name="describe_table", arguments={"table": "招投标"})],
    )
    message = response.assistant_message()

    assert message["role"] == "assistant"
    assert message["tool_calls"][0]["function"]["name"] == "describe_table"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"table": "招投标"}


@pytest.mark.parametrize(
    ("text", "sql", "assumption"),
    [
        ("口径：按发布时间统计\n```sql\nSELECT 1 AS x\n```", "SELECT 1 AS x", "按发布时间统计"),
        (
            "```\nWITH t AS (SELECT 1 AS x) SELECT x FROM t;\n```",
            "WITH t AS (SELECT 1 AS x) SELECT x FROM t",
            None,
        ),
        (
            "这里是结果：SELECT `name` FROM `企业基本信息` LIMIT 5",
            "SELECT `name` FROM `企业基本信息` LIMIT 5",
            None,
        ),
        (
            "假设：只统计有效资质\nselect count(*) from `标签数据`",
            "select count(*) from `标签数据`",
            "只统计有效资质",
        ),
        ("我无法回答这个问题", None, None),
    ],
)
def test_extract_sql_handles_common_model_outputs(text, sql, assumption):
    assert extract_sql(text) == (sql, assumption)


def test_scripted_llm_records_calls_and_supports_handlers():
    scripted = ScriptedLLM(["first", tool_call_response("submit_sql", {"sql": "SELECT 1"})])

    assert scripted.chat([{"role": "user", "content": "a"}]).content == "first"
    second = scripted.chat([{"role": "user", "content": "b"}], tools=[{}])
    assert second.tool_calls[0].name == "submit_sql"
    assert len(scripted.calls) == 2 and scripted.calls[1]["tools"] == [{}]

    echo = ScriptedLLM(handler=lambda messages, tools: messages[-1]["content"].upper())
    assert echo.chat([{"role": "user", "content": "hi"}]).content == "HI"


# --------------------------------------------------------------------------- JSON 接口客户端

WORKERS_AI_REPLY = {
    "model": "@cf/zai-org/glm-4.7-flash",
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": "先检索相关表。",
                "tool_calls": [
                    {
                        "id": "chatcmpl-tool-1",
                        "type": "function",
                        "function": {
                            "name": "search_schema",
                            "arguments": '{"keywords": "招投标 城市"}',
                        },
                    }
                ],
            }
        }
    ],
    "usage": {"prompt_tokens": 713, "completion_tokens": 62, "total_tokens": 775},
}


def test_parse_chat_completion_reads_tool_calls_and_usage():
    response = parse_chat_completion(WORKERS_AI_REPLY)

    assert response.content == "先检索相关表。"
    assert response.tool_calls == [
        ToolCall(
            "chatcmpl-tool-1",
            "search_schema",
            {"keywords": "招投标 城市"},
            '{"keywords": "招投标 城市"}',
        )
    ]
    assert response.usage.total_tokens == 775
    assert response.usage.calls == 1
    assert response.model == "@cf/zai-org/glm-4.7-flash"


def test_http_chat_llm_posts_openai_shaped_payload():
    sent = []

    def transport(payload):
        sent.append(payload)
        return WORKERS_AI_REPLY

    llm = HTTPChatLLM(transport, model="workers-ai")
    response = llm.chat(
        [{"role": "user", "content": "q"}], tools=[{"type": "function"}], max_tokens=600
    )

    assert sent == [
        {
            "messages": [{"role": "user", "content": "q"}],
            "temperature": 0.1,
            "max_tokens": 600,
            "tools": [{"type": "function"}],
        }
    ]
    assert response.tool_calls[0].name == "search_schema"


@pytest.mark.parametrize(
    ("reply", "fragment"),
    [
        ({"error": "今日 Workers AI 免费额度已用完"}, "免费额度"),
        ({"choices": []}, "没有返回"),
    ],
)
def test_http_chat_llm_turns_error_payloads_into_llm_errors(reply, fragment):
    llm = HTTPChatLLM(lambda payload: reply)

    with pytest.raises(LLMError, match=fragment):
        llm.chat([{"role": "user", "content": "q"}])


def test_http_chat_llm_wraps_transport_failures():
    def broken(payload):
        raise OSError("network down")

    with pytest.raises(LLMError, match="network down"):
        HTTPChatLLM(broken).chat([{"role": "user", "content": "q"}])


def test_build_llm_is_none_without_settings():
    assert build_llm(None) is None
    assert isinstance(
        build_llm(SETTINGS, client_factory=lambda **kw: _FakeClient([])), OpenAICompatibleLLM
    )
