"""入口意图识别：在任何模型调用和 SQL 生成之前，挡掉写操作、提示词注入和域外问题。

这一层故意用确定性规则：它是安全边界的一部分，不能被一段精心构造的问题“说服”。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from text2sql.semantic.catalog import SemanticCatalog
from text2sql.semantic.parser import normalize_question

_WRITE = re.compile(
    r"(删除|删掉|清空|清除|修改|更改|改成|改为|插入|写入|新增一条|添加一条|增加一条|创建表|建表|导入数据|"
    r"\bdrop\b|\bdelete\b|\bupdate\b|\binsert\b|\btruncate\b|\balter\b|\bgrant\b|\breplace\s+into\b)",
    re.IGNORECASE,
)
_WRITE_FALSE_POSITIVE = re.compile(r"(更新时间|最近更新|数据更新|更新日期)")
_INJECTION = re.compile(
    r"(忽略(之前|以上|上面|前面|先前)?的?(所有|全部)?(指令|规则|提示|设定|约束)|"
    r"ignore\s+(all\s+|any\s+)?(previous|prior|above|the\s+above)?\s*instructions|"
    r"system\s*prompt|系统提示词|你的提示词|提示词内容|扮演|越狱|jailbreak|数据库密码|api\s*key|密钥)",
    re.IGNORECASE,
)
# 通用的个人身份信息说法；数据集自己的敏感字段（语义层 sensitive_columns 的中文名与同义词）另外从目录里取
_PERSONAL = re.compile(
    r"(身份证|证件号|手机号|家庭住址|个人电话|私人电话|法定代表人|法人代表|自然人股东)"
)
_TOP_N = re.compile(r"(?:top|排名前|排行前|前)(\d{1,3})")

_TASK_CUES = (
    ("share", ("占比", "比例", "比重", "份额", "百分比")),
    (
        "ranking",
        ("top", "最多", "最高", "最大", "最少", "最低", "排名", "排行", "前10", "哪家", "哪个"),
    ),
    (
        "trend",
        ("趋势", "每年", "逐年", "历年", "变化", "走势", "按年", "各年", "每月", "各月", "月度"),
    ),
    ("list", ("哪些", "名单", "清单", "列出", "明细", "列表")),
    ("distribution", ("分布", "构成", "各", "按", "不同", "每个")),
)


@dataclass
class Intent:
    kind: str  # query | write_request | injection | personal_info | out_of_domain | empty
    task: str = "aggregate"  # aggregate | distribution | ranking | trend | share | list | detail
    top_n: int | None = None
    signals: list[str] = field(default_factory=list)
    message: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _domain_vocabulary(catalog: SemanticCatalog) -> tuple[str, ...]:
    cached = getattr(catalog, "_domain_vocabulary_cache", None)
    if cached is None:
        cached = _build_vocabulary(catalog)
        catalog._domain_vocabulary_cache = cached
    return cached


def _build_vocabulary(catalog: SemanticCatalog) -> tuple[str, ...]:
    words: set[str] = set(catalog.domain_words)
    for entity in catalog.entities.values():
        words.update(entity.synonyms)
        words.add(entity.label)
    for metric in catalog.metrics.values():
        words.update(metric.synonyms)
    for dimension in catalog.dimensions.values():
        words.update(dimension.synonyms)
        words.add(dimension.label)
    for flt in catalog.filters.values():
        words.update(flt.synonyms)
    for value_map in catalog.value_maps.values():
        words.update(value_map.surfaces())
    for table in catalog.tables.values():
        words.update(table.synonyms)
        words.add(table.name)
        for column in table.columns.values():
            words.update(column.synonyms)
            words.add(column.label)
    words.update(catalog.scoped_dimension_words)
    normalized = {normalize_question(w) for w in words}
    return tuple(sorted((w for w in normalized if len(w) >= 2), key=len, reverse=True))


def _sensitive_terms(catalog: SemanticCatalog) -> tuple[str, ...]:
    cached = getattr(catalog, "_sensitive_terms_cache", None)
    if cached is None:
        words = {
            word
            for table in catalog.tables.values()
            for column in table.columns.values()
            if catalog.is_sensitive(table.name, column.name)
            for word in (column.label, *column.synonyms)
        }
        cached = tuple(w for w in {normalize_question(w) for w in words} if len(w) >= 2)
        catalog._sensitive_terms_cache = cached
    return cached


def classify_intent(question: str, catalog: SemanticCatalog) -> Intent:
    text = (question or "").strip()
    topics = catalog.domain_topics or "表里的数据"
    if not text:
        return Intent(kind="empty", message=f"请输入一个关于{catalog.title or '数据'}的问题")

    if _INJECTION.search(text):
        return Intent(
            kind="injection",
            message=f"这个问题试图改变系统规则或索取敏感信息，已拒绝。可以问{topics}等数据问题。",
        )
    if _WRITE.search(text) and not _WRITE_FALSE_POSITIVE.search(text):
        return Intent(
            kind="write_request",
            message="本系统是只读分析助手，不会修改、删除或写入任何数据；可以换成查询或统计类问题。",
        )

    normalized = normalize_question(text)
    personal = _PERSONAL.search(text)
    term = (
        personal.group(0)
        if personal
        else next((word for word in _sensitive_terms(catalog) if word in normalized), None)
    )
    if term:
        # 在规划之前拒绝：模型路径可能会换成其他字段“凑”一个答案，这类问题不应进入任何规划器
        return Intent(
            kind="personal_info",
            message=f"问题涉及个人信息（{term}），本系统不查询、不展示个人信息。可以改问{topics}等统计问题。",
        )

    signals = [word for word in _domain_vocabulary(catalog) if word in normalized]
    if not signals and not catalog.open_domain:
        return Intent(
            kind="out_of_domain",
            message=f"问题与{catalog.title}无关。可以问{topics}等。",
        )

    top = _TOP_N.search(normalized)
    task = "aggregate"
    if re.search(r"[“\"「『].{2,60}(公司|集团|研究院)[”\"」』]", text) or re.search(
        r"(基本信息|详情|概况)", text
    ):
        task = "detail"
    else:
        for name, cues in _TASK_CUES:
            if any(cue in normalized for cue in cues):
                task = name
                break
    return Intent(
        kind="query", task=task, top_n=int(top.group(1)) if top else None, signals=signals[:12]
    )
