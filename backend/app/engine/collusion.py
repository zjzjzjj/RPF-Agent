"""跨投标人串通信号预计算（实施条例第 40 条 · 最小实现）。

设计约束（与 facts.py 一致）：事实与判定分离——本模块只产出**信号**，
不下"是否串通"的结论；`objective.run_check("collusion_check")` 读信号后
仍走三态（命中 → fail+need_human → conflict_flagged，**绝不自动否决**）。

信号清单（确定性、开标场级，同场每家拿到同一份）：
- price_identical   报价两两完全相同（条例 40 条"报价呈规律性差异"的退化形态）
- price_progression 报价呈等差规律（40 条第（四）项）
- shared_author     不同投标人文档作者元数据相同（40 条第（一）项的弱证据）

可扩展骨架：新增信号（文件创建时间聚集、保证金账户同源…）只需在
`compute_cross_bidder_signals` 追加——BidFacts.cross_bidder 与 run_check 分支不动。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

DEFAULT_PARAMS = {
    "min_bidders": 3,                  # 价格类信号所需的最少有效报价家数
    "equal_tol_yuan": 0.01,            # R1 视为"报价完全相同"的差额上限（元）
    "progression_rel_tol": 0.02,       # R2 等差判定：差分极差/平均差 ≤ 2%
    "progression_min_step_ratio": 0.001,  # R2 平均步长 ≥ 最低价的 0.1%（防抖动误报）
}

# 作者元数据的占位/噪声值：出现即视为"无作者信息"
_AUTHOR_PLACEHOLDERS = {"", "unknown", "n/a", "na", "作者", "佚名", "user", "admin", "-"}


def _norm_author(author: str) -> str:
    return " ".join(author.strip().split()).casefold()


def _price_identical(
    ids: list[int], prices: dict[int, Decimal | None], tol: Decimal
) -> list[tuple[int, int, Decimal]]:
    """两两差额 ≤ tol → (id_i, id_j, 金额)。"""
    hits = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            pi, pj = prices[ids[i]], prices[ids[j]]
            if pi is None or pj is None:
                continue
            if abs(pi - pj) <= tol:
                hits.append((ids[i], ids[j], pi))
    return hits


def _price_progression(
    ids: list[int], prices: dict[int, Decimal | None], params: dict
) -> tuple[list[int], list[Decimal]] | None:
    """升序报价差分近似恒定 → 视为"呈规律性差异"。命中返回 (参与家, 步长序列)。"""
    min_bidders = int(params.get("min_bidders") or DEFAULT_PARAMS["min_bidders"])
    vals = sorted({prices[i] for i in ids if prices.get(i) is not None})
    if len(vals) < max(3, min_bidders):
        return None
    diffs = [vals[k + 1] - vals[k] for k in range(len(vals) - 1)]
    mean = sum(diffs) / Decimal(len(diffs))
    if mean <= 0:
        return None
    rel_tol = Decimal(str(params.get("progression_rel_tol")
                           or DEFAULT_PARAMS["progression_rel_tol"]))
    if (max(diffs) - min(diffs)) > rel_tol * mean:
        return None
    min_step_ratio = Decimal(str(params.get("progression_min_step_ratio")
                                 or DEFAULT_PARAMS["progression_min_step_ratio"]))
    if mean < min_step_ratio * vals[0]:
        return None  # 步长过小（分/厘级抖动）不报，防误伤正常报价尾差
    return ids, diffs


def compute_cross_bidder_signals(
    prices: dict[int, Decimal | None],
    authors: dict[int, list[str]],
    params: dict | None = None,
) -> dict[int, dict]:
    """返回 {bidder_id: {"signals": [...], "detail": str, "available": bool}}。

    - prices: bidder_id → 已确认报价（create_run 前置保证 price_status='confirmed'）；
    - authors: bidder_id → 文档作者列表（解析层取到才有；缺省空）；
    - available: 任一信号源可用即 True（不可用 → run_check 走 need_human 兜底）。
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    tol = Decimal(str(p["equal_tol_yuan"]))
    min_bidders = int(p["min_bidders"])

    priced = [i for i in prices if prices.get(i) is not None]
    price_available = len(priced) >= min_bidders

    # 归一作者 → 投标人列表（噪声作者不算）
    author_map: dict[str, list[int]] = {}
    for bid_id, names in authors.items():
        for raw in names:
            key = _norm_author(raw)
            if key in _AUTHOR_PLACEHOLDERS:
                continue
            author_map.setdefault(key, []).append(bid_id)
    author_available = bool(author_map)

    identical = _price_identical(priced, prices, tol) if price_available else []
    progression = _price_progression(priced, prices, p) if price_available else None
    shared = {a: ids for a, ids in author_map.items() if len(set(ids)) >= 2}

    result: dict[int, dict] = {}
    for bid_id in set(list(prices.keys()) + list(authors.keys())):
        signals: list[str] = []
        details: list[str] = []
        if price_available:
            for a, b, amount in identical:
                if bid_id in (a, b):
                    signals.append("price_identical")
                    details.append(
                        f"报价与另一投标人完全相同（{amount} 元）"
                    )
                    break
            if progression is not None and bid_id in progression[0]:
                steps = "→".join(str(d) for d in progression[1][:6])
                signals.append("price_progression")
                details.append(f"报价呈等差规律（步长 {steps}）")
        if bid_id in {i for ids in shared.values() for i in ids}:
            signals.append("shared_author")
            hit = [a for a, ids in shared.items() if bid_id in ids]
            details.append(f"文档作者与他人相同：{'、'.join(hit)}")
        available = price_available or author_available
        result[bid_id] = {
            "signals": signals,
            "detail": "；".join(details),
            "available": available,
        }
    return result
