"""结果解读：确定性解读始终生成；配置了模型时再生成自然语言解读，由反思节点核对数字出处。"""

from __future__ import annotations

from text2sql.agent.llm import LLMClient, TokenUsage
from text2sql.agent.profiling import ResultProfile
from text2sql.agent.prompts import narrative_messages

EMPTY_NARRATIVE = "- 查询没有返回数据：当前数据库中没有满足这些条件的记录。"


def deterministic_narrative(profile: ResultProfile, *, max_facts: int = 5) -> str:
    if profile.shape == "empty":
        return EMPTY_NARRATIVE
    return "\n".join(f"- {fact.text}" for fact in profile.facts[:max_facts])


def llm_narrative(
    llm: LLMClient,
    *,
    question: str,
    description: str,
    profile: ResultProfile,
    max_tokens: int = 500,
) -> tuple[str, TokenUsage]:
    response = llm.chat(
        narrative_messages(question, description, profile.facts_payload()),
        temperature=0.2,
        max_tokens=max_tokens,
    )
    return response.content.strip(), response.usage
