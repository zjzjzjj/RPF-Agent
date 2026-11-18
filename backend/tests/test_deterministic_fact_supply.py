# -*- coding: utf-8 -*-
"""确定性事实源接入证据闸门（2026-11-18，ADR 0015）。

病灶回归锚：截图节是图片页、模型给不出 fact_evidence → check_scalar 抛
「事实 cert_section_present 缺少对应原页证据」→ 定向补抽 2 次耗尽 → 悬置。
供给层把确定性检测结论合成合法证据注入，模型口供被 schema 抑制 + pop 兜底。
红线测试：未检出不供给不否定；锚复核不过绝不放行；案例布尔只升不降。
"""
from __future__ import annotations

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.engine.auto_review import (
    AutoReviewDecision,
    _inject_supplied_facts,
    review_criterion,
)
from app.llm.base import LLMError

QUOTE = "本公司提供质量管理体系认证证书，证书编号为ABC123456。"
PAGE_MODEL = {"document_id": 7, "page_no": 3, "text": QUOTE, "source": "text"}
# 利群形态：截图节标题页（命中但页文本 <12 归一化字 → visual 锚兜底）
PAGE_TITLE_SHORT = {"document_id": 7, "page_no": 8, "text": "查询结果截图", "source": "ocr"}
# 文本标题页（命中且窗口 ≥12 字 → text 锚）
PAGE_HIT = {"document_id": 7, "page_no": 5,
            "text": "查询结果截图\n认证认可信息公共服务平台查询结果", "source": "text"}
PAGE_BLIND = {"document_id": 7, "page_no": 9, "text": "", "source": "ocr_img"}
RULE_COUNT = {"code": "c1", "name": "体系认证", "score_type": "count", "max_score": "3",
              "params": {"field": "certificates"}}


class Replies:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat_json(self, system, user, out_model, **kwargs):
        self.calls.append({"user": json.loads(user.split("\n", 1)[1].rsplit("\n", 1)[0])
                           if user.startswith("<review>") else user, **kwargs})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        try:
            return out_model.model_validate(reply), None
        except ValidationError as exc:
            raise LLMError(str(exc)) from exc


def decision(**overrides):
    return {"verdict": "pass", "reasoning": "原页存在对应证书内容",
            "evidence": [{"document_id": 7, "page_no": 3, "quote": QUOTE}],
            "facts": {}, **overrides}


def test_cert_section_supplied_passes_gate_without_model_evidence() -> None:
    """利群病灶回归锚：检测命中 → 供给过闸，模型不报该字段也不必给引文。"""
    replies = Replies(decision(facts={"certificates": []}))
    got = review_criterion(replies, RULE_COUNT, [PAGE_MODEL, PAGE_HIT])
    assert got.facts.get("cert_section_present") is True
    prov = got.facts.get("_fact_provenance", {}).get("cert_section_present", {})
    assert prov.get("detector") == "cert_evidence_present"
    assert prov.get("injected") is True
    engine_ev = [e for e in got.evidence if e.get("engine_supplied")]
    assert engine_ev and all(e["kind"] == "text" for e in engine_ev)
    assert len(replies.calls) == 1, "供给过闸不得触发定向补抽"


def test_short_image_page_falls_back_to_visual_anchor() -> None:
    """利群截图页形态（OCR 仅标题 <12 字）：visual 锚兜底，同样过闸。"""
    replies = Replies(decision(facts={"certificates": []}))
    got = review_criterion(replies, RULE_COUNT, [PAGE_MODEL, PAGE_TITLE_SHORT])
    assert got.facts.get("cert_section_present") is True
    visual = [e for e in got.evidence
              if e.get("engine_supplied") and e.get("kind") == "visual"]
    assert visual and visual[0]["page_no"] == 8


def test_model_repeated_supply_is_popped_not_retried() -> None:
    """模型无视 schema 重复报供给字段 → pop 兜底（第二道保险），冲突留痕不抛错。"""
    replies = Replies(decision(facts={
        "certificates": [], "cert_section_present": False,
        "fact_evidence": {"cert_section_present": []},
    }))
    got = review_criterion(replies, RULE_COUNT, [PAGE_MODEL, PAGE_HIT])
    assert got.facts.get("cert_section_present") is True, "检测值必须胜出"
    prov = got.facts["_fact_provenance"]["cert_section_present"]
    assert prov.get("repeated_input_popped") is True
    assert prov.get("conflict", {}).get("model_value") is False
    assert len(replies.calls) == 1


def test_blind_pages_no_supply_keeps_old_failure_path() -> None:
    """盲态（零文本页）不供给不否定：模型无引文口供照旧被拦（旧病灶行为保留）。"""
    replies = Replies(*[decision(facts={
        "certificates": [], "cert_section_present": True,
        "fact_evidence": {},
    }) for _ in range(3)])
    with pytest.raises(LLMError, match="缺少对应原页证据"):
        review_criterion(replies, RULE_COUNT, [PAGE_MODEL, PAGE_BLIND])


def test_tampered_anchor_is_dropped_never_passed() -> None:
    """变异强度：quote 不出自页文本 → 丢锚（anchor_verify_failed），绝不放行。"""
    bad = {"cert_section_present": {
        "value": True, "detector": "cert_evidence_present",
        "mode": "evidence", "exclusive": True,
        "anchors": [{"document_id": 7, "page_no": 3,
                     "quote": "这句绝对不在送审页上，是伪造的引文内容"}],
    }}
    rule = {**RULE_COUNT, "_supplied_facts": bad}
    replies = Replies(decision(facts={"certificates": []}))
    got = review_criterion(replies, rule, [PAGE_MODEL, PAGE_HIT])
    assert not got.facts.get("cert_section_present"), "丢锚后值不得注入"
    prov = got.facts["_fact_provenance"]["cert_section_present"]
    assert prov.get("anchor_verify_failed") == 1
    assert prov.get("mode") == "provenance_only"
    assert not any(e.get("engine_supplied") for e in got.evidence)


def test_case_flags_merge_only_upgrades() -> None:
    """案例布尔只升不降（overlay 范式）：检测 True 升模型 None/False；
    检测 False/None 不降模型 True。"""
    supplies = {"verified_cases": {
        "value": [{"name": "某项目", "social_security": True}],
        "detector": "overlay_case_flags", "identity": "name",
        "exclusive": False, "mode": "evidence", "anchors": [],
    }}
    # 模型 False + 检测 True → 升 True
    d = AutoReviewDecision(verdict="pass", reasoning="x", evidence=[],
                           facts={"verified_cases": [{"name": "某项目", "social_security": False}]})
    _inject_supplied_facts(d, supplies, [])
    assert d.facts["verified_cases"][0]["social_security"] is True
    # 模型 True + 检测 False → 保持 True
    supplies_down = {"verified_cases": {
        "value": [{"name": "某项目", "social_security": False}],
        "detector": "overlay_case_flags", "identity": "name",
        "exclusive": False, "mode": "evidence", "anchors": [],
    }}
    d2 = AutoReviewDecision(verdict="pass", reasoning="x", evidence=[],
                            facts={"verified_cases": [{"name": "某项目", "social_security": True}]})
    _inject_supplied_facts(d2, supplies_down, [])
    assert d2.facts["verified_cases"][0]["social_security"] is True


def test_schema_suppression_and_policy_in_payload() -> None:
    """整字段供给 → fact_schema 删条目（冻结价同款）+ supplied_fact_policy 声明。"""
    replies = Replies(decision(facts={"certificates": []}))
    review_criterion(replies, RULE_COUNT, [PAGE_MODEL, PAGE_HIT])
    payload = replies.calls[0]["user"]
    assert "cert_section_present" not in payload["fact_schema"]
    assert "程序按确定性检测供给" in payload["supplied_fact_policy"]
