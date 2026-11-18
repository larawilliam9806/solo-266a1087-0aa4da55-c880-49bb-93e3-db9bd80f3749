"""结算单与银行到账的最优匹配算法。

规则：
- 一笔银行到账可对应同商户、同到账日的 1~3 张结算单；
- 对应结算单金额（分）合计必须等于该笔到账金额；
- 每条记录（结算单/到账）在一次方案中最多使用一次；
- 目标：使"对上的结算单数量"最多。

由于匹配被限制在 (商户, 到账日) 组内，各组相互独立，
全局最优 = 各组最优之和。每组内用 DFS + 记忆化搜索，
并收集至多 `max_solutions` 个不同的最优方案，用于歧义判定。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations


@dataclass(frozen=True)
class Record:
    id: str
    merchant: str
    arrival_date: str  # ISO 格式日期字符串 YYYY-MM-DD
    amount_cents: int


@dataclass(frozen=True)
class MatchGroup:
    deposit_id: str
    settlement_ids: tuple[str, ...]
    merchant: str
    arrival_date: str
    amount_cents: int


@dataclass
class Solution:
    matches: list[MatchGroup]
    matched_settlement_count: int
    matched_settlement_ids: frozenset[str]
    matched_deposit_ids: frozenset[str]


def _candidate_combos(deposit_amount: int, amounts: list[int]) -> list[tuple[int, ...]]:
    """枚举某笔到账的所有可行结算单组合（1~3 张、合计相等），返回下标组合。"""
    n = len(amounts)
    out: list[tuple[int, ...]] = []
    for r in (1, 2, 3):
        if r > n:
            break
        for combo in combinations(range(n), r):
            if amounts[combo[0]] + (amounts[combo[1]] if r > 1 else 0) + (
                amounts[combo[2]] if r > 2 else 0
            ) == deposit_amount:
                out.append(combo)
    return out


def _group_optimal_matchings(
    deposits: list[Record], slips: list[Record], limit: int = 2
) -> list[tuple[tuple[int, tuple[int, ...]], ...]]:
    """求单个 (商户, 到账日) 组内至多 `limit` 个不同的最优匹配。

    每个匹配表示为 ((到账下标, 结算单下标组合), ...)，按到账下标排序。
    """
    n_dep = len(deposits)
    amounts = [s.amount_cents for s in slips]
    # 每笔到账的候选组合：(结算单位掩码, 下标组合)
    cand_masks: list[list[tuple[int, tuple[int, ...]]]] = []
    for d in deposits:
        entries = []
        for combo in _candidate_combos(d.amount_cents, amounts):
            mask = 0
            for i in combo:
                mask |= 1 << i
            entries.append((mask, combo))
        cand_masks.append(entries)

    @lru_cache(maxsize=None)
    def best(di: int, used: int) -> int:
        """从第 di 笔到账开始、已占用结算单位掩码为 used 时，最多还能对上的结算单数。"""
        if di == n_dep:
            return 0
        res = best(di + 1, used)  # 该笔到账不匹配
        for mask, combo in cand_masks[di]:
            if not mask & used:
                v = len(combo) + best(di + 1, used | mask)
                if v > res:
                    res = v
        return res

    solutions: list[tuple[tuple[int, tuple[int, ...]], ...]] = []

    def collect(di: int, used: int, path: list[tuple[int, tuple[int, ...]]]) -> None:
        if len(solutions) >= limit:
            return
        if di == n_dep:
            solutions.append(tuple(path))
            return
        target = best(di, used)
        # 选择 1：该笔到账不匹配
        if best(di + 1, used) == target:
            collect(di + 1, used, path)
        # 选择 2：该笔到账匹配某个候选组合
        for mask, combo in cand_masks[di]:
            if len(solutions) >= limit:
                return
            if not mask & used and len(combo) + best(di + 1, used | mask) == target:
                path.append((di, combo))
                collect(di + 1, used | mask, path)
                path.pop()

    collect(0, 0, [])
    return solutions


def analyze(
    deposits: list[Record], slips: list[Record], max_solutions: int = 2
) -> tuple[bool, list[Solution]]:
    """对未确认记录做最优匹配分析。

    返回 (是否歧义, 最优方案列表)。无歧义时列表含 1 个方案；
    存在多个不同的最优方案时返回 2 个不同的方案（不自动确认）。
    """
    dep_by_key: dict[tuple[str, str], list[Record]] = defaultdict(list)
    slip_by_key: dict[tuple[str, str], list[Record]] = defaultdict(list)
    for d in deposits:
        dep_by_key[(d.merchant, d.arrival_date)].append(d)
    for s in slips:
        slip_by_key[(s.merchant, s.arrival_date)].append(s)

    # 每个可同时存在到账与结算单的 (商户, 到账日) 组，求其最优匹配列表
    per_group: list[list[list[MatchGroup]]] = []
    for key in sorted(set(dep_by_key) & set(slip_by_key)):
        ds = sorted(dep_by_key[key], key=lambda r: r.id)
        ss = sorted(slip_by_key[key], key=lambda r: r.id)
        matchings = _group_optimal_matchings(ds, ss, limit=max_solutions)
        group_solutions: list[list[MatchGroup]] = []
        for matching in matchings:
            groups = [
                MatchGroup(
                    deposit_id=ds[di].id,
                    settlement_ids=tuple(sorted(ss[i].id for i in combo)),
                    merchant=ds[di].merchant,
                    arrival_date=ds[di].arrival_date,
                    amount_cents=ds[di].amount_cents,
                )
                for di, combo in matching
            ]
            group_solutions.append(groups)
        per_group.append(group_solutions)

    ambiguous = any(len(g) > 1 for g in per_group)

    def compose(alt_index: int | None) -> Solution:
        matches: list[MatchGroup] = []
        for i, group_solutions in enumerate(per_group):
            pick = 1 if (alt_index is not None and i == alt_index) else 0
            matches.extend(group_solutions[pick])
        matched_slips = frozenset(sid for m in matches for sid in m.settlement_ids)
        matched_deps = frozenset(m.deposit_id for m in matches)
        return Solution(
            matches=sorted(matches, key=lambda m: m.deposit_id),
            matched_settlement_count=len(matched_slips),
            matched_settlement_ids=matched_slips,
            matched_deposit_ids=matched_deps,
        )

    solutions = [compose(None)]
    if ambiguous:
        alt = next(i for i, g in enumerate(per_group) if len(g) > 1)
        solutions.append(compose(alt))
    return ambiguous, solutions
