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
    *,
    trim_count: int = 0,
    trim_threshold: int = 0,
) -> tuple[Decimal, str]:
    """返回 (基准价, 计算说明)。prices 非空由调用方保证。

    trim_count/trim_threshold：绝对家数截尾（"有效投标人＞5 个时扣除一个最高价和
    一个最低价"）。trim_count > 0 时优先于 trim_ratio；仅 n > trim_threshold 才截，
    且至少保留 1 家。
    """
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
        if trim_count > 0:
            cut = trim_count if n > trim_threshold else 0
            cut = min(cut, max(0, (n - 1) // 2))  # 至少保留 1 家
            note = (
                f"有效投标人 {n} 家＞{trim_threshold}，去掉最高最低各 {cut} 家"
                if cut
                else f"有效投标人 {n} 家≤{trim_threshold}，不剔除最高最低"
            )
        else:
            cut = int(n * trim_ratio)  # 两端各截去
            note = f"去掉最高最低各{cut}家"
        kept = ordered[cut : n - cut] if n - 2 * cut >= 1 else ordered
        avg = sum(kept) / Decimal(len(kept))
        return (
            _q(avg * k, 2),
            f"基准价={note}后的平均值{_fmt(avg)}×{k}={_fmt(avg * k)}元",
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
    upper = min(upper, max_score)  # 评分项不得超过满分
    lower = Decimal(str(formula["lower_bound"])) if formula.get("lower_bound") is not None else ZERO
    places = int(formula.get("rounding") or 2)
    exclude_invalid = bool(formula.get("exclude_invalid", True))
    deduct_rate = Decimal(str(formula.get("deduct_rate") or 0))

    # 不对称扣分率：真实招标文件常见"每低 1% 扣 0.4、每高 1% 扣 0.8"这类上下不同费率，
    # 单一 deduct_rate 表达不了（浙江中烟某项目 5 家报价即为此型）。扩展参数放
    # params_json——表结构既定的扩展位。只给 deduct_rate 时两个方向都取它，
    # 行为与改动前完全一致。
    ext = formula.get("params_json") or {}
    rate_down = Decimal(str(ext.get("deduct_rate_down", deduct_rate)))
    rate_up = Decimal(str(ext.get("deduct_rate_up", deduct_rate)))
    trim_count = int(ext.get("trim_count") or 0)
    trim_threshold = int(ext.get("trim_threshold") or 0)
    if formula_type == "deviation_deduct" and rate_down == ZERO and rate_up == ZERO:
        raise ValueError(
            "偏离扣分公式缺少扣分率（deduct_rate / deduct_rate_down / deduct_rate_up）"
            "——继续计算会让所有人拿满分；请解冻规则包补全公式"
        )

    invalid_price: list[PriceResult] = []
    in_benchmark: list[Decimal] = []
    for b in bids:
        if b.price is None or b.price <= ZERO:
            invalid_price.append(
                PriceResult(b.bidder_id, b.name, b.price, ZERO, ZERO, True,
                            "报价为空或≤0，价格分记 0（待人工复核）")
            )
            continue
        if not (b.excluded and exclude_invalid):
            in_benchmark.append(b.price)
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

    benchmark, bench_expl = compute_benchmark(
        in_benchmark, method, k, trim_ratio,
        trim_count=trim_count, trim_threshold=trim_threshold,
    )
    parts = [bench_expl]

    results: list[PriceResult] = []
    for b in sorted(bids, key=lambda x: (x.price is None, x.price or ZERO)):
        if b.price is None or b.price <= ZERO:
            continue  # 已在 invalid_price 中记 0，不进入基准/比值计算
        deviation = (b.price - benchmark) / benchmark * 100 if benchmark else Decimal(0)
        note = ""
        if b.excluded and exclude_invalid:
            note = "（该报价未计入基准价）"

        if formula_type in ("benchmark_ratio", "lowest_ratio"):
            raw = max_score * benchmark / b.price
            calc = (f"价格分={_fmt(max_score, places)}×{_fmt(benchmark)}"
                    f"/{_fmt(b.price)}={_fmt(raw, places)}")
        elif formula_type == "deviation_deduct":
            rate = rate_up if deviation >= 0 else rate_down
            raw = max_score - rate * abs(deviation)
            calc = (f"偏离基准 {_fmt(deviation, 2)}%，每 1 个百分点扣 {_fmt(rate)} 分："
                    f"{_fmt(max_score, places)}−{_fmt(rate)}×{_fmt(abs(deviation), 2)}"
                    f"={_fmt(raw, places)}")
        elif formula_type == "above_deduct":
            raw = max_score - rate_up * max(Decimal(0), deviation)
            calc = (f"高于基准 {_fmt(max(Decimal(0), deviation), 2)}%，"
                    f"每 1 个百分点扣 {_fmt(rate_up)} 分：{_fmt(raw, places)}")
        else:
            raise ValueError(f"未知价格公式类型：{formula_type}")

        capped = min(max(raw, lower), upper)
        score = _q(capped, places)
        if capped != raw:
            note += f"（未封顶前 {_fmt(raw, places)}，已按上下限截取）"
        parts.append(f"{b.name}：{calc}→{score}{note}")
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
        results=results + invalid_price,
    )
