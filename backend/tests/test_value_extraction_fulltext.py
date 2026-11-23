# -*- coding: utf-8 -*-
"""包A（2026-11-19）：承诺字段全文抽取升级 + "抽取失败≠结论0"。

覆盖：
a) 送审窗口抽不到、全文文本命中（含"30 分钟内到场"单位换算）→ 出正确分，
   且全程零 OCR/零视觉/零 LLM 调用；
b) 锚词命中但正则解析不出 → 残差 LLM 抽值轮（mock）：抽值 + 逐字引文 +
   数值复算校验通过才出分；
c) 全文也抽不到 → fallback kind=value_extraction_unresolved，未定分、
   不是"不符合"结论、不进 final_score 定格；
d) confirmed_absent + 细则制裁句 → 仍按 0 分结论（反向锁，禁合并）；
e) BID_EVAL_VALUE_EXTRACT_LLM=0 → 残差轮一次都不调用。
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.engine.auto_review import AutoReviewDecision
from app.engine.facts import BidFacts
from app.engine.material_inventory import build_inventory
from app.engine.objective import (
    _extract_response_hours, score_value_tier, value_anchor_snippets,
)
from app.engine.objective_fastpath import build_objective_plan, score_objective
from app.llm.base import LLMError
from app.llm.gateway import Gateway
from app.parsing import paddle_client
from app.services import automation_service as automation
from app.services import automatic_scoring_service as scoring

# 档位来自细则口径（≤1 小时 4 分、≤4 小时 2 分）：30 分钟若被误读成 30 小时
# 会不落任何档 → 0，因此该档位同时锁死单位换算。
TIERS = [{"max_value": "1", "score": "4"}, {"max_value": "4", "score": "2"}]


def _criterion(**param_overrides):
    params = {
        "engine": "value_tier", "field": "response_hours",
        "direction": "lower_better", "tiers": [dict(t) for t in TIERS],
    }
    params.update(param_overrides)
    return {
        "code": "c60", "name": "重大故障到场响应时间", "score_type": "count",
        "max_score": "4",
        "description": "重大故障响应时间不超过1小时得4分，不超过4小时得2分。",
        "params": params,
    }


def _forbid_calls(monkeypatch, store):
    """OCR/视觉/LLM 客户端零调用断言：任何一次触达都直接炸测试。"""
    def _boom(*_args, **_kwargs):
        store.append("forbidden")
        raise AssertionError("承诺值确定性抽取路径不得触达 OCR/视觉/LLM 客户端")

    monkeypatch.setattr(scoring, "_supplement_pages", _boom)
    monkeypatch.setattr(paddle_client, "remote_ocr_page", _boom)
    monkeypatch.setattr(Gateway, "chat", _boom)
    monkeypatch.setattr(Gateway, "chat_json", _boom)


# ---- a) 窗口/选段抽不到 → 全文语料命中（单位换算） --------------------------

def test_window_miss_fulltext_hit_minutes_conversion(monkeypatch):
    forbidden: list = []
    _forbid_calls(monkeypatch, forbidden)
    criterion = {
        "max_score": Decimal("4"),
        # 送审窗口只有索引指针，没有承诺值
        "value_window_text": "索引指针：服务承诺见投标文件第 60 页",
        # 全文语料（服务层注入的该家文本层）里承诺真实存在
        "fulltext_corpus": "投标服务承诺：我方承诺重大故障30分钟内到场，24小时内恢复。",
    }
    facts = BidFacts(text="送审选段文本里没有任何承诺值")
    result = score_value_tier(criterion, _criterion()["params"], facts)
    assert result.unresolved is None
    # 30 分钟 = 0.5 小时 ≤ 1 → 4 分（若当 30 小时算则不落档 → 0，锁死换算）
    assert result.score == Decimal("4")
    assert "0.5" in result.detail
    assert forbidden == []


def test_fastpath_scores_promise_from_fulltext_without_ocr_or_llm(monkeypatch):
    forbidden: list = []
    _forbid_calls(monkeypatch, forbidden)
    pages = [{
        "document_id": 1, "page_no": 3, "source": "text",
        "text": "服务承诺：我方承诺重大故障30分钟内到场，24小时内恢复系统。",
    }]
    inventory = build_inventory(1, pages)
    criterion = _criterion()
    assert build_objective_plan(criterion) is not None
    result = score_objective(criterion, inventory)
    assert result is not None
    assert result.zero_reason is None
    assert result.verdict == "pass"
    assert result.score == Decimal("4")
    assert forbidden == []


# ---- b) 残差 LLM 抽值轮（mock） --------------------------------------------

UNPARSEABLE = "投标服务承诺\n响应时间：按甲方要求确定（通常4小时内）\n"


def _anchor_hit_inventory():
    pages = [{"document_id": 1, "page_no": 9, "source": "text", "text": UNPARSEABLE}]
    return pages, build_inventory(1, pages)


def test_deterministic_extraction_fails_but_anchor_hits():
    """(b)(c) 前置：该文本锚词命中、正则解析不出 → 快路径标 value_extraction_unresolved。"""
    pages, inventory = _anchor_hit_inventory()
    result = score_objective(_criterion(), inventory)
    assert result is not None
    assert result.zero_reason == "value_extraction_unresolved"
    assert result.verdict == "concern"  # 不再是 "fail"/不符合
    assert value_anchor_snippets("response_hours", UNPARSEABLE)


def test_residual_llm_round_extracts_value_quote_and_recompute():
    pages, inventory = _anchor_hit_inventory()
    criterion = _criterion()
    quote = "响应时间：按甲方要求确定（通常4小时内）"
    calls: list = []

    class _FakeGateway:
        def chat_json(self, system, user, out_model, **kwargs):
            calls.append((system, user))
            return out_model(value="4", quote=quote), None

    outcome = scoring._value_extraction_residual(
        criterion, pages, inventory, lambda: _FakeGateway())
    assert len(calls) == 1
    assert outcome is not None
    assert outcome.basis == "deterministic"
    # 复算：4 小时 ≤ 4 → 2 分（档位来自冻结细则，不是模型给的分）
    assert outcome.score == Decimal("2")
    assert outcome.raw["verified_facts"]["response_hours"] == "4"
    assert quote in outcome.raw["verified_facts"]["response_hours_quote"]
    assert outcome.evidence and outcome.evidence[0]["quote"] == quote
    # 引文逐字落页证（9 页）
    assert outcome.evidence[0]["page_no"] == 9
    assert "复算通过" in outcome.reasoning


def test_residual_rejects_value_not_recomputable_from_quote():
    """引文复算不过（值与引文对不上）→ 不出分，交调用方落暂定兜底。"""
    pages, inventory = _anchor_hit_inventory()

    class _BadGateway:
        def chat_json(self, system, user, out_model, **kwargs):
            # 引文写 4 小时却报 8 小时——复算必须拦下
            return out_model(value="8", quote="响应时间：按甲方要求确定（通常4小时内）"), None

    outcome = scoring._value_extraction_residual(
        _criterion(), pages, inventory, lambda: _BadGateway())
    assert outcome is None


def test_residual_rejects_quote_missing_from_bid_text():
    pages, inventory = _anchor_hit_inventory()

    class _FakeGateway:
        def chat_json(self, system, user, out_model, **kwargs):
            return out_model(value="4", quote="我方承诺2小时响应（原文中并不存在）"), None

    outcome = scoring._value_extraction_residual(
        _criterion(), pages, inventory, lambda: _FakeGateway())
    assert outcome is None


# ---- c) 全文也抽不到 → 暂定兜底（未定分，非不符合） ------------------------

def test_extraction_failure_settles_as_provisional_not_noncompliance():
    pages = [{"document_id": 1, "page_no": 2, "source": "text", "text": "无关材料，没有承诺值。"}]
    criterion = _criterion()
    detail = ("未在投标文件中抽取到 response_hours 的承诺值"
              "（抽取未果，暂定待人工核对，不构成不符合结论）")
    outcome = scoring._value_extraction_unresolved_outcome(criterion, pages, detail)
    assert outcome.basis == "fallback"
    assert outcome.verdict == "concern"  # 不是 "fail"（不符合）
    assert outcome.score is None          # 不进 final_score 定格
    assert outcome.raw["fallback"]["kind"] == "value_extraction_unresolved"
    assert outcome.warnings[0]["fallback_score"]["kind"] == "value_extraction_unresolved"
    assert "暂定待人工" in outcome.reasoning

    # _persist_cell 同构落库形态 → _valid_cell 认它（已定格未定分，计完成）
    row = SimpleNamespace(
        ai_raw_json={**outcome.raw, "auto_review": {
            "state": "completed", "attempts": 1, "basis": outcome.basis,
            "reasoning": outcome.reasoning, "coverage_complete": False,
            "calculation": outcome.raw.get("calculation"),
            "fallback": outcome.raw.get("fallback"),
        }},
        review_status="auto_confirmed",
        ai_reasoning=outcome.reasoning,
        machine_status=outcome.verdict,
        final_score=outcome.score,
        warnings_json=outcome.warnings,
    )
    assert automation._valid_cell(row, criterion, []) is True
    assert row.final_score is None


# ---- d) 反向锁：确认缺失 + 制裁句仍按 0 分结论 -----------------------------

def test_confirmed_absent_with_sanction_still_scores_zero_conclusion():
    criterion = {
        "code": "c60", "name": "重大故障响应时间", "score_type": "count", "max_score": "4",
        "description": "重大故障响应时间不超过4小时得4分。"
                       "【证明材料】提供服务承诺函，未提供的不得分。",
        "source_quote": "响应时间按招标文件要求提供承诺函，未提供的不得分。",
        "params": {"engine": "value_tier", "field": "response_hours",
                   "tiers": [{"max_value": "4", "score": "4"}]},
    }
    decision = AutoReviewDecision(
        verdict="fail", reasoning="完整查阅后确认未提供响应承诺函",
        coverage_complete=True, confirmed_absent=True, facts={})
    # 全文也确实抽不到承诺值（否则"确认缺失"前提不成立）
    score, detail, _ = scoring._verified_count(
        criterion, decision, [{"text": "服务承诺章节（本页无响应时间承诺）"}])
    assert score == Decimal(0)
    assert "完整材料检索确认" in detail


def test_unconfirmed_extraction_failure_is_not_a_zero_conclusion():
    """没有 confirmed_absent 时，抽取失败必须报错（走补评/兜底），不得返回 0 结论。"""
    criterion = _criterion()
    decision = AutoReviewDecision(
        verdict="pass", reasoning="尚未核实承诺值", coverage_complete=True, facts={})
    with pytest.raises(LLMError, match="no_facts"):
        scoring._verified_count(
            criterion, decision, [{"text": "服务承诺章节（本页无响应时间承诺）"}])


# ---- e) 开关关闭 → 残差轮不调用 -------------------------------------------

def test_value_extract_llm_disabled_skips_residual_round(monkeypatch):
    monkeypatch.setenv("BID_EVAL_VALUE_EXTRACT_LLM", "0")
    pages, inventory = _anchor_hit_inventory()
    calls: list = []

    def factory():
        calls.append(1)
        raise AssertionError("开关关闭时不得触达 LLM")

    outcome = scoring._value_extraction_residual(
        _criterion(), pages, inventory, factory)
    assert outcome is None
    assert calls == []


# ---- 抽取器现实写法（单位换算/表格分隔/中文数字/全角） ----------------------

def test_response_hours_unit_and_separator_forms():
    # 分钟换算
    value, _ = _extract_response_hours("我方承诺30分钟内到场。")
    assert value == Decimal("0.5")
    # 表格分隔形态
    value, _ = _extract_response_hours("响应时间 | 2小时\n承诺满足")
    assert value == Decimal("2")
    # 中文数字 + "为"
    value, _ = _extract_response_hours("故障响应时间为三小时内到达现场。")
    assert value == Decimal("3")
    # 天 → 小时
    value, _ = _extract_response_hours("我方承诺1天内到场响应。")
    assert value == Decimal("24")


def test_warranty_months_forms_and_fullwidth_digits():
    from app.engine.objective import _extract_warranty_months

    value, _ = _extract_warranty_months("本项目质保期为3年。")
    assert value == Decimal("36")
    value, _ = _extract_warranty_months("质保期：３６个月")
    assert value == Decimal("36")
    value, _ = _extract_warranty_months("提供两年免费保修服务。")
    assert value == Decimal("24")
