"""评测运行与报告。

指标：
- 覆盖率 = 作答题数 / 应答题数
- 作答精确率 = 作答且结果正确 / 作答题数（衡量“答了会不会答错”）
- 总体执行准确率（EX） = 结果正确 / 应答题数
- 拒答准确率 = 被拒答的应拒题 / 应拒题数
- Schema 召回率 = 标准答案用到的表中被召回的比例（与规划器无关，独立衡量召回）
"""

from __future__ import annotations

import json
import re
import statistics
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from text2sql.evaluation.compare import results_match
from text2sql.sql.guard import SQLGuard

ZNJZ_BENCHMARK_PATH = Path(__file__).resolve().parents[1] / "datasets" / "znjz" / "benchmark.jsonl"
MODE_LABELS = {
    "semantic": "离线语义层路径",
    "llm": "SQL 智能体路径",
    "auto": "双路径（语义层优先，模型兜底）",
}


@dataclass(frozen=True)
class BenchmarkItem:
    id: str
    question: str
    category: str
    expect: str  # answer | refuse
    gold_sql: str | None = None
    order_matters: bool = False


def load_benchmark(path: Path | str = ZNJZ_BENCHMARK_PATH) -> list[BenchmarkItem]:
    items = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            raw = json.loads(line)
            items.append(
                BenchmarkItem(
                    id=raw["id"],
                    question=raw["question"],
                    category=raw["category"],
                    expect=raw["expect"],
                    gold_sql=raw.get("gold_sql"),
                    order_matters=bool(raw.get("order_matters", False)),
                )
            )
    return items


@dataclass
class ItemOutcome:
    id: str
    question: str
    category: str
    expect: str
    status: str
    planner: str | None
    correct: bool | None = None
    refused_ok: bool | None = None
    reason: str = ""
    latency_ms: float = 0.0
    sql: str | None = None
    gold_tables: list[str] = field(default_factory=list)
    linked_tables: list[str] = field(default_factory=list)
    link_recall: float | None = None
    tokens: int = 0


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


@dataclass
class EvalSummary:
    mode: str
    total: int = 0
    answerable: int = 0
    refusals_expected: int = 0
    answered: int = 0
    correct: int = 0
    refused_ok: int = 0
    linking_recall: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    llm_calls: int = 0
    tokens: int = 0
    by_category: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        return _ratio(self.answered, self.answerable)

    @property
    def precision(self) -> float:
        return _ratio(self.correct, self.answered)

    @property
    def execution_accuracy(self) -> float:
        return _ratio(self.correct, self.answerable)

    @property
    def refusal_accuracy(self) -> float:
        return _ratio(self.refused_ok, self.refusals_expected)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            coverage=self.coverage,
            precision=self.precision,
            execution_accuracy=self.execution_accuracy,
            refusal_accuracy=self.refusal_accuracy,
        )
        return payload


def _gold_tables(sql: str) -> list[str]:
    tree = sqlglot.parse_one(sql, read="mysql")
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    return sorted({t.name for t in tree.find_all(exp.Table) if t.name not in ctes})


def run_evaluation(
    agent: Any,
    items: Iterable[BenchmarkItem],
    *,
    backend: Any,
    mode: str,
    on_item: Callable[[ItemOutcome], None] | None = None,
) -> tuple[EvalSummary, list[ItemOutcome]]:
    gold_guard = SQLGuard.from_catalog(agent.catalog, agent.schema, max_rows=5000)
    summary = EvalSummary(mode=mode)
    outcomes: list[ItemOutcome] = []
    latencies: list[float] = []
    recalls: list[float] = []

    for item in items:
        started = time.perf_counter()
        result = agent.ask(item.question)
        latency = round((time.perf_counter() - started) * 1000, 1)
        latencies.append(latency)
        linked = next(
            (e["detail"].get("tables", []) for e in result.trace if e["node"] == "link"), []
        )
        outcome = ItemOutcome(
            id=item.id,
            question=item.question,
            category=item.category,
            expect=item.expect,
            status=result.status,
            planner=result.planner,
            latency_ms=latency,
            sql=result.safe_sql or result.sql,
            linked_tables=list(linked),
            tokens=int(result.usage.get("total_tokens", 0)),
        )
        summary.llm_calls += int(result.usage.get("calls", 0))
        summary.tokens += outcome.tokens
        bucket = summary.by_category.setdefault(
            item.category,
            {"answerable": 0, "answered": 0, "correct": 0, "refusals": 0, "refused_ok": 0},
        )

        if item.expect == "refuse":
            summary.refusals_expected += 1
            bucket["refusals"] += 1
            outcome.refused_ok = result.status in ("rejected", "declined")
            outcome.reason = result.message or ""
            if outcome.refused_ok:
                summary.refused_ok += 1
                bucket["refused_ok"] += 1
        else:
            summary.answerable += 1
            bucket["answerable"] += 1
            gold_report = gold_guard.check(item.gold_sql or "")
            gold = backend.execute(gold_report.safe_sql, max_rows=5000)
            outcome.gold_tables = _gold_tables(item.gold_sql or "")
            if outcome.gold_tables:
                outcome.link_recall = round(
                    len(set(outcome.gold_tables) & set(linked)) / len(outcome.gold_tables), 4
                )
                recalls.append(outcome.link_recall)
            if result.status == "answered":
                summary.answered += 1
                bucket["answered"] += 1
                verdict = results_match(
                    gold.columns,
                    gold.rows,
                    result.columns,
                    result.rows,
                    order_matters=item.order_matters,
                )
                outcome.correct = verdict.match
                outcome.reason = verdict.reason
                if verdict.match:
                    summary.correct += 1
                    bucket["correct"] += 1
            else:
                outcome.reason = result.message or result.error or ""

        summary.total += 1
        outcomes.append(outcome)
        if on_item is not None:
            on_item(outcome)

    summary.linking_recall = round(statistics.fmean(recalls), 4) if recalls else 0.0
    if latencies:
        ordered = sorted(latencies)
        summary.latency_p50_ms = round(statistics.median(ordered), 1)
        summary.latency_p95_ms = ordered[
            min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
        ]
    return summary, outcomes


def check_gate(
    summary: EvalSummary, *, min_precision: float, min_refusal: float, min_coverage: float
) -> list[str]:
    failures = []
    if summary.answered == 0:
        failures.append("没有作答任何题目")
    elif summary.precision < min_precision:
        failures.append(f"作答精确率 {summary.precision:.1%} 低于门槛 {min_precision:.0%}")
    if summary.refusals_expected and summary.refusal_accuracy < min_refusal:
        failures.append(f"拒答准确率 {summary.refusal_accuracy:.1%} 低于门槛 {min_refusal:.0%}")
    if summary.coverage < min_coverage:
        failures.append(f"覆盖率 {summary.coverage:.1%} 低于门槛 {min_coverage:.0%}")
    return failures


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_markdown(summary: EvalSummary, outcomes: list[ItemOutcome], meta: dict[str, Any]) -> str:
    title = MODE_LABELS.get(summary.mode, summary.mode)
    lines = [
        f"### {title}",
        "",
        f"运行日期 {meta.get('date', '')}，数据源 `{meta.get('backend', '')}`"
        + (f"，模型 `{meta['model']}`" if meta.get("model") else "")
        + "。",
        "",
        "| 指标 | 数值 | 说明 |",
        "|---|---:|---|",
        f"| 题目数 | {summary.total} | 应答 {summary.answerable}，应拒 {summary.refusals_expected} |",
        f"| 覆盖率 | {_pct(summary.coverage)} | 作答题数 / 应答题数 |",
        f"| 作答精确率 | {_pct(summary.precision)} | 作答的题目中，执行结果与标准答案一致的比例 |",
        f"| 总体执行准确率（EX） | {_pct(summary.execution_accuracy)} | 结果正确的题数 / 应答题数 |",
        f"| 拒答准确率 | {_pct(summary.refusal_accuracy)} | 应拒题中被拒答的比例 |",
        f"| Schema 召回率 | {_pct(summary.linking_recall)} | 标准答案用到的表被召回的比例 |",
        f"| 单题延迟 P50 / P95 | {summary.latency_p50_ms:.0f} / {summary.latency_p95_ms:.0f} ms | 端到端，含执行 |",
        f"| 模型调用 / token | {summary.llm_calls} / {summary.tokens} | 语义层路径为 0 |",
        "",
        "| 类别 | 应答 | 作答 | 正确 | 应拒 | 正确拒答 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for category, row in summary.by_category.items():
        lines.append(
            f"| {category} | {row['answerable']} | {row['answered']} | {row['correct']} | {row['refusals']} | {row['refused_ok']} |"
        )

    problems = [
        o
        for o in outcomes
        if (o.expect == "answer" and o.correct is not True)
        or (o.expect == "refuse" and not o.refused_ok)
    ]
    if problems:
        lines += [
            "",
            "未作答、答错或未拒答的题目：",
            "",
            "| 编号 | 问题 | 状态 | 原因 |",
            "|---|---|---|---|",
        ]
        for o in problems:
            state = (
                "答错" if o.correct is False else ("未拒答" if o.expect == "refuse" else o.status)
            )
            reason = re.sub(r"\s+", " ", o.reason or "")[:80].replace("|", "\\|")
            lines.append(f"| {o.id} | {o.question} | {state} | {reason} |")
    return "\n".join(lines)


def replace_marked_section(document: str, key: str, block: str) -> str:
    begin, end = f"<!-- EVAL:{key}:BEGIN -->", f"<!-- EVAL:{key}:END -->"
    body = f"{begin}\n{block.strip()}\n{end}"
    if begin in document and end in document:
        start = document.index(begin)
        stop = document.index(end) + len(end)
        return document[:start] + body + document[stop:]
    return document.rstrip() + "\n\n" + body + "\n"
