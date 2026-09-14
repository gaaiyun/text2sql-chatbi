"""SQL 智能体：用 function calling 在受控工具集里查结构、查取值、试运行，最后提交一条 SQL。

与“一次性让模型写出 SQL”相比，这个循环把 NL2SQL 最常见的静默错误放到了模型自己能看见的地方：
值条件写错时 preview_sql 返回 0 行并附带真实取值，模型在提交前就能修正。

边界：
- 每次模型调用计一步，超过 max_steps 仍未提交则失败，不无限循环；
- submit_sql 必须通过安全门，被拒绝的提交作为工具结果回给模型；
- 服务商不支持 tools 时降级为单轮提示，从文本中提取 SQL。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from text2sql.agent.llm import LLMClient, TokenUsage, ToolsNotSupportedError, extract_sql
from text2sql.agent.prompts import sql_agent_user_message
from text2sql.agent.tools import TOOL_SPECS

NUDGE = "请调用 submit_sql 提交最终 SQL；如果问题无法用现有数据回答，请直接说明原因。"
LAST_CALL = "这是最后一次调用机会：如果已有试运行结果符合问题，请立即调用 submit_sql 提交；否则直接说明无法回答的原因。"
FALLBACK_NOTE = "智能体在步数上限内没有显式提交，采用最后一次试运行成功（返回了数据）的 SQL"


_NUMBERING = re.compile(r"^\s*(?:\d+[.、)）]|[-*•])\s*")


def _as_items(value: Any) -> list[str]:
    """口径说明应是字符串数组；模型有时给一整段带编号的文本，按行拆开并去掉编号。"""
    if isinstance(value, str):
        parts = re.split(r"[\n；;]+", value)
    elif isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        parts = []
    return [text for text in (_NUMBERING.sub("", part).strip() for part in parts) if text]


@dataclass
class AgentStep:
    index: int
    tool: str
    arguments: dict[str, Any]
    ok: bool
    summary: str
    latency_ms: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SQLDraft:
    sql: str | None
    assumptions: list[str] = field(default_factory=list)
    steps: list[AgentStep] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    mode: str = "tool_calling"  # tool_calling | single_shot
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sql": self.sql,
            "assumptions": self.assumptions,
            "steps": [s.to_dict() for s in self.steps],
            "usage": self.usage.to_dict(),
            "mode": self.mode,
            "error": self.error,
        }


class SQLAgent:
    max_tool_chars = 6000

    def __init__(
        self, llm: LLMClient, toolbox: Any, *, max_steps: int = 8, max_tokens: int = 1200
    ) -> None:
        self.llm = llm
        self.toolbox = toolbox
        self.max_steps = max_steps
        self.max_tokens = max_tokens

    def draft(
        self,
        question: str,
        *,
        system_prompt: str,
        fallback_prompt: str | None = None,
        feedback: str | None = None,
        on_step: Callable[[AgentStep], None] | None = None,
    ) -> SQLDraft:
        user = sql_agent_user_message(question, feedback)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user},
        ]
        draft = SQLDraft(sql=None)
        nudged = False
        last_good_preview: str | None = None

        for step in range(self.max_steps):
            if step == self.max_steps - 1 and step > 0 and messages[-1]["role"] == "tool":
                messages.append({"role": "user", "content": LAST_CALL})
            try:
                response = self.llm.chat(
                    messages, tools=TOOL_SPECS, temperature=0.0, max_tokens=self.max_tokens
                )
            except ToolsNotSupportedError:
                return self._single_shot(fallback_prompt or system_prompt, user, draft)
            draft.usage.add(response.usage)

            if not response.tool_calls:
                sql, assumption = extract_sql(response.content)
                if sql:
                    draft.sql = sql
                    draft.assumptions = [assumption] if assumption else []
                    return draft
                if nudged:
                    draft.error = f"模型没有给出 SQL：{response.content[:200]}"
                    return draft
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": NUDGE})
                nudged = True
                continue

            messages.append(response.assistant_message())
            for call in response.tool_calls:
                started = time.perf_counter()
                if call.name == "submit_sql":
                    sql = str(call.arguments.get("sql") or "").strip()
                    report = self.toolbox.guard.check(sql)
                    if report.is_safe:
                        self._record(
                            draft, call.name, call.arguments, True, "提交最终 SQL", started, on_step
                        )
                        draft.sql = sql
                        draft.assumptions = _as_items(call.arguments.get("assumptions"))
                        return draft
                    payload = {"ok": False, "errors": report.errors, "hints": report.hints}
                    summary = f"提交被安全门拒绝：{'；'.join(report.errors)}"
                    ok = False
                else:
                    result = self.toolbox.execute(call.name, call.arguments)
                    payload, summary, ok = result.payload, result.summary, result.ok
                    if (
                        call.name == "preview_sql"
                        and ok
                        and payload.get("row_count")
                        and not payload.get("warning")
                    ):
                        last_good_preview = str(call.arguments.get("sql") or "").strip()
                self._record(draft, call.name, call.arguments, ok, summary, started, on_step)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": self._serialize(payload)}
                )

        if last_good_preview:
            # 模型把步数花在反复确认上、最后没来得及提交：试运行成功且有数据的 SQL 已过安全门，
            # 采用它并在口径里写明，后面照常经过安全门、执行和质量检查
            started = time.perf_counter()
            arguments = {"sql": last_good_preview, "auto": True}
            self._record(draft, "submit_sql", arguments, True, FALLBACK_NOTE, started, on_step)
            draft.sql = last_good_preview
            draft.assumptions = [FALLBACK_NOTE]
            return draft
        draft.error = f"智能体在 {self.max_steps} 步内没有提交 SQL"
        return draft

    def _single_shot(self, system_prompt: str, user: str, draft: SQLDraft) -> SQLDraft:
        draft.mode = "single_shot"
        response = self.llm.chat(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user}],
            tools=None,
            temperature=0.0,
            max_tokens=self.max_tokens,
        )
        draft.usage.add(response.usage)
        sql, assumption = extract_sql(response.content)
        draft.sql = sql
        draft.assumptions = [assumption] if assumption else []
        if sql is None:
            draft.error = f"模型没有给出 SQL：{response.content[:200]}"
        return draft

    @staticmethod
    def _record(
        draft: SQLDraft,
        tool: str,
        arguments: dict[str, Any],
        ok: bool,
        summary: str,
        started: float,
        on_step: Callable[[AgentStep], None] | None,
    ) -> AgentStep:
        step = AgentStep(
            index=len(draft.steps) + 1,
            tool=tool,
            arguments=dict(arguments),
            ok=ok,
            summary=summary,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        draft.steps.append(step)
        if on_step is not None:
            on_step(step)
        return step

    @classmethod
    def _serialize(cls, payload: dict[str, Any]) -> str:
        content = json.dumps(payload, ensure_ascii=False, default=str)
        if len(content) <= cls.max_tool_chars:
            return content
        return json.dumps(
            {"truncated": True, "preview": content[: cls.max_tool_chars - 40]}, ensure_ascii=False
        )
