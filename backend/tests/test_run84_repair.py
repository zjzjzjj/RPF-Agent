"""Regressions observed on the real run 84, with all providers kept offline."""
import json
from decimal import Decimal

import pytest

from app.engine import auto_review
from app.engine.auto_review import review_criterion
from app.llm.base import LLMError
from app.services import automatic_scoring_service as scoring
from app.services.review_activity import reporting


QUOTE = "本公司已依法登记成立并持有有效营业执照，统一社会信用代码为123456。"
PAGE = {"document_id": 75, "page_no": 3, "text": QUOTE, "source": "text"}
RULE = {"name": "投标人主体资格", "score_type": "veto"}


class Gateway:
    def __init__(self, reply):
        self.reply = reply
        self.payloads = []

    def chat_json(self, system, user, out_model, **kwargs):
        payload = json.loads(user.split("\n", 1)[1].rsplit("\n", 1)[0])
        self.payloads.append(payload)
        reply = self.reply(payload) if callable(self.reply) else self.reply
        return out_model.model_validate(reply), None


def result(**kwargs):
    return {"verdict": "pass", "reasoning": "原件载明有效主体登记信息。",
            "evidence": [{"document_id": 75, "page_no": 3, "quote": QUOTE}], **kwargs}


def test_host_fields_cannot_poison_or_override_the_model_contract():
    gateway = Gateway(result(reviewed_pages=[3, "第4页"], coverage_complete=True,
                             sub_score_calculation={"model": "not authoritative"}, confirmed_absent=None))
    got = review_criterion(gateway, RULE, [PAGE])
    assert got.reviewed_pages == [{"document_id": 75, "page_no": 3}]
    assert got.coverage_complete and not got.confirmed_absent
    assert got.sub_score_calculation is None
    assert "reviewed_pages" not in gateway.payloads[0]["response_schema"]["properties"]


def test_null_absence_never_becomes_confirmation_of_missing_documents():
    with pytest.raises(LLMError, match="没有可核验"):
        review_criterion(Gateway(result(verdict="fail", evidence=[], confirmed_absent=None)), RULE, [PAGE])


def test_layout_only_quote_difference_maps_back_to_original_span():
    original = "本公司已依法登记成立并持有有效营业执照，\n统一社会信用代码为１２３４５６。"
    got = review_criterion(Gateway(result()), RULE, [{**PAGE, "text": original}])
    assert got.evidence[0]["quote"] == original


@pytest.mark.parametrize("bad", [QUOTE.replace("有效", "无效"), QUOTE.replace("123456", "123457")])
def test_layout_normalization_never_accepts_changed_facts(bad):
    with pytest.raises(LLMError, match="逐字"):
        review_criterion(Gateway(result(evidence=[{"document_id": 75, "page_no": 3, "quote": bad}])), RULE, [PAGE])


def test_source_id_selects_actual_original_without_model_retyping():
    gateway = Gateway(lambda p: result(evidence=[{"source_id": p["pages"][0]["source_id"]}]))
    got = review_criterion(gateway, RULE, [PAGE])
    assert got.evidence[0]["quote"] == QUOTE
    assert got.evidence[0]["document_id"] == 75
    assert got.evidence[0]["page_no"] == 3


def test_typo_id_is_rebound_only_by_unique_exact_source_quote():
    gateway = Gateway(result(evidence=[{"source_id": "mistyped", "document_id": 75,
                                        "page_no": 3, "quote": QUOTE}]))
    got = review_criterion(gateway, RULE, [PAGE])
    assert got.evidence[0]["quote"] == QUOTE
    assert gateway.payloads[0]["allowed_source_ids"] == [p["source_id"] for p in gateway.payloads[0]["pages"]]
    assert gateway.payloads[0]["allowed_evidence_ids"] == []


@pytest.mark.parametrize("reference", [
    {"source_id": "mistyped"},
    {"source_id": "mistyped", "quote": QUOTE},
    {"source_id": "mistyped", "document_id": 76, "page_no": 3, "quote": QUOTE},
    {"source_id": "mistyped", "document_id": 75, "page_no": 5, "quote": QUOTE},
    {"source_id": "mistyped", "document_id": 75, "page_no": 3, "quote": QUOTE.replace("有效", "无效")},
])
def test_typo_id_never_recovers_an_ambiguous_wrong_or_invented_source(reference):
    with pytest.raises(LLMError, match="原文范围"):
        review_criterion(Gateway(result(evidence=[reference])), RULE, [PAGE, {**PAGE, "page_no": 4}])


@pytest.mark.parametrize("extra", [{"page_no": 4}, {"document_id": 76}, {"quote": "伪造了一条完全不存在于原始文件中的登记证明记录。"}])
def test_source_id_cannot_launder_wrong_page_or_invented_quote(extra):
    gateway = Gateway(lambda p: result(evidence=[{"source_id": p["pages"][0]["source_id"], **extra}]))
    with pytest.raises(LLMError):
        review_criterion(gateway, RULE, [PAGE])


def test_unrelated_capital_does_not_poison_qualification_but_still_blocks_count_score():
    got = review_criterion(Gateway(result(facts={"registered_capital": "9000000"})), RULE, [PAGE])
    assert got.verdict == "pass" and got.facts == {}
    with pytest.raises(LLMError, match="对应原页"):
        review_criterion(Gateway(result(facts={"registered_capital": "9000000"})),
                         {**RULE, "score_type": "count"}, [PAGE])


def test_price_extraction_keeps_only_grounded_facts_even_though_match_is_non_scoring():
    quote = "本项目投标总价为123456元人民币，包含全部服务费用。"
    def reply(payload):
        ref = {"source_id": payload["pages"][0]["source_id"]}
        return result(evidence=[ref], facts={"bid_price": "123456", "fact_evidence": {"bid_price": [ref]}})
    got = review_criterion(Gateway(reply), {**RULE, "score_type": "match", "_extract_price": True},
                           [{**PAGE, "text": quote}])
    assert got.facts["bid_price"] == "123456"
    assert got.facts["fact_evidence"]["bid_price"][0]["quote"] == quote


def test_synthesis_reuses_checked_evidence_ids_and_reports_each_batch(monkeypatch):
    monkeypatch.setattr(auto_review, "TEXT_BUDGET", 2000)
    pages = [{**PAGE, "text": QUOTE + "甲" * 1100}, {**PAGE, "page_no": 4, "text": QUOTE + "乙" * 1100}]
    def reply(payload):
        if payload["batch_results"] is None:
            assert payload["collection_only"]
            return result(evidence=[{"source_id": payload["pages"][0]["source_id"]}])
        assert payload["pages"] == []
        assert payload["allowed_source_ids"] == []
        assert payload["allowed_evidence_ids"] == [item["evidence_id"]
            for batch in payload["batch_results"] for item in batch["evidence"]]
        return result(evidence=[{"evidence_id": item["evidence_id"]}
                                for batch in payload["batch_results"] for item in batch["evidence"]])
    messages = []
    with reporting(messages.append):
        got = review_criterion(Gateway(reply), {**RULE, "score_type": "count"}, pages)
    assert got.coverage_complete and {e["page_no"] for e in got.evidence} == {3, 4}
    assert any("1/2" in message for message in messages)
    assert any("2/2" in message for message in messages)
    assert any("综合" in message for message in messages)


def test_unknown_synthesis_evidence_id_is_rejected(monkeypatch):
    monkeypatch.setattr(auto_review, "TEXT_BUDGET", 2000)
    pages = [{**PAGE, "text": QUOTE + "甲" * 1100}, {**PAGE, "page_no": 4, "text": QUOTE + "乙" * 1100}]
    def reply(payload):
        ref = {"evidence_id": "not-reviewed"} if payload["batch_results"] else {"source_id": payload["pages"][0]["source_id"]}
        return result(evidence=[ref])
    with pytest.raises(LLMError, match="原文范围"):
        review_criterion(Gateway(reply), {**RULE, "score_type": "count"}, pages)


def test_actual_cancel_is_never_retried_as_model_failure(monkeypatch):
    from app.jobs.queue import JobCancelled
    monkeypatch.setenv("BID_EVAL_AUTO_REVIEW_ROUNDS", "2")
    def cancel(message):
        raise JobCancelled()
    with reporting(cancel), pytest.raises(JobCancelled):
        scoring._assess_cell(RULE, [PAGE], "synthetic", lambda: None)


def test_model_requested_continuation_images_are_delivered_in_supplement(monkeypatch):
    monkeypatch.setenv("BID_EVAL_AUTO_REVIEW_ROUNDS", "2")
    pages = [{**PAGE, "page_no": i} for i in range(1, 21)]
    calls = []

    def supplement(criterion, original, *, visual, **kwargs):
        if visual:
            keys = set(criterion["_visual_page_keys"])
            assert keys == {(75, i) for i in range(10, 18)}
            return [{**p, **({"image_url": "real-rendered-image"} if (75, p["page_no"]) in keys else {})} for p in original]
        return original

    def review(gateway, criterion, original, **kwargs):
        calls.append(kwargs["use_vision"])
        if not kwargs["use_vision"]:
            raise LLMError("评审证据尚未解决：第10-17页工商变更信息续页的页图未提供，需核验公章")
        assert all(p.get("image_url") for p in original if 10 <= p["page_no"] <= 17)
        return auto_review.AutoReviewDecision(verdict="pass", reasoning="续页已核验", evidence=[{
            "document_id": 75, "page_no": 10, "kind": "visual", "observation": "续页记录与公章可见",
        }])

    monkeypatch.setattr(scoring, "_supplement_pages", supplement)
    monkeypatch.setattr(auto_review, "review_criterion", review)
    out = scoring._assess_cell(RULE, pages, "synthetic", lambda: None)
    assert out.verdict == "pass" and calls == [False, True]


def test_visual_page_requests_cannot_read_pages_outside_associated_files():
    wanted, keys = scoring._requested_visual_pages({"error": "缺第1-99999页页图"}, [PAGE])
    assert wanted and not keys


def test_document_absence_can_use_complete_ocr_and_supplemental_visual_reading(monkeypatch):
    pages = [PAGE, {**PAGE, "page_no": 4, "unreadable": True, "image_url": "data:image/png;base64,AAAA"}]
    gateway = Gateway(result(verdict="fail", confirmed_absent=True, evidence=[]))
    monkeypatch.setattr(auto_review, "get_vision_gateway", lambda: gateway)
    got = review_criterion(None, {"name": "工商变更记录文件", "score_type": "veto"}, pages, use_vision=True)
    assert got.confirmed_absent and got.coverage_complete
    # Explicit stamp/image absence retains its exhaustive-image requirement:
    # a visual observation may be returned, but the absence conclusion must
    # still cover every non-blank original page image.
    visual_gateway = Gateway(result(verdict="fail", confirmed_absent=True, evidence=[
        {"document_id": 75, "page_no": 4, "kind": "visual", "observation": "第4页未见公章"},
    ]))
    monkeypatch.setattr(auto_review, "get_vision_gateway", lambda: visual_gateway)
    with pytest.raises(LLMError, match="全部非空白"):
        review_criterion(None, {"name": "工商变更记录签章", "score_type": "veto"}, pages, use_vision=True)
