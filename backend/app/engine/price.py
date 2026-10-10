"""价格分公式引擎：纯 Python + Decimal，禁止任何 LLM 参与。

支撑 4 类公式 × 5 种基准价算法；所有中间值与结果可解释
（formula_explanation 要能直接打印进评标报告）。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.engine.models import PriceBid, PriceComputation, PriceResult

ZERO = Decimal("0")
ONE = Decimal("1")


def _q(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _fmt(value: Decimal, places: int = 2) -> str:
    return f"{_q(value, places):,.{places}f}"


# ---------------------------------------------------------------------------
# 基准价
# ---------------------------------------------------------------------------
def compute_benchmark(
    prices: list[Decimal],
    method: str = "average",
    k: Decimal = ONE,
    trim_ratio: Decimal = ZERO,
) -> tuple[Decimal, str]:
    """返回 (基准价, 计算说明)。prices 非空由调用方保证。"""
    if method == "lowest":
        base = min(prices)
        return _q(base * k, 2), f"基准价=最低有效报价{_fmt(min(prices))}×{k}={_fmt(base * k)}元"
    if method == "second_lowest":
        ordered = sorted(prices)
        base = ordered[1] if len(ordered) > 1 else ordered[0]
        return _q(base * k, 2), f"基准价=次低有效报价{_fmt(base)}×{k}={_fmt(base * k)}元"
    if method == "average_trimmed":
        ordered = sorted(prices)
        n = len(ordered)
        cut = int((n * trim_ratio))  # 两端各截去
        kept = ordered[cut : n - cut] if n - 2 * cut >= 1 else ordered
        avg = sum(kept) / Decimal(len(kept))
        return (
            _q(avg * k, 2),
            f"基准价=去掉最高最低各{cut}家后的平均值{_fmt(avg)}×{k}={_fmt(avg * k)}元",
        )
    if method == "average_below":
        avg_all = sum(prices) / Decimal(len(prices))
        below = [p for p in prices if p <= avg_all]
        avg = sum(below) / Decimal(len(below))
        return (
            _q(avg * k, 2),
            f"基准价=低于平均价（{_fmt(avg_all)}）的报价平均值{_fmt(avg)}×{k}={_fmt(avg * k)}元",
        )
    # 默认：算术平均
    avg = sum(prices) / Decimal(len(prices))
    expr = "+".join(_fmt(p) for p in sorted(prices))
    return (
        _q(avg * k, 2),
        f"基准价=({expr})/{len(prices)}×{k}={_q(avg * k, 2)}元",
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def compute_price_scores(bids: list[PriceBid], formula: dict) -> PriceComputation:
    """formula 来自规则包快照的 formula 字段。

    有效报价 = 已确认、未被否决/建议否决（exclude_invalid=true 时）、未超限价。
    建议否决的投标人**不参与基准价**，但仍计算其价格分供人工交叉验证。
    """
    max_score = Decimal(str(formula.get("max_score") or 40))
    formula_type = formula.get("formula_type") or "benchmark_ratio"
    method = formula.get("benchmark_method") or "average"
    k = Decimal(str(formula.get("k") or 1))
    trim_ratio = Decimal(str(formula.get("trim_ratio") or 0))
    upper = Decimal(str(formula["upper_bound"])) if formula.get("upper_bound") is not None else max_score
    lower = Decimal(str(formula["lower_bound"])) if formula.get("lower_bound") is not None else ZERO
    places = int(formula.get("rounding") or 2)
    exclude_invalid = bool(formula.get("exclude_invalid", True))
    deduct_rate = Decimal(str(formula.get("deduct_rate") or 0))

    in_benchmark = [b.price for b in bids if not (b.excluded and exclude_invalid)]
    if not in_benchmark:
        return PriceComputation(
            benchmark=None,
            formula_explanation="没有有效报价，无法计算基准价",
            results=[
                PriceResult(b.bidder_id, b.name, b.price, Decimal(0), Decimal(0), b.excluded,
                            "无有效报价，价格分记 0（待人工复核）")
                for b in bids
            ],
        )

    benchmark, bench_expl = compute_benchmark(in_benchmark, method, k, trim_ratio)
    parts = [bench_expl]

    results: list[PriceResult] = []
    for b in sorted(bids, key=lambda x: x.price):
        deviation = (b.price - benchmark) / benchmark * 100 if benchmark else Decimal(0)
        note = ""
        if b.excluded and exclude_invalid:
            note = "（该报价未计入基准价）"

        if formula_type in ("benchmark_ratio", "lowest_ratio"):
            raw = max_score * benchmark / b.price
        elif formula_type == "deviation_deduct":
            raw = max_score - deduct_rate * abs(deviation)
        elif formula_type == "above_deduct":
            raw = max_score - deduct_rate * max(Decimal(0), deviation)
        else:
            raise ValueError(f"未知价格公式类型：{formula_type}")

        capped = min(max(raw, lower), upper)
        score = _q(capped, places)
        if capped != raw:
            note += f"（未封顶前 {_fmt(raw, places)}，已按上下限截取）"
        parts.append(
            f"{b.name}：价格分={_fmt(max_score, places)}×{_fmt(benchmark)}/{_fmt(b.price)}"
            f"={_fmt(raw, places)}→{score}{note}"
        )
        results.append(
            PriceResult(
                bidder_id=b.bidder_id,
                name=b.name,
                price=b.price,
                price_score=score,
                deviation_pct=_q(deviation, 2),
                excluded_from_benchmark=bool(b.excluded and exclude_invalid),
                explanation="；".join(parts[-1:]),
            )
        )

    return PriceComputation(
        benchmark=benchmark,
        formula_explanation="；".join(parts),
        results=results,
    )
