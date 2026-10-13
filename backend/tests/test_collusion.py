"""串通检测最小实现（engine/collusion.py + objective collusion_check 分支）单测。

对应计划 §八·3：R1 相同价 / R2 等差规律 / R3 同作者 / 不可用兜底 / 未注入兜底 / params 覆盖。
语义红线：命中信号 → fail+need_human → conflict_flagged（**绝不自动否决**）。
"""
from __future__ import annotations

from decimal import Decimal

from app.engine.collusion import DEFAULT_PARAMS, compute_cross_bidder_signals
from app.engine.facts import BidFacts
from app.engine.objective import run_check, verdict_for


def _facts(sig: dict) -> BidFacts:
    return BidFacts(text="", cross_bidder=sig)


def test_price_identical_signal() -> None:
    prices = {1: Decimal("1000000"), 2: Decimal("1000000"), 3: Decimal("1000000")}
    out = compute_cross_bidder_signals(prices, {})
    assert all(out[i]["available"] for i in prices)
    for i in (1, 2, 3):
        assert "price_identical" in out[i]["signals"]
        assert out[i]["detail"]
    # run_check：命中 → fail + need_human → verdict conflict_flagged（不自动否决）
    for i in (1, 2, 3):
        result = run_check("collusion_check", {}, _facts(out[i]))
        assert result.status == "fail"
        assert result.need_human is True
        verdict = verdict_for(f"c{i}", "视为串通", result, on_fail="recommend_reject")
        assert verdict == "conflict_flagged"


def test_price_progression_signal() -> None:
    # 等差 100/101/102（步长 1 元，≥ 最低价 0.1%=100 元？否——0.001*100=0.1，步长 1 ≥ 0.1 ✓）
    seq = {1: Decimal("100"), 2: Decimal("101"), 3: Decimal("102")}
    out = compute_cross_bidder_signals(seq, {})
    for i in seq:
        assert "price_progression" in out[i]["signals"], out[i]

    # 非等差：差分 3 与 8，极差 5 > 2% * 5.5 = 0.11 → 不命中
    gap = {1: Decimal("100"), 2: Decimal("103"), 3: Decimal("111")}
    out = compute_cross_bidder_signals(gap, {})
    for i in gap:
        assert "price_progression" not in out[i]["signals"], out[i]


def test_insufficient_bidders_unavailable() -> None:
    prices = {1: Decimal("500"), 2: Decimal("600")}  # 2 家 < min_bidders=3
    out = compute_cross_bidder_signals(prices, {})
    for i in prices:
        assert out[i]["available"] is False
        assert out[i]["signals"] == []
    # run_check：不可用 → unknown + need_human
    result = run_check("collusion_check", {}, _facts(out[1]))
    assert result.status == "unknown"
    assert result.need_human is True
    assert verdict_for("c", "串通", result, on_fail="recommend_reject") == "need_human"


def test_shared_author_signal() -> None:
    prices = {i: Decimal(str(1000 + i * 7)) for i in range(1, 5)}  # 不等差
    authors = {1: ["张三"], 2: ["  张三  "], 3: ["李四"]}  # 归一后 1/2 同作者
    out = compute_cross_bidder_signals(prices, authors)
    assert "shared_author" in out[1]["signals"]
    assert "shared_author" in out[2]["signals"]
    assert "shared_author" not in out[3]["signals"]
    assert "张三" in out[1]["detail"]

    # 噪声作者（占位值）不算：无有效作者 → R3 不可用；但价格源可用 → available 仍 True
    noise = compute_cross_bidder_signals(prices, {1: ["unknown"], 2: ["unknown"]})
    assert "shared_author" not in noise[1]["signals"]
    assert noise[1]["available"] is True  # 4 家报价可用


def test_empty_cross_bidder_fallback() -> None:
    # qualification_preview 等场景未注入信号 → unknown 兜底，不抛错
    result = run_check("collusion_check", {}, BidFacts(text=""))
    assert result.status == "unknown"
    assert result.need_human is True


def test_params_override() -> None:
    prices = {i: Decimal(str(100 + i)) for i in range(1, 5)}  # 4 家
    default = compute_cross_bidder_signals(prices, {})
    assert default[1]["available"] is True
    strict = compute_cross_bidder_signals(prices, {}, {"min_bidders": 5})
    assert strict[1]["available"] is False  # 4 < 5 → 价格源不可用
    # DEFAULT 不被污染
    assert DEFAULT_PARAMS["min_bidders"] == 3


def test_shared_author_via_run_check() -> None:
    """R3 命中经 run_check 的完整链路：fail + need_human。"""
    out = compute_cross_bidder_signals({}, {1: ["李四"], 2: ["李四"]})
    result = run_check("collusion_check", {}, _facts(out[1]))
    assert result.status == "fail"
    assert "同作者" in result.message or "文档作者" in result.message
