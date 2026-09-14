"""执行准确率（EX）的结果集比对。

判定口径：
- 忽略列名和列顺序，按“列取值一致”对齐标准答案的每一列；预测结果多出的列不计较（例如多返回了企业 ID）；
- 数值按业务精度（两位小数）比较，避免 ROUND 与否造成的误判；
- 标准答案对行顺序有要求（Top N、趋势）时按顺序比较，否则按多重集合比较。
"""

from __future__ import annotations

import itertools
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

_MAX_ASSIGNMENTS = 2000


@dataclass
class MatchResult:
    match: bool
    reason: str = ""


def normalize_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, Decimal)):
        number = round(float(value), 2)
        return 0.0 if number == 0 else number
    return str(value).strip()


def _sort_key(value: Any) -> tuple[int, Any]:
    if value is None:
        return (0, "")
    if isinstance(value, float):
        return (1, value)
    return (2, str(value))


def _matrix(columns: Sequence[str], rows: Sequence[Any]) -> list[tuple[Any, ...]]:
    matrix = []
    for row in rows:
        values = [row.get(c) for c in columns] if isinstance(row, dict) else list(row)
        matrix.append(tuple(normalize_cell(v) for v in values))
    return matrix


def results_match(
    gold_columns: Sequence[str],
    gold_rows: Sequence[Any],
    pred_columns: Sequence[str],
    pred_rows: Sequence[Any],
    *,
    order_matters: bool = False,
) -> MatchResult:
    gold = _matrix(gold_columns, gold_rows)
    pred = _matrix(pred_columns, pred_rows)
    if len(gold) != len(pred):
        return MatchResult(False, f"行数不同：标准答案 {len(gold)} 行，预测 {len(pred)} 行")
    if not gold:
        return MatchResult(True)

    width_gold, width_pred = len(gold[0]), len(pred[0])
    if width_pred < width_gold:
        return MatchResult(False, f"预测结果只有 {width_pred} 列，少于标准答案的 {width_gold} 列")

    def signature(matrix: list[tuple[Any, ...]], index: int) -> tuple[Any, ...]:
        return tuple(sorted((row[index] for row in matrix), key=_sort_key))

    gold_signatures = [signature(gold, i) for i in range(width_gold)]
    pred_signatures = [signature(pred, j) for j in range(width_pred)]
    candidates = [
        [j for j in range(width_pred) if pred_signatures[j] == gold_signatures[i]]
        for i in range(width_gold)
    ]
    if any(not options for options in candidates):
        missing = [
            gold_columns[i] if i < len(gold_columns) else str(i)
            for i, options in enumerate(candidates)
            if not options
        ]
        return MatchResult(False, f"标准答案的列 {missing} 在预测结果中找不到取值一致的列")

    target = gold if order_matters else Counter(gold)
    for assignment in itertools.islice(itertools.product(*candidates), _MAX_ASSIGNMENTS):
        if len(set(assignment)) != len(assignment):
            continue
        projected = [tuple(row[j] for j in assignment) for row in pred]
        if (projected if order_matters else Counter(projected)) == target:
            return MatchResult(True)
    return MatchResult(False, "各列取值一致，但行的组合或顺序与标准答案不同")
