"""提示词。

业务口径全部来自语义层（规则、表说明、真实取值），这里只负责组织信息和写清约束。
提示词里的每条要求都对应系统里的一道真实检查：写了“只写一条 SELECT”，安全门就会拒绝其他语句；
写了“返回 0 行先怀疑取值”，preview_sql 就会带回可疑空结果诊断。模型不遵守时不会静默通过。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from text2sql.semantic.catalog import SemanticCatalog

NO_EXEMPLARS = "（本题没有相似示例）"


@dataclass
class ConversationTurn:
    question: str
    sql: str | None
    description: str | None
    assumptions: list[str] = field(default_factory=list)


@dataclass
class PromptContext:
    title: str
    description: str
    rules: tuple[str, ...]
    anchor_year: int
    max_rows: int
    schema_text: str
    exemplars_text: str = ""
    history: list[ConversationTurn] = field(default_factory=list)
    value_mentions: list[tuple[str, str, str]] = field(default_factory=list)

    @classmethod
    def from_catalog(
        cls,
        catalog: SemanticCatalog,
        *,
        schema_text: str,
        exemplars_text: str = "",
        max_rows: int = 500,
        history: list[ConversationTurn] | None = None,
        value_mentions: list[tuple[str, str, str]] | None = None,
    ) -> PromptContext:
        return cls(
            title=catalog.title,
            description=catalog.description,
            rules=catalog.rules,
            anchor_year=catalog.anchor_year,
            max_rows=max_rows,
            schema_text=schema_text,
            exemplars_text=exemplars_text,
            history=list(history or []),
            value_mentions=list(value_mentions or []),
        )


_AGENT_WORKFLOW = """# 工作方式
你可以调用工具查看表结构、字段真实取值，并在提交前试运行 SQL：
- search_schema(keywords)：按关键词找相关的表和字段
- describe_table(table)：查看表的字段、类型、口径说明和真实取值
- get_column_values(table, column, keyword?)：查看字段的真实取值，可按关键词过滤
- validate_sql(sql)：只做安全与字段检查，不执行
- preview_sql(sql)：执行并返回前 5 行，确认条件确实命中数据
- submit_sql(sql, assumptions)：提交最终 SQL，调用后结束

推荐步骤：
1. 先读「相关表」。字段含义或取值不确定时调用 describe_table / get_column_values，不要凭印象写取值。
2. 写好 SQL 后调用 preview_sql，它返回总行数和每一列的取值概况。返回 0 行时，优先怀疑条件取值写错，查真实取值后修正，而不是直接提交；结果符合预期时不要重复试运行同一条 SQL。
3. 结果符合问题后调用 submit_sql，assumptions 用中文逐条写清口径，例如按哪个时间字段、是否去重、金额口径。"""

_SINGLE_SHOT_FORMAT = """# 输出格式
先输出一行「口径：……」说明本次统计口径，再输出一个 ```sql 代码块。代码块之外不要输出其他内容。"""


def _common_sections(ctx: PromptContext) -> str:
    rules = "\n".join(f"{i}. {rule}" for i, rule in enumerate(ctx.rules, start=1))
    history = ""
    if ctx.history:
        turn = ctx.history[-1]
        history = (
            "\n\n# 对话上下文\n"
            "当前问题可能是对上一轮的追问（例如「那深圳呢」「改成按年份看」），这时在上一轮 SQL 的基础上修改；"
            "与上一轮无关时忽略本节。\n"
            f"上一轮问题：{turn.question}\n"
            f"上一轮口径：{turn.description or '无'}\n"
            f"上一轮 SQL：\n```sql\n{turn.sql or ''}\n```"
        )
    mentions = ""
    if ctx.value_mentions:
        # 值索引在问题里认出的取值：模型不必再调用 get_column_values 确认这些值是否存在
        shown = "、".join(
            f"`{table}`.{column} = '{value}'" for table, column, value in ctx.value_mentions[:8]
        )
        mentions = f"\n\n问题中出现的真实取值（已在库中确认存在）：{shown}"
    return (
        "# 硬性约束\n"
        "- 只写一条 SELECT（可以用 WITH），任何修改数据的语句都会被拒绝。\n"
        "- 只使用「相关表」或工具返回中出现过的表和字段；不要写 SELECT *；不要查询法定代表人等个人信息。\n"
        f"- 表名和中文列别名用反引号；结果最多 {ctx.max_rows} 行，明细查询要写 ORDER BY 和 LIMIT。\n"
        "- 输出列一律起中文别名，例如 `country_zh` AS `国家`、SUM(`amount`) AS `销售额`；结果表格、图表和解读直接用这些列名。\n"
        "- 不要自行添加问题没有要求的筛选、门槛或截断（例如 HAVING COUNT(*) >= 50）；问“有多少”时只返回一行总数。\n"
        f"- 数据截至 {ctx.anchor_year} 年，「近 N 年」指 {ctx.anchor_year} 年及之前共 N 个自然年。\n\n"
        f"# 业务口径\n{rules}\n\n"
        f"# 相关表\n{ctx.schema_text}{mentions}\n\n"
        f"# 参考示例（已验证可执行，借鉴结构，字段以相关表为准）\n{ctx.exemplars_text or NO_EXEMPLARS}"
        f"{history}"
    )


def _header(ctx: PromptContext) -> str:
    return f"你是资深数据分析工程师，负责把业务问题转换成 MySQL 8.0 只读查询。数据库是「{ctx.title}」：{ctx.description}"


def sql_agent_system_prompt(ctx: PromptContext) -> str:
    return f"{_header(ctx)}\n\n{_AGENT_WORKFLOW}\n\n{_common_sections(ctx)}"


def single_shot_system_prompt(ctx: PromptContext) -> str:
    return f"{_header(ctx)}\n\n{_SINGLE_SHOT_FORMAT}\n\n{_common_sections(ctx)}"


def sql_agent_user_message(question: str, feedback: str | None = None) -> str:
    message = f"问题：{question}"
    return f"{message}\n\n{feedback}" if feedback else message


def narrative_messages(
    question: str, description: str, facts: dict[str, Any]
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": "你是企业数据分析师，只根据给定的查询结果事实写结论。不得引入外部知识，不得编造或推算新的数字。",
        },
        {
            "role": "user",
            "content": (
                f"问题：{question}\n"
                f"统计口径：{description}\n"
                "结果事实（JSON）：\n"
                f"{json.dumps(facts, ensure_ascii=False, indent=2)}\n\n"
                "写 2 到 4 条中文要点：\n"
                "1. 第一条直接回答问题。\n"
                "2. 引用的每个数字都必须原样出现在结果事实中，不要自己计算百分比、增长率或平均值。\n"
                "3. 结果被截断、样本很少或存在空值时，最后一条说明局限。\n"
                "只输出 Markdown 无序列表。"
            ),
        },
    ]
