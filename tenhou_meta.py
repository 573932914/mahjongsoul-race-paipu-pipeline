"""从天凤 tenhou.net/6 自定义 JSON 中提取四家信息。

天凤 JSON 的 log 是“每局一个 17 元数组”：
  log[i][0] = seed [局番, 本场, 供托]
  log[i][1] = 该局开始时的四家点数（座位序）
  log[i][-1] = 本局结果，如 ["和了", [四家得失], ...] / ["流局", [四家得失], ...]

注意：把所有局的得失直接累加会多出“未被取走的立直棒”（每根 1000 点）。
因此优先使用顶层 sc（[终局点数, 得失/1000] × 4）；没有 sc 时用赛事接口的
终局点数作为兜底。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TenhouMeta:
    names: list[str]
    start_scores: list[int]
    end_scores: list[int]
    deltas: list[int]
    ranks: list[int]
    rounds: int
    played_at: str = ""
    title: list[str] = field(default_factory=list)
    rule: dict = field(default_factory=dict)
    ref: str = ""


def load(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _pad(values: list, n: int = 4, default=0) -> list:
    values = list(values or [])
    return (values + [default] * n)[:n]


def extract(data: dict, fallback_end: list[int] | None = None) -> TenhouMeta:
    names = [str(x) for x in _pad(data.get("name"), 4, "")]
    log = data.get("log") or []
    if not log:
        raise ValueError("牌谱没有 log")

    first = log[0]
    start = [int(x) for x in _pad(first[1] if len(first) > 1 else [], 4, 0)]

    end: list[int] | None = None
    sc = data.get("sc")
    if isinstance(sc, list) and len(sc) >= 8:
        try:
            end = [int(sc[2 * i]) for i in range(4)]
        except (TypeError, ValueError):
            end = None
    if end is None and fallback_end is not None:
        end = [int(x) for x in _pad(fallback_end, 4, 0)]
    if end is None:
        # 最坏情况：逐局累加（可能有未取走的立直棒，仅作兜底）
        end = list(start)
        for kyoku in log:
            result = kyoku[-1] if kyoku else None
            if isinstance(result, list) and len(result) > 1 and isinstance(result[1], list) and len(result[1]) == 4:
                end = [a + int(b) for a, b in zip(end, result[1])]

    deltas = [e - s for e, s in zip(end, start)]

    order = sorted(range(4), key=lambda i: (-end[i], i))
    ranks = [0] * 4
    for rank, seat in enumerate(order, 1):
        ranks[seat] = rank

    title = data.get("title") or []
    played_at = ""
    if isinstance(title, list) and len(title) > 1:
        played_at = str(title[1])
    if not played_at:
        ref = str(data.get("ref", ""))
        if len(ref) >= 6 and ref[:6].isdigit():
            played_at = f"20{ref[0:2]}-{ref[2:4]}-{ref[4:6]}"

    return TenhouMeta(
        names=names,
        start_scores=start,
        end_scores=end,
        deltas=deltas,
        ranks=ranks,
        rounds=len(log),
        played_at=played_at,
        title=list(title) if isinstance(title, list) else [str(title)],
        rule=data.get("rule") or {},
        ref=str(data.get("ref", "")),
    )


def validate(meta: TenhouMeta, expected_names: list[str] | None = None) -> list[str]:
    """返回警告列表（不抛异常）。"""
    warnings = []
    if len(meta.names) != 4 or any(not n for n in meta.names):
        warnings.append("四家名称不完整")
    if any(s <= 0 for s in meta.start_scores):
        warnings.append(f"开局点数异常：{meta.start_scores}")
    if sum(meta.end_scores) != sum(meta.start_scores):
        warnings.append(
            f"终局点数总和 {sum(meta.end_scores)} != 开局总和 {sum(meta.start_scores)}"
            "（可能是残留守备的立直棒）"
        )
    if expected_names:
        left = sorted(meta.names)
        right = sorted(expected_names)
        if left != right:
            warnings.append(f"姓名与赛事接口不一致：{meta.names} vs {expected_names}")
    return warnings
