# -*- coding: utf-8 -*-
"""价格公式原文锚定、截尾口径与"原文 vs 执行参数"校验（P0-2）。

测试 run113 暴露的三处机制缺陷：
1. 评分项自身含扣分句时，编译窗口早退，丢失同段的门槛/二次平均/超倍剔除句；
2. 截尾门槛"≥N 家"被当作"> N 家"（恰好 N 家时漏截）；
3. 二次平均缺明示门槛时静默默认 6；且冻结前无"原文 vs 执行参数"比对。

全部数值均为合成值（句式取自评标细则常见写法），不取任何项目的原文数值或答案（红线）。
"""
from __future__ import annotations

import copy
from decimal import Decimal

from app.engine.models import PriceBid
from app.engine.price import compute_benchmark, compute_price_scores
from app.rules.compiler import (
    enrich_extracted,
    formula_source_window,
    parse_price_formula_text,
)
from app.rules.model import ExtractedCriterion
from app.rules.validators import run_validators

D = Decimal

# 合成价格条款：门槛 75%（技术部分 40 分 → 30 分）、≥5 家首均去 1 高 1 低再二次平均、
# 扣分率 0.3 / 0.5、超 2 倍剔除重算。扣分句在中段，门槛句与离群"注"分居其前后。
FORMULA_CLUSTER = (
    "（1）商务报价的有效报价需满足以下条件：技术得分需达到技术部分总分的75%即30分及以上，"
    "未达到的不进入最佳报价值的计算。"
    "（2）最佳报价值：若有效投标人≥5个，则全部有效投标人最终报价扣除一个最高价和一个最低价后，"
    "剩余有效投标人评标价的算术平均值为第一次算术平均值。对低于第一次算术平均值的有效投标价再进行算术平均值计算，"
    "得出“最佳报价值”。若有效投标人<5个，则全部有效投标人最终报价的算术平均值为第一次算术平均值。"
    "对低于第一次算术平均值的有效投标价再进行算术平均值计算，得出“最佳报价值”。"
    "（3）商务报价得分计算：①等于最佳报价值的，得50分；②投标人报价低于最佳报价值时，每下浮一个百分点扣0.3分；"
    "③投标人报价高于最佳报价值时，每上浮一个百分点扣0.5分。"
    "注：有效投标价格>最佳报价*2或<最佳报价/2，其商务报价得分为0分，剔除该报价后重新计算得出最佳报价值。"
)


def _price_criterion(own: str) -> ExtractedCriterion:
    return ExtractedCriterion(category="price", name="价格", max_score=D("50"), score_type="formula",
                              description=own or None, quote=own or None)


# --- 1. 原文锚定：扣分句为锚，取公式段；无关扣分句不串入 -------------------------------

def test_formula_window_covers_gate_rates_and_outlier_note():
    window = formula_source_window("前言。" + FORMULA_CLUSTER + "后记。")
    assert "不进入最佳报价值的计算" in window
    assert "每下浮一个百分点扣0.3分" in window
    assert "剔除该报价后重新计算" in window


def test_formula_window_ignores_distant_unrelated_rate_sentence():
    tender = "付款条件：逾期付款的，每低于一个百分点按日计息。" + "无关条款" * 400 + FORMULA_CLUSTER
    window = formula_source_window(tender)
    assert "最佳报价值" in window
    assert "逾期付款" not in window


def test_formula_window_empty_without_rate_anchor():
    assert formula_source_window("本项目无价格扣分条款。") == ""


# --- 2. 编译口径：自身已含扣分句时仍须补全同段公式要素（run113 早退缺陷回归） ----------

def test_enrich_keeps_gate_outlier_and_both_rates_when_own_text_has_one_rate():
    [c] = enrich_extracted([_price_criterion("每上浮一个百分点扣0.5分。")], FORMULA_CLUSTER)
    assert c.formula_type == "deviation_deduct"
    assert c.benchmark_method == "average_trimmed_then_below"
    assert c.deduct_rate_up == D("0.5")
    assert c.deduct_rate_down == D("0.3")  # 下浮扣分率来自锚定段（自身文本没有）
    assert c.trim_threshold == 5 and c.params["trim_threshold_op"] == "ge"
    assert c.params["benchmark_gate"]["min_ratio"] == "75"
    assert c.params["benchmark_gate"]["stated_min_score"] == "30"
    assert c.params["outlier_policy"]["max_mult"] == "2"
    assert c.params["outlier_policy"]["min_mult"] == "0.5"
    assert "最佳报价值" in c.params["_price_source"]


# --- 3. 截尾门槛口径：≥N 与 >N 必须区分；无明示则不猜 ---------------------------------

def test_trim_threshold_operator_follows_stated_wording():
    ge = parse_price_formula_text("若有效投标人≥5个，扣除一个最高价和一个最低价后的算术平均值为基准价。")
    gt = parse_price_formula_text("有效投标人超过5家时，扣除一个最高价和一个最低价后的算术平均值为基准价。")
    assert (ge["trim_threshold"], ge["trim_threshold_op"]) == (5, "ge")
    assert (gt["trim_threshold"], gt["trim_threshold_op"]) == (5, "gt")


def test_two_stage_average_without_stated_count_has_no_default_threshold():
    text = ("剩余有效投标人报价的算术平均值为第一次算术平均值。"
            "对低于第一次算术平均值的有效投标价再进行算术平均值计算，得出最佳报价值。"
            "每下浮一个百分点扣0.3分；每上浮一个百分点扣0.5分。")
    parsed = parse_price_formula_text(text)
    assert parsed["benchmark_method"] == "average_trimmed_then_below"
    assert "trim_threshold" not in parsed


def test_engine_inclusive_threshold_trims_exactly_at_n():
    prices = [D("100"), D("110"), D("120"), D("130"), D("400")]  # n = 5
    trimmed, note = compute_benchmark(prices, "average_trimmed", D("1"),
                                      trim_count=1, trim_threshold=5, trim_threshold_op="ge")
    untrimmed, _ = compute_benchmark(prices, "average_trimmed", D("1"),
                                     trim_count=1, trim_threshold=5, trim_threshold_op="gt")
    assert trimmed == D("120.00")  # 去 1 高 1 低：(110+120+130)/3
    assert untrimmed == D("172.00")  # 不截：(100+110+120+130+400)/5
    assert "≥5" in note


def test_engine_inclusive_threshold_does_not_trim_below_n():
    prices = [D("100"), D("110"), D("120"), D("400")]  # n = 4 < 5
    benchmark, _ = compute_benchmark(prices, "average_trimmed", D("1"),
                                     trim_count=1, trim_threshold=5, trim_threshold_op="ge")
    assert benchmark == D("182.50")


def test_price_scores_honor_inclusive_threshold_from_formula_params():
    formula = {"formula_type": "deviation_deduct", "benchmark_method": "average_trimmed", "k": "1",
               "max_score": "50", "rounding": 2,
               "params_json": {"deduct_rate_down": "0.3", "deduct_rate_up": "0.5",
                               "trim_count": "1", "trim_threshold": "5", "trim_threshold_op": "ge"}}
    bids = [PriceBid(i, f"投标人{i}", p) for i, p in enumerate(
        [D("100"), D("110"), D("120"), D("130"), D("400")], start=1)]
    computed = compute_price_scores(bids, formula)
    assert computed.benchmark == D("120.00")


# --- 4. 冻结前"原文 vs 执行参数"校验 -----------------------------------------------

def _consistent_ext() -> dict:
    p = parse_price_formula_text(FORMULA_CLUSTER)
    return {"deduct_rate_down": str(p["deduct_rate_down"]), "deduct_rate_up": str(p["deduct_rate_up"]),
            "trim_count": str(p["trim_count"]), "trim_threshold": str(p["trim_threshold"]),
            "trim_threshold_op": p["trim_threshold_op"], "benchmark_gate": p["benchmark_gate"],
            "outlier_policy": p["outlier_policy"], "_price_source": formula_source_window(FORMULA_CLUSTER)}


def _snapshot(ext: dict, tech_total: str = "40") -> dict:
    own = "每上浮一个百分点扣0.5分。"
    price = {"code": "P1", "name": "价格", "category": "price", "score_type": "formula", "max_score": "50",
             "is_veto": False, "source_quote": own, "description": own,
             "formula": {"formula_type": "deviation_deduct", "benchmark_method": "average_trimmed_then_below",
                         "k": "1", "params_json": ext}}
    tech = {"code": "T1", "name": "技术", "category": "technical", "score_type": "count",
            "max_score": tech_total, "is_veto": False, "source_quote": "技术"}
    return {"package": {"total_max": 100}, "criteria": [price, tech]}


def _codes(findings, code: str) -> list:
    return [f for f in findings if f.code == code]


def test_consistent_parameters_raise_no_source_mismatch():
    findings = run_validators(_snapshot(_consistent_ext()))
    assert not _codes(findings, "PRICE_FORMULA_SOURCE_MISMATCH")
    assert not _codes(findings, "PRICE_GATE_BASE_MISMATCH")
    assert not _codes(findings, "PRICE_PARAMS_INCOMPLETE")


def test_symmetric_rates_against_asymmetric_source_is_an_error():
    ext = _consistent_ext()
    ext["deduct_rate_down"] = ext["deduct_rate_up"]
    found = _codes(run_validators(_snapshot(ext)), "PRICE_FORMULA_SOURCE_MISMATCH")
    assert found and found[0].severity == "error"


def test_missing_gate_and_outlier_are_source_mismatches():
    for key in ("benchmark_gate", "outlier_policy"):
        ext = _consistent_ext()
        ext.pop(key)
        found = _codes(run_validators(_snapshot(ext)), "PRICE_FORMULA_SOURCE_MISMATCH")
        assert found and found[0].severity == "error", key


def test_operator_flip_is_a_source_mismatch():
    ext = _consistent_ext()
    ext["trim_threshold_op"] = "gt"
    assert _codes(run_validators(_snapshot(ext)), "PRICE_FORMULA_SOURCE_MISMATCH")


def test_two_stage_without_threshold_is_incomplete():
    ext = _consistent_ext()
    ext.pop("trim_threshold")
    ext.pop("trim_threshold_op")
    found = _codes(run_validators(_snapshot(ext)), "PRICE_PARAMS_INCOMPLETE")
    assert found and found[0].severity == "error"


def test_stated_gate_score_checked_against_category_base_as_warning():
    # 原文写明即 30 分（75% × 40）；技术类别合计只有 36 → 27 分，不一致 → 告警（类别归位嫌疑）
    found = _codes(run_validators(_snapshot(_consistent_ext(), tech_total="36")), "PRICE_GATE_BASE_MISMATCH")
    assert found and found[0].severity == "warn"
    assert not _codes(run_validators(_snapshot(_consistent_ext(), tech_total="40")), "PRICE_GATE_BASE_MISMATCH")
