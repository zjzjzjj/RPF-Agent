"""Automatic business decisions remain distinct from model/reading failures."""
from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.engine import auto_review
from app.engine.auto_judge import AutoJudgements, BatchJudgementItem, refine_with_llm, refine_with_llm_batch
from app.engine.auto_review import AutoReviewDecision, is_cross_bidder_requirement, is_external_requirement, review_criterion
from app.engine.facts import BidFacts
from app.engine.models import PrimitiveResult
from app.errors import DataClassDenied, LLMNotConfigured
from app.llm.base import LLMError, LLMResult
from app.llm.gateway import Gateway


QUOTE = "本公司提供质量管理体系认证证书，证书编号为ABC123456。"
PAGE = {"document_id": 7, "page_no": 3, "text": QUOTE, "source": "text"}
RULE = {"name": "质量管理体系认证证书", "score_type": "veto", "max_score": None}


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
            # Match the real Gateway: a bad JSON contract is an LLMError.
            raise LLMError(str(exc)) from exc


def decision(**overrides):
    return {"verdict": "pass", "reasoning": "原页存在对应证书内容",
            "evidence": [{"document_id": 7, "page_no": 3, "quote": QUOTE}], **overrides}


def repair_attempts(reply):
    """Repeat invalid output for the initial request and both bounded repairs."""
    return [deepcopy(reply) for _ in range(3)]


def test_text_source_is_bound_to_actual_document_and_page():
    got = review_criterion(Replies(decision()), RULE, [PAGE])
    assert got.coverage_complete
    assert got.evidence[0]["kind"] == "text"
    assert got.reviewed_pages == [{"document_id": 7, "page_no": 3}]


def test_document_verdict_schema_repairs_external_abstention_through_real_gateway(monkeypatch):
    from app.llm import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "_disk_get", lambda key: None)
    monkeypatch.setattr(gateway_module, "_disk_put", lambda *args: None)
    monkeypatch.setattr(Gateway, "_evict_failed", lambda self, key: self._cache.pop(key, None))

    class Provider:
        name = "offline-verdict-contract"
        model = "offline-verdict-contract"
        host = "localhost"

        def __init__(self):
            self.calls = []

        def complete(self, system, user, **kwargs):
            self.calls.append(user)
            payload = json.loads(user.split("</review>", 1)[0].split("\n", 1)[1])
            assert payload["allowed_verdicts"] == ["pass", "fail"]
            assert payload["response_schema"]["properties"]["verdict"]["enum"] == ["pass", "fail"]
            reply = (decision(verdict="unverifiable", evidence=[]) if len(self.calls) == 1
                     else decision())
            return LLMResult(text=json.dumps(reply), provider=self.name, model=self.model)

    provider = Provider()
    got = review_criterion(Gateway(provider), RULE, [PAGE])
    assert got.verdict == "pass" and len(provider.calls) == 2
    assert "JSON契约修复" in provider.calls[1]


def test_three_invalid_json_generations_fail_without_whole_cell_ocr_retry(app_env, monkeypatch):
    from app.llm import gateway as gateway_module
    from app.services import automatic_scoring_service as scoring

    monkeypatch.setattr(gateway_module, "_disk_get", lambda key: None)
    monkeypatch.setattr(gateway_module, "_disk_put", lambda *args: None)
    monkeypatch.setenv("BID_EVAL_AUTO_REVIEW_ROUNDS", "2")
    monkeypatch.setattr(scoring, "_supplement_pages", lambda *args, **kwargs: pytest.fail("JSON生成耗尽后不得重读全文"))

    class Provider:
        name = model = "offline-invalid-json"
        host = "localhost"

        def __init__(self):
            self.calls = []

        def complete(self, system, user, **kwargs):
            self.calls.append(user)
            return LLMResult(text='{"verdict":', provider=self.name, model=self.model)

    provider = Provider()
    gateway = Gateway(provider)
    with pytest.raises(LLMError, match="JSON 不完整") as failure:
        scoring._assess_cell(RULE, [PAGE], "synthetic", lambda: gateway)
    assert len(provider.calls) == 3
    assert failure.value.review_attempts_exhausted is True
    assert failure.value.attempt_history[-1]["round"] == 0


def test_external_verdict_schema_keeps_explicit_evidence_boundary():
    gateway = Replies(decision(verdict="unverifiable", evidence=[]))
    got = review_criterion(gateway, {**RULE, "name": "电子投标文件递交时间"}, [PAGE])
    assert got.verdict == "unverifiable"
    assert gateway.calls[0]["user"]["allowed_verdicts"] == ["pass", "fail", "unverifiable"]


def test_corporate_control_checks_are_external_but_registration_documents_are_not():
    assert is_external_requirement({"name": "不得为同一负责人或存在控股管理关系的单位同时投标"})
    assert not is_external_requirement({"name": "工商变更信息记录及单位盖章"})
    assert not is_external_requirement({"name": "负责人资格证书及承诺书"})


@pytest.mark.parametrize("name", ["串通投标情形（否决投标）", "不同投标人投标文件由同一单位或个人编制（视为串通投标）",
                                  "不得为同一负责人或存在控股管理关系的单位同时投标"])
def test_horizontal_requirements_need_all_selected_bidders(name):
    rule = {**RULE, "name": name}
    assert is_cross_bidder_requirement(rule) and is_external_requirement(rule)
    with pytest.raises(LLMError, match="全部所选投标人"):
        review_criterion(Replies(decision(verdict="unverifiable", evidence=[])), rule, [PAGE])


def test_horizontal_boundary_cannot_hide_missing_or_unreadable_peer_pages():
    peer = {**PAGE, "document_id": 8, "page_no": 1, "bidder_id": 2, "bidder_name": "乙"}
    own = {**PAGE, "page_no": 1, "bidder_id": 1, "bidder_name": "甲"}
    rule = {**RULE, "name": "串通投标情形", "_frozen_bidders": [{"id": 1}, {"id": 2}],
            "_comparison_documents": [{"document_id": 7, "bidder_id": 1, "page_count": 1},
                                      {"document_id": 8, "bidder_id": 2, "page_count": 1}]}
    reply = decision(verdict="unverifiable", evidence=[], reasoning="已比对各家文件，缺少平台IP记录")
    gateway = Replies(reply)
    got = review_criterion(gateway, rule, [own, peer])
    assert got.coverage_complete and len(got.reviewed_pages) == 2
    assert {p["bidder_id"] for p in gateway.calls[0]["user"]["pages"]} == {1, 2}
    for invalid in ([own], [own, own], [own, {**peer, "bidder_id": 1}]):
        with pytest.raises(LLMError, match="全部所选投标人"):
            review_criterion(Replies(reply), rule, invalid)
    with pytest.raises(LLMError, match="全部可读"):
        review_criterion(Replies(reply), rule, [own, {**peer, "unreadable": True}])


def test_ordinary_anti_collusion_declaration_is_still_document_review():
    rule = {**RULE, "name": "无串通投标承诺书"}
    assert not is_cross_bidder_requirement(rule) and not is_external_requirement(rule)
    assert not auto_review.is_price_limit_requirement({**RULE, "check_type": "math_relation", "params": {"max_amount_yuan": "100"}})


def test_annual_price_contract_requires_independent_components_and_allows_real_absence():
    rule = {**RULE, "name": "投标报价最高限价", "check_type": "math_relation",
            "params": {"max_amount_yuan": "5049600", "yearly_max_amount_yuan": ["1519800", "1683200", "1846600"]},
            "_frozen_bidder": {"bid_price": "4017400", "price_status": "confirmed"}}
    gateway = Replies(decision(verdict="fail", evidence=[], confirmed_absent=True,
                               reasoning="完整查阅全部材料，未提供三个年度的报价明细"))
    got = review_criterion(gateway, rule, [PAGE])
    assert got.confirmed_absent and got.coverage_complete and got.verdict == "fail"
    payload = gateway.calls[0]["user"]
    assert [c["field"] for c in payload["required_price_components"]] == [f"yearly_bid_price_{i}" for i in range(1, 4)]
    assert all(f"yearly_bid_price_{i}" in payload["fact_schema"] for i in range(1, 4))
    assert "bid_price" not in payload["fact_schema"]
    repeated = review_criterion(Replies(decision(verdict="fail", evidence=[], confirmed_absent=True,
                                               facts={"bid_price": "4017400", "fact_evidence": {"bid_price": []}})), rule, [PAGE])
    assert "bid_price" not in repeated.facts and "bid_price" not in repeated.facts["fact_evidence"]
    with pytest.raises(LLMError, match="报价冲突"):
        review_criterion(Replies(*repair_attempts(decision(facts={"bid_price": "4017401"}))), rule, [PAGE])
    with pytest.raises(LLMError, match="冻结总价不能代替"):
        review_criterion(Replies(decision(evidence=[])), rule, [PAGE])

    absent = decision(verdict="fail", evidence=[], confirmed_absent=True,
                      missing_evidence=["第一年度含税投标报价原页", "分年度报价表/开标一览表"])
    got = review_criterion(Replies(absent), rule, [PAGE])
    assert got.missing_evidence == [] and got.confirmed_missing_evidence == absent["missing_evidence"]
    unresolved = review_criterion(Replies({**absent, "missing_evidence": ["第一年度报价页图模糊，需重新识别"]}), rule, [PAGE])
    assert unresolved.missing_evidence and not unresolved.confirmed_missing_evidence
    with pytest.raises(LLMError, match="全部可读"):
        review_criterion(Replies(absent), rule, [{**PAGE, "unreadable": True}])


@pytest.mark.parametrize("evidence", [
    [{"document_id": 8, "page_no": 3, "quote": QUOTE}],
    [{"document_id": 7, "page_no": 4, "quote": QUOTE}],
    [{"document_id": 7, "page_no": 3, "quote": "此处是模型编造的并不存在的证书材料。"}],
    [{"document_id": 7, "page_no": 3, "quote": "ABC123456"}],
])
def test_hallucinated_or_short_citation_requires_automatic_repair(evidence):
    gateway = Replies(*repair_attempts(decision(evidence=evidence)))
    with pytest.raises(LLMError, match="定向补抽 2 次"):
        review_criterion(gateway, RULE, [PAGE])
    assert len(gateway.calls) == 3
    assert [call["user"].get("validation_feedback", {}).get("attempt", 0) for call in gateway.calls] == [0, 1, 2]


def test_external_missing_record_is_a_completed_boundary_conclusion():
    rule = {"name": "电子投标文件递交时限", "score_type": "veto"}
    got = review_criterion(Replies(decision(verdict="unverifiable", evidence=[],
                                          missing_evidence=["交易平台递交回执"])), rule, [])
    assert got.verdict == "unverifiable"
    assert got.missing_evidence == []
    assert "交易平台递交回执" in got.reasoning
    assert got.score is None


def test_ordinary_scan_cannot_escape_review_as_unverifiable():
    with pytest.raises(LLMError, match="非外部事实"):
        review_criterion(Replies(decision(verdict="unverifiable", evidence=[])), RULE, [PAGE])


def test_provisional_batch_abstention_does_not_bypass_the_final_document_contract(monkeypatch):
    monkeypatch.setattr(auto_review, "TEXT_BUDGET", 2100)
    pages = [{**PAGE, "text": QUOTE + "甲" * 1200}, {**PAGE, "page_no": 4, "text": QUOTE + "乙" * 1200}]
    provisional = decision(verdict="unverifiable", evidence=[], reasoning="本批没有足够材料，留给综合复评")
    gateway = Replies(provisional, provisional, decision(verdict="fail", evidence=[], confirmed_absent=True))
    got = review_criterion(gateway, RULE, pages, round_no=1)
    assert got.verdict == "fail" and got.coverage_complete and got.confirmed_absent
    assert all(c["user"]["collection_only"] and "unverifiable" in c["user"]["allowed_verdicts"] for c in gateway.calls[:2])
    assert gateway.calls[-1]["user"]["allowed_verdicts"] == ["pass", "fail"]
    assert not gateway.calls[-1]["user"]["collection_only"]
    with pytest.raises(LLMError, match="非外部事实"):
        review_criterion(Replies(provisional, provisional, provisional), RULE, pages, round_no=1)


@pytest.mark.parametrize("name, expected", [
    ("开标现场密封完好性检查", True), ("投标文件密封方式", True),
    ("现场密封", True), ("投标文件现场密封检查", True),
    ("电子投标文件递交时限", True), ("投标文件送达时间", True),
    ("现场服务响应时间", False), ("营业执照复印件", False),
    ("法定代表人签章", False), ("质量管理体系认证证书", False),
    ("投标文件签章与密封承诺", False),
    ("投标文件按时递交（电子采购平台上传）", True),
    ("投标文件逾期上传", True),
    ("未按要求获取招标文件的不得投标", True),
    ("按评标委员会要求澄清、说明或补正", True),
    ("不按评标委员会要求澄清或说明", True),
    ("不得与招标项目设计、技术规格编制或咨询公司存在关联", True),
    ("与招标人存在利害关系", True),
    ("电子招标投标交易平台运营机构投标", True),
    ("获取招标文件的授权委托书签章", False),
    ("技术规格编制服务方案", False),
    ("澄清说明承诺书签章", False),
])
def test_external_classifier_is_limited_to_genuine_external_facts(name, expected):
    assert is_external_requirement({"name": name}) is expected


def test_unreadable_scan_never_means_confirmed_absence():
    with pytest.raises(LLMError, match="全部可读"):
        review_criterion(Replies(decision(verdict="fail", evidence=[], confirmed_absent=True)),
                         RULE, [{**PAGE, "text": "证书目录", "source": "ocr_degraded"}])


def test_actual_absence_after_complete_reading_is_allowed():
    got = review_criterion(Replies(decision(verdict="fail", evidence=[], confirmed_absent=True)), RULE, [PAGE])
    assert got.confirmed_absent and got.coverage_complete


def test_objective_facts_cannot_be_replaced_with_model_score():
    with pytest.raises(LLMError, match="确定性引擎"):
        review_criterion(Replies(decision(score="3")), {**RULE, "score_type": "count", "max_score": "3"}, [PAGE])


def test_case_and_certificate_entries_need_individual_sources():
    with pytest.raises(LLMError, match="逐条"):
        review_criterion(Replies(*repair_attempts(decision(facts={"verified_cases": [{"name": "未知案例", "amount": "800000"}]}))),
                         {**RULE, "score_type": "count"}, [PAGE])
    with pytest.raises(LLMError, match="逐本"):
        review_criterion(Replies(*repair_attempts(decision(facts={"certificates": ["ISO9001"]}))),
                         {**RULE, "score_type": "count"}, [PAGE])


def test_certificate_with_valid_per_certificate_backing_is_accepted():
    got = review_criterion(Replies(decision(facts={
        "certificates": ["ISO9001"], "cert_backing": {"ISO9001": {"present": True, "quote": QUOTE}},
    })), {**RULE, "score_type": "count"}, [PAGE])
    assert got.facts["certificates"] == ["ISO9001"]


@pytest.mark.parametrize("score", ["6", "-1", "NaN", "Infinity"])
def test_subjective_score_must_be_finite_and_within_frozen_maximum(score):
    with pytest.raises(LLMError):
        review_criterion(Replies(decision(score=score)), {**RULE, "score_type": "subjective", "max_score": "5"}, [PAGE])


def test_model_error_is_not_a_noncompliance_verdict():
    gateway = Replies(LLMError("timeout", transient=True))
    with pytest.raises(LLMError, match="timeout"):
        refine_with_llm(gateway, RULE, BidFacts(text=QUOTE), QUOTE, PrimitiveResult("unknown", "未定"))
    with pytest.raises(LLMError, match="timeout"):
        refine_with_llm_batch(Replies(LLMError("timeout")), [{"criterion": {**RULE, "code": "C1"}}], QUOTE)


def test_invalid_batch_verdict_is_not_converted_to_fail():
    class BadBatch:
        def chat_json(self, *args, **kwargs):
            return AutoJudgements(judgements=[BatchJudgementItem(code="C1", verdict="unknown", reasoning="缺证据")]), None

    with pytest.raises(LLMError, match="契约无效"):
        refine_with_llm_batch(BadBatch(), [{"criterion": {**RULE, "code": "C1"}}], QUOTE)


def test_vision_configuration_is_explicit(monkeypatch):
    monkeypatch.delenv("BID_EVAL_VISION_MODEL", raising=False)
    with pytest.raises(LLMNotConfigured, match="BID_EVAL_VISION_MODEL"):
        auto_review.get_vision_gateway()


def test_visual_review_uses_separate_gateway_and_real_page_image(monkeypatch):
    visual = Replies(decision(evidence=[{"document_id": 7, "page_no": 3, "kind": "visual",
                                         "observation": "原页右下角存在投标人印章图像"}]))
    monkeypatch.setattr(auto_review, "get_vision_gateway", lambda: visual)
    got = review_criterion(Replies(), RULE, [{**PAGE, "source": "ocr_degraded", "image_url": "data:image/png;base64,AAAA"}],
                           use_vision=True)
    assert got.coverage_complete
    assert visual.calls[0]["images"] == ["data:image/png;base64,AAAA"]
    assert got.evidence[0]["quote"] == ""
    assert got.evidence[0]["image_sha256"]


def test_image_contents_are_part_of_gateway_cache_key():
    kwargs = dict(system="s", user="u", provider="p", model="m", prompt_version="v")
    assert Gateway._cache_key(**kwargs, images=["image-a"]) != Gateway._cache_key(**kwargs, images=["image-b"])
    assert Gateway._cache_key(**kwargs) == Gateway._cache_key(**kwargs, images=[])


def test_visual_gateway_preserves_existing_data_class_gate(monkeypatch):
    class NeverCalled:
        name, model, host = "fake", "vision", "external.example.com"

        def complete(self, *args, **kwargs):
            raise AssertionError("confidential page must not reach the provider")

    monkeypatch.setenv("BID_EVAL_ALLOW_REMOTE_CONFIDENTIAL", "0")
    with pytest.raises(DataClassDenied):
        Gateway(NeverCalled()).chat("s", "u", images=["data:image/png;base64,AAAA"], data_class="confidential")


def test_scalar_fact_requires_its_own_source():
    with pytest.raises(LLMError, match="对应原页证据"):
        review_criterion(Replies(*repair_attempts(decision(facts={"response_hours": "4"}))),
                         {**RULE, "score_type": "count"}, [PAGE])
    numeric_quote = "本公司郑重承诺故障响应时间为4小时内到达现场。"
    got = review_criterion(Replies(decision(evidence=[{"document_id": 7, "page_no": 3, "quote": numeric_quote}], facts={
        "response_hours": "4", "fact_evidence": {"response_hours": [
            {"document_id": 7, "page_no": 3, "quote": numeric_quote}]},
    })), {**RULE, "score_type": "count"}, [{**PAGE, "text": numeric_quote}])
    assert got.facts["response_hours"] == "4"


def test_long_count_review_reads_all_batches_before_final_facts(monkeypatch):
    monkeypatch.setattr(auto_review, "TEXT_BUDGET", 2000)
    second_quote = "第二份材料载明本公司另有一项完整且有效的认证证明。"
    first = {**PAGE, "text": QUOTE + "甲" * (1000 - len(QUOTE))}
    second = {**PAGE, "page_no": 4, "text": second_quote + "乙" * (1000 - len(second_quote))}
    second_evidence = {"document_id": 7, "page_no": 4, "quote": second_quote}
    gateway = Replies(decision(), decision(evidence=[second_evidence]),
                      decision(evidence=[{"document_id": 7, "page_no": 3, "quote": QUOTE}, second_evidence]))
    got = review_criterion(gateway, {**RULE, "score_type": "count"}, [first, second])
    assert got.coverage_complete
    assert len(gateway.calls) == 3
    assert len(got.evidence) == 2


def test_synthesis_cannot_invent_visual_observation(monkeypatch):
    monkeypatch.setattr(auto_review, "IMAGE_BATCH_SIZE", 1)
    observation = "右下角可见红色印章图像，印章文字与投标人一致"
    e1 = {"document_id": 7, "page_no": 3, "kind": "visual", "observation": observation}
    e2 = {**e1, "page_no": 4}
    visual = Replies(decision(evidence=[e1]), decision(evidence=[e2]),
                     *repair_attempts(decision(evidence=[{**e1, "observation": "未经图片批次观察的新结论"}])))
    monkeypatch.setattr(auto_review, "get_vision_gateway", lambda: visual)
    pages = [{**PAGE, "text": "", "source": "ocr_degraded", "image_url": "data:image/png;base64,AAAA"},
             {**PAGE, "page_no": 4, "text": "", "source": "ocr_degraded", "image_url": "data:image/png;base64,BBBB"}]
    with pytest.raises(LLMError, match="未经原批次核验"):
        review_criterion(Replies(), RULE, pages, use_vision=True)
    assert len(visual.calls[0]["images"]) == 1
    assert len(visual.calls[1]["images"]) == 1
    assert len(visual.calls) == 5
    assert all("images" not in call for call in visual.calls[2:])


SUBJECTIVE_RULE = {
    "name": "服务方案", "score_type": "subjective", "max_score": "5",
    "description": "响应方案（0-2分）、维护方案（0-3分），未提供对应方案不得分。",
}


def subitem_decision(**overrides):
    return decision(score="5", sub_scores=[
        {"name": "响应方案", "score": "1", "quote": QUOTE},
        {"name": "维护方案", "score": "2", "quote": QUOTE},
    ], **overrides)


def test_subjective_subitem_sum_is_computed_from_frozen_components():
    gateway = Replies(subitem_decision())
    got = review_criterion(gateway, SUBJECTIVE_RULE, [PAGE])
    assert got.score == Decimal("3")
    assert got.sub_score_calculation == "1 + 2 = 3"
    assert [entry["max"] for entry in got.sub_scores] == ["2", "3"]
    assert gateway.calls[0]["user"]["frozen_subitems"] == [
        {"name": "响应方案", "max_score": "2"}, {"name": "维护方案", "max_score": "3"},
    ]


def test_missing_subjective_subitem_never_becomes_synthetic_zero():
    reply = subitem_decision()
    reply["sub_scores"].pop()
    with pytest.raises(LLMError, match="尚未完成"):
        review_criterion(Replies(reply), SUBJECTIVE_RULE, [PAGE])


@pytest.mark.parametrize("marker", [
    {"abstain": True}, {"gate_killed": True}, {"provisional": True},
    {"evidence_state": "missing"}, {"zero_reason": "all_abstain"},
    {"missing_evidence": ["维护原文"]}, {"status": "need_human"},
])
def test_subjective_abstention_requires_supplemental_review(marker):
    reply = subitem_decision()
    reply["sub_scores"][1].update(marker)
    with pytest.raises(LLMError, match="弃权或未决"):
        review_criterion(Replies(reply), SUBJECTIVE_RULE, [PAGE])


@pytest.mark.parametrize("score", ["4", "-1", "NaN", "Infinity", None])
def test_subjective_subitems_cannot_exceed_frozen_caps_or_omit_score(score):
    reply = subitem_decision()
    reply["sub_scores"][1]["score"] = score
    with pytest.raises(LLMError):
        review_criterion(Replies(reply), SUBJECTIVE_RULE, [PAGE])


@pytest.mark.parametrize("change", [
    {"quote": "模型编造的维护方案原文以及证据内容"}, {"quote": "本公司"}, {"quote": ""},
])
def test_every_subjective_subitem_requires_grounded_evidence(change):
    reply = subitem_decision()
    reply["sub_scores"][1].update(change)
    with pytest.raises(LLMError, match="原页证据"):
        review_criterion(Replies(reply), SUBJECTIVE_RULE, [PAGE])


def test_subjective_duplicate_and_unknown_components_are_rejected():
    for extra in ("响应方案", "临时服务方案"):
        reply = subitem_decision()
        reply["sub_scores"].append({"name": extra, "score": "1", "quote": QUOTE})
        with pytest.raises(LLMError, match="重复或未知"):
            review_criterion(Replies(reply), SUBJECTIVE_RULE, [PAGE])


def test_confirmed_absent_subjective_component_uses_explicit_zero_rule():
    reply = subitem_decision()
    reply["sub_scores"][1] = {"name": "维护方案", "score": "0", "confirmed_absent": True}
    got = review_criterion(Replies(reply), SUBJECTIVE_RULE, [PAGE])
    assert got.score == Decimal("1")
    without_zero_rule = {**SUBJECTIVE_RULE, "description": "响应方案（0-2分）、维护方案（0-3分）"}
    with pytest.raises(LLMError, match="原页证据"):
        review_criterion(Replies(reply), without_zero_rule, [PAGE])
    with pytest.raises(LLMError, match="原页证据"):
        review_criterion(Replies(reply), SUBJECTIVE_RULE, [{**PAGE, "unreadable": True}])


def test_complete_subjective_absence_cannot_award_an_invented_score():
    reply = decision(score="5", confirmed_absent=True, evidence=[])
    rule = {**RULE, "score_type": "subjective", "max_score": "5", "description": "未提供方案不得分"}
    with pytest.raises(LLMError, match="不能凭空"):
        review_criterion(Replies(reply), rule, [PAGE])
    reply["score"] = "0"
    assert review_criterion(Replies(reply), rule, [PAGE]).score == 0


def test_frozen_subjective_levels_exclude_scores_in_unrated_gaps():
    rule = {**RULE, "score_type": "subjective", "max_score": "5", "levels": [
        {"label": "较好", "min": "4", "max": "5"}, {"label": "一般", "min": "1", "max": "2"},
    ]}
    with pytest.raises(LLMError, match="合法档位"):
        review_criterion(Replies(decision(score="3")), rule, [PAGE])


def test_verified_blank_pages_are_readable_without_ocr_or_vision():
    blank = {**PAGE, "page_no": 4, "text": "", "source": "skipped", "known_blank": True}
    gateway = Replies(decision())
    got = review_criterion(gateway, RULE, [PAGE, blank])
    assert got.coverage_complete
    assert gateway.calls[0]["user"]["pages"][1]["known_blank"] is True


@pytest.mark.parametrize("invented", [
    {"document_id": 999, "page_no": 1, "quote": "本项目投标总价为999999元人民币，报价真实有效。"},
    {"document_id": 999, "page_no": 1, "kind": "visual"},
])
def test_numeric_facts_cannot_mix_valid_and_unverified_sources(invented):
    reply = decision(facts={"bid_price": "999999", "fact_evidence": {"bid_price": [
        {"document_id": 7, "page_no": 3, "quote": QUOTE}, invented,
    ]}})
    with pytest.raises(LLMError, match="未核验的原页引用"):
        review_criterion(Replies(*repair_attempts(reply)), {**RULE, "score_type": "count"}, [PAGE])


def test_unreadable_receipt_cannot_be_completed_as_missing_external_record():
    rule = {"name": "电子投标文件递交时限", "score_type": "veto"}
    reply = decision(verdict="unverifiable", evidence=[], missing_evidence=["平台回执"])
    with pytest.raises(LLMError, match="全部可读原页"):
        review_criterion(Replies(reply), rule, [{**PAGE, "text": "回执扫描件", "unreadable": True}])


def test_external_boundary_reads_all_associated_pages_even_on_initial_review(monkeypatch):
    monkeypatch.setattr(auto_review, "TEXT_BUDGET", 2000)
    pages = [{**PAGE, "page_no": i, "text": "递交时限" + "甲" * 1100} for i in range(1, 27)]

    class BoundaryGateway:
        def __init__(self):
            self.seen = set()

        def chat_json(self, system, user, out_model, **kwargs):
            payload = json.loads(user.split("\n", 1)[1].rsplit("\n", 1)[0])
            if payload["batch_results"] is None:
                self.seen.update(p["page_no"] for p in payload["pages"])
            return out_model.model_validate(decision(verdict="unverifiable", evidence=[])), None

    gateway = BoundaryGateway()
    got = review_criterion(gateway, {"name": "电子投标文件递交时限", "score_type": "veto"}, pages)
    assert got.coverage_complete and gateway.seen == set(range(1, 27))


def test_supplemental_review_expands_to_low_relevance_tail_pages(monkeypatch):
    monkeypatch.setattr(auto_review, "TEXT_BUDGET", 2000)
    tail = "末页记载实际运行保障流程，包含详细响应机制以及事故处理措施。"
    pages = [{**PAGE, "page_no": i, "text": "运行方案" + "甲" * 1100} for i in range(1, 27)]
    pages[-1]["text"] = tail + "乙" * 1100
    rule = {"name": "运行方案", "score_type": "subjective", "max_score": "5"}

    class TailGateway:
        def __init__(self):
            self.seen = set()

        def chat_json(self, system, user, out_model, **kwargs):
            payload = json.loads(user.split("\n", 1)[1].rsplit("\n", 1)[0])
            if payload["batch_results"] is not None:
                checked = [e for batch in payload["batch_results"] for e in batch["evidence"]]
                reply = (decision(score="4", evidence=[{"evidence_id": e["evidence_id"]} for e in checked])
                         if checked else decision(score=None, evidence=[], missing_evidence=["响应机制原文"]))
                return out_model.model_validate(reply), None
            if payload["batch_results"] is None:
                self.seen.update(p["page_no"] for p in payload["pages"])
            if tail in "".join(p["text"] for p in payload["pages"]):
                reply = decision(score="4", evidence=[{"document_id": 7, "page_no": 26, "quote": tail}])
            else:
                reply = decision(score=None, evidence=[], missing_evidence=["响应机制原文"])
            return out_model.model_validate(reply), None

    first = TailGateway()
    with pytest.raises(LLMError):
        review_criterion(first, rule, pages)
    assert 26 not in first.seen
    supplemental = TailGateway()
    got = review_criterion(supplemental, rule, pages, round_no=1)
    assert got.score == 4 and got.coverage_complete
    assert supplemental.seen == set(range(1, 27))


def test_visual_absence_requires_images_for_every_nonblank_page(monkeypatch):
    reply = decision(verdict="fail", confirmed_absent=True, evidence=[{
        "document_id": 7, "page_no": 3, "kind": "visual", "observation": "该页无签章图像",
    }])
    monkeypatch.setattr(auto_review, "get_vision_gateway", lambda: Replies(reply))
    pages = [{**PAGE, "image_url": "data:image/png;base64,AAAA"}, {**PAGE, "page_no": 4}]
    with pytest.raises(LLMError, match="全部非空白原页图像"):
        review_criterion(None, {**RULE, "name": "投标函签章"}, pages, use_vision=True)
    pages[1].update(text="", source="skipped", known_blank=True)
    got = review_criterion(None, {**RULE, "name": "投标函签章"}, pages, use_vision=True)
    assert got.confirmed_absent and got.coverage_complete


def test_visual_evidence_hash_matches_original_image_bytes(monkeypatch):
    reply = decision(evidence=[{"document_id": 7, "page_no": 3, "kind": "visual",
                               "observation": "右下角可见公章图像"}])
    monkeypatch.setattr(auto_review, "get_vision_gateway", lambda: Replies(reply))
    page = {**PAGE, "image_url": "data:image/png;base64,AAAA"}
    got = review_criterion(None, RULE, [page], use_vision=True)
    assert got.evidence[0]["image_sha256"] == hashlib.sha256(b"\x00\x00\x00").hexdigest()
    got = review_criterion(None, RULE, [{**page, "image_sha256": "stored-image-bytes-hash"}], use_vision=True)
    assert got.evidence[0]["image_sha256"] == "stored-image-bytes-hash"


@pytest.mark.parametrize("score_type, score", [("subjective", "1"), ("veto", None)])
@pytest.mark.parametrize("failure", [{"unreadable": True}, {"requires_vision": True}, {"source": "ocr_degraded"}])
def test_reading_failure_cannot_be_hidden_by_a_valid_quote_and_final_score(score_type, score, failure):
    unread = {**PAGE, "page_no": 4, **failure}
    rule = {**RULE, "score_type": score_type, "max_score": "5"}
    with pytest.raises(LLMError, match="不可读页面"):
        review_criterion(Replies(decision(score=score)), rule, [PAGE, unread])


def test_unretrieved_unreadable_tail_page_blocks_subjective_final_score(monkeypatch):
    monkeypatch.setattr(auto_review, "TEXT_BUDGET", 2000)
    tail = {**PAGE, "page_no": 4, "text": "扫描方案尚未读取", "unreadable": True, "requires_vision": True}
    pages = [{**PAGE, "text": QUOTE + "甲" * 2200}, tail]
    monkeypatch.setattr(auto_review, "select_candidate_pages", lambda *args, **kwargs: [pages[0]])
    class LocalQuoteGateway:
        def chat_json(self, system, user, out_model, **kwargs):
            payload = json.loads(user.split("\n", 1)[1].rsplit("\n", 1)[0])
            if payload["batch_results"] is not None:
                return out_model.model_validate(decision(score="1", evidence=[{
                    "evidence_id": payload["allowed_evidence_ids"][0],
                }])), None
            local = payload["pages"][0]["text"][:30]
            return out_model.model_validate(decision(score="1", evidence=[{
                "document_id": 7, "page_no": 3, "quote": local,
            }])), None
    with pytest.raises(LLMError, match="不可读页面"):
        review_criterion(LocalQuoteGateway(), {**RULE, "score_type": "subjective", "max_score": "5"}, pages)


def test_attached_but_unsent_image_cannot_discharge_technical_failure():
    unread = {**PAGE, "page_no": 4, "unreadable": True, "image_url": "data:image/png;base64,AAAA"}
    with pytest.raises(LLMError, match="不可读页面"):
        review_criterion(Replies(decision(score="1")),
                         {**RULE, "score_type": "subjective", "max_score": "5"}, [PAGE, unread])


def test_actual_visual_reading_discharges_unreadable_page_before_subjective_score(monkeypatch):
    visual = Replies(decision(score="4", evidence=[
        {"document_id": 7, "page_no": 3, "quote": QUOTE},
        {"document_id": 7, "page_no": 4, "kind": "visual", "observation": "原页包含完整方案图表"},
    ]))
    monkeypatch.setattr(auto_review, "get_vision_gateway", lambda: visual)
    unread = {**PAGE, "page_no": 4, "text": "扫描图未识别", "unreadable": True, "requires_vision": True,
              "image_url": "data:image/png;base64,AAAA"}
    got = review_criterion(None, {**RULE, "score_type": "subjective", "max_score": "5"},
                           [PAGE, unread], use_vision=True)
    assert got.score == 4 and got.coverage_complete
    assert visual.calls[0]["images"] == [unread["image_url"]]


def test_readable_long_subjective_input_may_finish_from_sufficient_selected_evidence(monkeypatch):
    monkeypatch.setattr(auto_review, "TEXT_BUDGET", 2000)
    pages = [PAGE, {**PAGE, "page_no": 4, "text": "其他可读材料" + "甲" * 2500}]
    monkeypatch.setattr(auto_review, "select_candidate_pages", lambda *args, **kwargs: [PAGE])
    got = review_criterion(Replies(decision(score="4")),
                           {**RULE, "score_type": "subjective", "max_score": "5"}, pages)
    assert got.score == 4 and not got.coverage_complete


def test_continue_mode_gives_objective_count_one_semantic_repair():
    from app.services.execution_version import bind_run_execution

    rule = {"name": "投标人业绩", "score_type": "count", "max_score": "3"}
    bad = decision(facts={"person_attributes": [{"name": "张三"}]})
    gateway = Replies(deepcopy(bad), deepcopy(bad))
    manifest = {"executor_version": 1, "prompt_version": "test-prompt"}
    with bind_run_execution(manifest, failure_policy="continue"), pytest.raises(LLMError):
        review_criterion(gateway, rule, [PAGE])
    assert len(gateway.calls) == 2


def test_direct_subjective_context_is_a_single_bounded_batch():
    from app.engine.auto_review import direct_subjective_pages

    pages = [
        {"document_id": 1, "page_no": number, "text": "运维方案 " * 5000,
         "source": "text", "source_id": f"d1p{number}"}
        for number in range(1, 9)
    ]
    selected = direct_subjective_pages(
        {"name": "运维需求分析与解决措施", "description": "运维方案完整性"}, pages)
    assert len(selected) <= 6
    assert sum(len(page["text"]) for page in selected) <= 24_000
    assert all(page["source_id"] for page in selected)
