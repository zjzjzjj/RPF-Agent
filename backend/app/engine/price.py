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
def _tiers_cut(n: int, tiers: list) -> int:
    """分档截尾查表（P1-A）：tiers=[[bound|None, cut], ...] 升序、末档 None。

    结构语义 = 取**最小的 bound ≥ n** 档的 cut（None 档兜底）。
    例 [[5,0],[10,1],[None,2]]：n=3→0、n=8→1、n=12→2。
    """
    for bound, cut in tiers:
        if bound is None or n <= int(bound):
            return int(cut)
    return 0


def compute_benchmark(
    prices: list[Decimal],
    method: str = "average",
    k: Decimal = ONE,
    trim_ratio: Decimal = ZERO,
    *,
    trim_count: int = 0,
    trim_threshold: int = 0,
    trim_tiers: list | None = None,
) -> tuple[Decimal, str]:
    """返回 (基准价, 计算说明)。prices 非空由调用方保证。

    trim_count/trim_threshold：绝对家数截尾（"有效投标人＞5 个时扣除一个最高价和
    一个最低价"）。trim_count > 0 时优先于 trim_ratio；仅 n > trim_threshold 才截，
    且至少保留 1 家。

    trim_tiers：按有效家数分档截尾（P1-A，"≤5 不截 / 5<n≤10 去 1+1 / >10 去 2+2"），
    非空时优先于 trim_count/trim_threshold——语料 4/21 份为此形态（老代 15/30 边界、
    ×0.95 簇 11/15/10 边界），旧近似口径会系统性算错档位。
    """
    if method == "lowest":
        base = min(prices)
        return _q(base * k, 2), f"基准价=最低有效报价{_fmt(min(prices))}×{k}={_fmt(base * k)}元"
    if method == "second_lowest":
        ordered = sorted(prices)
        # 2026-10-28：n=2 时取最低价（宁波空调箱项目 p90 原文："进入报价评审的
        # 投标人只有2家时，取2家投标人中的最低投标报价作为最佳报价值"）——
        # 旧实现取 ordered[1]（较高者），与原文相反，直接改变价格分。
        base = ordered[0] if len(ordered) == 2 else (
            ordered[1] if len(ordered) > 1 else ordered[0]
        )
        return _q(base * k, 2), f"基准价=次低有效报价{_fmt(base)}×{k}={_fmt(base * k)}元"
    if method == "average_trimmed":
        ordered = sorted(prices)
        n = len(ordered)
        if trim_tiers:
            cut = _tiers_cut(n, trim_tiers)
            cut = min(cut, max(0, (n - 1) // 2))  # 至少保留 1 家
            note = f"有效投标人 {n} 家命中分档规则，去掉最高最低各 {cut} 家"
        elif trim_count > 0:
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
    if method == "average_trimmed_then_below":
        # 复合二次平均（实验室设备/第二批检测/重招）：≥N 家首均先去 1 高 1 低，
        # 再对低于首均者二次算术平均；<N 家不去极值直接首均。
        ordered = sorted(prices)
        n = len(ordered)
        if trim_count > 0 and n > trim_threshold:
            cut = min(trim_count, max(0, (n - 1) // 2))
            first_pool = ordered[cut : n - cut]
            first_note = f"有效投标人 {n} 家＞{trim_threshold}，首均前去掉最高最低各 {cut} 家"
        else:
            first_pool = ordered
            first_note = f"有效投标人 {n} 家≤{trim_threshold}，首均不去极值"
        first = sum(first_pool) / Decimal(len(first_pool))
        below = [p for p in prices if p <= first]
        below = below or [first]
        avg = sum(below) / Decimal(len(below))
        return (
            _q(avg * k, 2),
            f"基准价=第一次算术平均（{first_note}）={_fmt(first)}；"
            f"对低于首均的 {len(below)} 家报价二次平均={_fmt(avg)}×{k}={_fmt(avg * k)}元",
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
def _gate_threshold(gate: dict) -> Decimal | None:
    """门槛阈值：percent → min_ratio（百分数，配合 gate_basis 使用）；absolute → 绝对分。"""
    try:
        ratio = Decimal(str(gate.get("min_ratio")))
    except Exception:  # noqa: BLE001
        return None
    return ratio if ratio > 0 else None


def _gate_fail(
    gate: dict, b: "PriceBid"
) -> bool | None:
    """门槛判定（P1-C）。True=未达标 False=达标 None=不可判定（不执行，机器看不见≠不符合）。

    - tech/tech_qualification/biz_tech：gate_value 与阈值比较（percent 需 gate_basis 分母）；
    - sample_or_composite：样品明确 False（sample_pass）或合计低于绝对分数线即 fail，
      样品未知时只看合计线（未知不致害）。
    """
    ratio = _gate_threshold(gate)
    if ratio is None:
        return None
    kind = gate.get("ratio_kind")
    on = gate.get("gate_on") or "tech"
    if on == "sample_or_composite":
        if b.sample_pass is False:
            return True
        if b.gate_value is None:
            return None
        if kind == "absolute":
            return b.gate_value < ratio
        if b.gate_basis is None:
            return None
        return b.gate_value < b.gate_basis * ratio / Decimal(100)
    if b.gate_value is None:
        return None
    if kind == "absolute":
        return b.gate_value < ratio
    if b.gate_basis is None:
        return None
    return b.gate_value < b.gate_basis * ratio / Decimal(100)


def compute_price_scores(bids: list[PriceBid], formula: dict) -> PriceComputation:
    """formula 来自规则包快照的 formula 字段。

    有效报价 = 已确认、未被否决/建议否决（exclude_invalid=true 时）、未超限价。
    建议否决的投标人**不参与基准价**，但仍计算其价格分供人工交叉验证。

    P1 执行链（全部参数来自快照，红线：无默认阈值）：
    - P1-A trim_tiers 分档截尾（优先于单档近似）；
    - P1-C benchmark_gate 门槛（exclude：剔出基准仍计分；half_max：赋满分×比率短路）；
      门槛分不可得（None）→ 不执行并注明人工复核；
    - P1-B outlier_policy 离群保护（超倍→0 分→剔除后重算基准价，仅一轮）；
      参数缺失 → 不执行并注明人工复核。
    """
    max_score = Decimal(str(formula.get("max_score") or 40))
    formula_type = str(formula.get("formula_type") or "").strip()
    if not formula_type:
        # 2026-10-28：公式未识别（空 formula_type）→ 全员"不可计算"，不产出数字。
        # 旧实现 `or "benchmark_ratio"` 会把未识别静默回退成可计算类型，
        # 违反"识别不出即阻断"（validator PRICE_FORMULA_UNRECOGNIZED 的配套）。
        note = "价格公式未识别（formula_type 缺失），价格分不可计算——需人工确认公式"
        return PriceComputation(
            benchmark=None,
            formula_explanation=note,
            results=[
                PriceResult(b.bidder_id, b.name, b.price, ZERO, ZERO,
                            bool(b.excluded and exclude_invalid),
                            f"{b.name}：{note}")
                for b in bids
            ],
        )
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
    trim_tiers = ext.get("trim_tiers") or None
    gate = ext.get("benchmark_gate") if isinstance(ext.get("benchmark_gate"), dict) else None
    policy = ext.get("outlier_policy") if isinstance(ext.get("outlier_policy"), dict) else None
    lot_weights = ext.get("lot_weights") or None
    if formula_type == "deviation_deduct" and rate_down == ZERO and rate_up == ZERO:
        raise ValueError(
            "偏离扣分公式缺少扣分率（deduct_rate / deduct_rate_down / deduct_rate_up）"
            "——继续计算会让所有人拿满分；请解冻规则包补全公式"
        )

    invalid_price: list[PriceResult] = []
    valid: list[PriceBid] = []
    for b in bids:
        if b.price is None or b.price <= ZERO:
            invalid_price.append(
                PriceResult(b.bidder_id, b.name, b.price, ZERO, ZERO, True,
                            "报价为空或≤0，价格分记 0（待人工复核）")
            )
            continue
        valid.append(b)

    # ---- P1-C 门槛：失败者剔出基准价（half_max 另按赋半分计） ----
    gate_state: dict[int, bool | None] = {}
    gate_notes: list[str] = []
    gate_unknown = False
    if gate:
        # 前置条件（规则原文"有效家数超过N家才…"）：有效家数不足 → 门槛不生效
        min_valid = gate.get("min_valid_bidders")
        if min_valid is not None:
            n_valid = len([b for b in valid if not (b.excluded and exclude_invalid)])
            if n_valid < int(min_valid):
                gate_notes.append(
                    f"有效投标家数 {n_valid} 未超过门槛（< {min_valid} 家），按规则原文不执行70%门槛"
                )
                gate = None
    if gate:
        for b in valid:
            fail = _gate_fail(gate, b)
            gate_state[b.bidder_id] = fail
            if fail is None:
                gate_unknown = True
        if gate_unknown:
            gate_notes.append(
                "部分投标人门槛所需分不可得，未因门槛排除（人工复核）"
            )
        n_fail = sum(1 for v in gate_state.values() if v)
        # 全数排除护栏：门槛把所有投标人都排掉 → 基准价不存在、全员价格分 0
        # （规则本意是剔低分者，全剔=规则自相矛盾/门槛输入失真）→ 不执行门槛，
        # 按门槛前口径计算并注明（2026-10-27 实证：主观分被目录页压低 → 全员
        # <70% → 7 家全剔 → 价格分全 0 的灾难）。
        if n_fail and n_fail == len(valid):
            gate_notes.append(
                f"门槛将排除全部 {len(valid)} 家（无一达到 {gate.get('min_ratio')}% 要求），"
                "规则自相矛盾/分数输入异常——不执行门槛，按门槛前口径计算基准价（人工复核）"
            )
            gate = None
            gate_state = {}
            n_fail = 0
        if n_fail:
            on_fail = gate.get("on_fail") or "exclude"
            gate_notes.append(
                f"{n_fail} 家未达门槛（{gate.get('gate_on')} ≥ {gate.get('min_ratio')}"
                f"{'%' if gate.get('ratio_kind') == 'percent' else '分'}），"
                + ("按细则赋满分报价分的 "
                   if on_fail == "half_max" else "按细则不进入基准价计算")
                + (f"{gate.get('half_max_ratio')}%" if on_fail == "half_max" else "")
            )

    def _in_pool(b: PriceBid) -> bool:
        if b.excluded and exclude_invalid:
            return False
        if gate and gate_state.get(b.bidder_id) is True:
            return False  # 门槛失败剔出基准价（两种 on_fail 都不进基准）
        return True

    in_benchmark: list[Decimal] = [b.price for b in valid if _in_pool(b)]
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
        trim_tiers=trim_tiers,
    )
    parts = [bench_expl] + gate_notes

    # ---- P1-B 离群保护：超倍/半价 → 0 分 → 剔除后重算基准价（仅一轮） ----
    outlier_ids: set[int] = set()
    max_mult = Decimal(str(policy["max_mult"])) if policy and policy.get("max_mult") else None
    min_mult = Decimal(str(policy["min_mult"])) if policy and policy.get("min_mult") else None
    if policy and (max_mult is not None or min_mult is not None) and benchmark > ZERO:
        for b in valid:
            if not _in_pool(b):
                continue
            if (max_mult is not None and b.price > benchmark * max_mult) or (
                min_mult is not None and b.price < benchmark * min_mult
            ):
                outlier_ids.add(b.bidder_id)
        if outlier_ids:
            survivors = [b.price for b in valid
                         if _in_pool(b) and b.bidder_id not in outlier_ids]
            if survivors:
                names = "、".join(b.name for b in valid if b.bidder_id in outlier_ids)
                parts.append(
                    f"离群保护（{names}）：报价超出基准价"
                    f"[{min_mult if min_mult is not None else '—'},"
                    f"{max_mult if max_mult is not None else '—'}]倍区间，价格分记 0 并剔除"
                )
                benchmark, bench_expl2 = compute_benchmark(
                    survivors, method, k, trim_ratio,
                    trim_count=trim_count, trim_threshold=trim_threshold,
                    trim_tiers=trim_tiers,
                )
                parts.append(f"剔除后重算{bench_expl2}")
    elif policy and max_mult is None and min_mult is None:
        parts.append("离群保护参数不全，未执行离群剔除（人工复核）")

    # ---- 计分 ----
    results: list[PriceResult] = []
    for b in sorted(valid, key=lambda x: x.price or ZERO):
        note = ""
        if b.excluded and exclude_invalid:
            note = "（该报价未计入基准价）"
        if gate and gate_state.get(b.bidder_id) is True:
            note += "（未达基准价门槛）"

        # P1-C half_max：赋满分×比率直接短路，不按偏离计算
        if gate and gate_state.get(b.bidder_id) is True and (gate.get("on_fail") == "half_max"):
            half = max_score * (Decimal(str(gate.get("half_max_ratio") or 0)) / Decimal(100))
            capped = min(max(half, lower), upper)
            score = _q(capped, places)
            parts.append(
                f"{b.name}：未达门槛，报价分=满分{_fmt(max_score, places)}×"
                f"{gate.get('half_max_ratio')}%={_fmt(capped, places)}→{score}{note}"
            )
            results.append(
                PriceResult(b.bidder_id, b.name, b.price, score, ZERO,
                            bool(b.excluded and exclude_invalid),
                            parts[-1])
            )
            continue

        # P1-B 离群者：0 分（已剔除出重算后的基准价）
        if b.bidder_id in outlier_ids:
            parts.append(f"{b.name}：报价为离群值，价格分记 0{note}")
            results.append(
                PriceResult(b.bidder_id, b.name, b.price, ZERO, ZERO,
                            bool(b.excluded and exclude_invalid),
                            parts[-1])
            )
            continue

        deviation = (b.price - benchmark) / benchmark * 100 if benchmark else Decimal(0)

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
            # 2026-10-28：未知公式类型不再抛异常（会崩整场评分），改为该投标人
            # 价格分记"不可计算"并显式标注，交人工——与"识别不出即阻断"原则一致。
            parts.append(f"{b.name}：价格公式类型「{formula_type}」不可计算（需人工确认公式参数）")
            results.append(
                PriceResult(
                    bidder_id=b.bidder_id, name=b.name, price=b.price,
                    price_score=ZERO, deviation_pct=ZERO,
                    excluded_from_benchmark=bool(b.excluded and exclude_invalid),
                    explanation=parts[-1],
                )
            )
            continue

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
                explanation=parts[-1],
            )
        )

    if lot_weights:
        # P1-D 占位：多产品加权未执行，按单包总价口径近似（校验器同步告警）
        parts.append("多产品加权（lot_weights）未实现，按单包总价口径近似计算——人工复核")

    return PriceComputation(
        benchmark=benchmark,
        formula_explanation="；".join(parts),
        results=results + invalid_price,
    )
