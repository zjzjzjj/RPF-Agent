"""Precise V1 repair guidance preserves the existing evidence acceptance contract."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from app.engine import auto_review as review
from app.llm.base import LLMError


QUOTE = "本页原件载明真实完整的投标响应和服务保障承诺。"
OTHER_QUOTE = "另一页载明项目人员具备有效资格证书和工作经历。"
OBSERVATION = "原页右下角可见投标人红色印章及授权人签字图像"
TEXT = {"document_id": 7, "page_no": 3, "kind": "text", "quote": QUOTE}
VISUAL = {"document_id": 7, "page_no": 3, "kind": "visual", "observation": OBSERVATION,
          "image_sha256": "a" * 64}
SOURCES = {"text-original": TEXT, "text-other": {**TEXT, "page_no": 4, "quote": OTHER_QUOTE},
           "visual-original": VISUAL}
PAGE = {"document_id": 7, "page_no": 3, "text": QUOTE, "source": "text"}


@pytest.mark.parametrize("field,incorrect", [("document_id", 8), ("page_no", 99), ("kind", "visual")])
def test_conflicting_reference_reports_exact_nested_field_and_true_binding(field, incorrect):
    value = {"facts": {"verified_cases": [{"evidence": [{"source_id": "text-original", field: incorrect}]}]}}
    with pytest.raises(LLMError) as failure:
        review._resolve_source_ids(value, SOURCES)
    message = str(failure.value)
    assert f"$.facts.verified_cases[0].evidence[0].{field}" in message
    assert "$.facts.verified_cases[0].evidence[0].source_id" in message
    assert '原ID "text-original"' in message
    assert 'document_id=7, page_no=3, kind="text"' in message
    if field == "kind":
        assert "文本ID不能充当视觉证据" in message
        assert "不填文本source_id/evidence_id" in message
    else:
        assert "只返回该原ID" in message


def test_quote_error_locates_escaped_json_key_without_echoing_quote():
    fabricated = "此段编造的资格证书文字根本没有出现在所绑定的原页。"
    value = {"facts": {"price detail": [{"evidence_id": "text-original", "quote": fabricated}]}}
    with pytest.raises(LLMError) as failure:
        review._resolve_source_ids(value, SOURCES)
    message = str(failure.value)
    assert '$.facts["price detail"][0].quote' in message
    assert '原ID "text-original"' in message and "不要重抄quote" in message
    assert fabricated not in message


def test_visual_reference_with_wrong_page_keeps_visual_repair_guidance():
    with pytest.raises(LLMError) as failure:
        review._resolve_source_ids({"evidence": [{"source_id": "text-original", "page_no": 99,
                                                 "kind": "visual", "observation": OBSERVATION}]}, SOURCES)
    message = str(failure.value)
    assert "$.evidence[0].page_no" in message and "文本ID不能充当视觉证据" in message
    assert "只返回该原ID" not in message


def test_unknown_id_suggests_only_three_same_hint_page_ids_without_resolving():
    sources = {**{f"same-page-{index}": TEXT for index in range(5)}, **SOURCES,
               "different-owner": {**TEXT, "document_id": 9}}
    value = {"evidence": [{"source_id": "mistyped-original", "document_id": 7, "page_no": 3}]}
    with pytest.raises(LLMError) as failure:
        review._resolve_source_ids(value, sources)
    message = str(failure.value)
    assert "$.evidence[0].source_id" in message and '原ID "mistyped-original"' in message
    assert "该原ID未知" in message
    assert all(f"same-page-{index}" in message for index in range(3))
    assert all(reference not in message for reference in ("same-page-3", "same-page-4", "text-other", "different-owner"))


def test_ambiguous_exact_quote_still_fails_with_field_and_original_id():
    sources = {"one": TEXT, "two": {**TEXT, "page_no": 4}}
    with pytest.raises(LLMError) as failure:
        review._resolve_source_ids({"evidence": [{"source_id": "unknown", "quote": QUOTE}]}, sources)
    message = str(failure.value)
    assert "$.evidence[0].source_id" in message and '原ID "unknown"' in message and "歧义" in message


@pytest.mark.parametrize("has_visual_candidate", [True, False])
def test_unknown_visual_id_never_suggests_text_id_or_accepts_made_up_id(has_visual_candidate):
    sources = {"text-one": TEXT, "text-two": TEXT,
               "other-page-visual": {**VISUAL, "page_no": 4}}
    if has_visual_candidate:
        sources["checked-visual"] = VISUAL
    with pytest.raises(LLMError) as failure:
        review._resolve_source_ids({"evidence": [{"source_id": "d7p3_visual", "document_id": 7,
                                                 "page_no": 3, "kind": "visual", "observation": OBSERVATION}]}, sources)
    message = str(failure.value)
    assert "$.evidence[0].source_id" in message and '原ID "d7p3_visual"' in message
    assert "不得生成或改写ID" in message and "初评没有可用visual ID" in message
    assert "不填source_id/evidence_id" in message and "综合时只选已核验visual" in message
    assert all(reference not in message for reference in ("text-one", "text-two", "other-page-visual"))
    assert ("checked-visual" in message) is has_visual_candidate


@pytest.mark.parametrize("reference", [
    {"source_id": "text-original", "document_id": 8, "page_no": 3, "quote": QUOTE},
    {"source_id": "unknown", "document_id": 8, "page_no": 3, "quote": QUOTE},
    {"source_id": "unknown", "document_id": 7, "page_no": 3, "quote": QUOTE.replace("响应", "应答")},
    {"evidence_id": "visual-original", "kind": "text"},
    {"evidence_id": "visual-original", "observation": "改写为原批次从未观察的签章结论"},
])
def test_feedback_does_not_accept_changed_ownership_fuzzy_quotes_or_visual_observations(reference):
    with pytest.raises(LLMError):
        review._resolve_source_ids({"evidence": [reference]}, SOURCES)


@pytest.mark.parametrize("value,expected", [
    ({"evidence": [{"source_id": "text-original"}]},
     {"evidence": [{"source_id": "text-original", **TEXT}]}),
    ({"facts": {"evidence": [{"source_id": "text-original", "document_id": "7", "page_no": "3"}]}},
     {"facts": {"evidence": [{"source_id": "text-original", **TEXT}]}}),
    ({"evidence": [{"source_id": "text-original", "quote": "原件载明真实完整的投标响应和服务保障承诺。"}]},
     {"evidence": [{"source_id": "text-original", **TEXT, "quote": "原件载明真实完整的投标响应和服务保障承诺。"}]}),
    ({"evidence": [{"source_id": "mistyped-but-exact", "document_id": 7, "page_no": 3, "quote": QUOTE}]},
     {"evidence": [{"source_id": "mistyped-but-exact", **TEXT}]}),
    ({"evidence": [{"evidence_id": "visual-original"}]},
     {"evidence": [{"evidence_id": "visual-original", **VISUAL}]}),
    ({"evidence": [{"source_id": "text-original", "evidence_id": "visual-original"}]},
     {"evidence": [{"source_id": "text-original", "evidence_id": "visual-original", **VISUAL}]}),
    ({"source_id": "text-original", "evidence_id": None, "document_id": 999},
     {"source_id": "text-original", "evidence_id": None, "document_id": 999}),
    ({"sub_scores": [{"name": "原子项", "score": "0", "confirmed_absent": False}], "other": [0, None, True]},
     {"sub_scores": [{"name": "原子项", "score": "0", "confirmed_absent": False}], "other": [0, None, True]}),
])
def test_accepted_resolution_matches_frozen_legacy_output_without_input_mutation(value, expected):
    original_value, original_sources = deepcopy(value), deepcopy(SOURCES)
    assert review._resolve_source_ids(value, SOURCES) == expected
    assert value == original_value and SOURCES == original_sources


class RepairGateway:
    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    def chat_json(self, system, user, response_model, **kwargs):
        payload = json.loads(user.removeprefix("<review>\n").removesuffix("\n</review>"))
        self.calls.append({"system": system, "payload": payload, **kwargs})
        evidence, facts = self.respond(payload, len(self.calls))
        return response_model(verdict="pass", reasoning="依据原页核验本项要求", evidence=evidence, facts=facts), None


def test_nested_fact_reference_is_corrected_from_specific_feedback_on_second_attempt():
    def respond(payload, attempt):
        reference = {"source_id": payload["allowed_source_ids"][0]}
        nested = {**reference, "page_no": 99} if attempt == 1 else reference
        if attempt == 2:
            feedback = payload["validation_feedback"]["error"]
            assert "$.facts.verified_cases[0].evidence[0].page_no" in feedback
            assert 'page_no=3' in feedback and "只返回该原ID" in feedback
        return [reference], {"verified_cases": [{"name": "真实案例", "evidence": [nested]}]}

    gateway = RepairGateway(respond)
    result = review._call(gateway, {"name": "项目案例", "score_type": "count"}, [PAGE],
                          data_class="synthetic", previous=None, round_no=0, use_vision=False)
    assert len(gateway.calls) == 2
    assert result.facts["verified_cases"][0]["evidence"][0]["page_no"] == 3
    assert [row["state"] for row in result.validation_history] == ["failed", "completed"]
    first, second = (call["payload"] for call in gateway.calls)
    assert first == {key: value for key, value in second.items() if key != "validation_feedback"}
    assert gateway.calls[0]["system"] == gateway.calls[1]["system"] == review.SYSTEM_PROMPT


@pytest.mark.parametrize("has_image", [True, False])
def test_text_id_as_visual_requires_actual_page_observation_and_never_downgrades_to_text(has_image):
    def respond(payload, attempt):
        if attempt == 1:
            reference = {"source_id": payload["allowed_source_ids"][0], "kind": "visual", "observation": OBSERVATION}
        else:
            feedback = payload["validation_feedback"]["error"]
            if attempt == 2:
                assert "$.evidence[0].kind" in feedback and 'kind="text"' in feedback
                assert "文本ID不能充当视觉证据" in feedback and "不填文本source_id/evidence_id" in feedback
                assert len(feedback) <= 500
            reference = {"document_id": 7, "page_no": 3, "kind": "visual", "observation": OBSERVATION}
        return [reference], {}

    gateway = RepairGateway(respond)
    page = {**PAGE, **({"image_url": "data:image/png;base64,AAAA", "image_sha256": "a" * 64} if has_image else {})}
    if not has_image:
        with pytest.raises(LLMError, match="实际送审页图"):
            review._call(gateway, {"name": "签章核验", "score_type": "match"}, [page],
                         data_class="synthetic", previous=None, round_no=1, use_vision=True)
        assert len(gateway.calls) == 3
        return
    result = review._call(gateway, {"name": "签章核验", "score_type": "match"}, [page],
                          data_class="synthetic", previous=None, round_no=1, use_vision=True)
    assert len(gateway.calls) == 2
    assert result.evidence[0]["kind"] == "visual" and result.evidence[0]["quote"] == ""
    assert result.evidence[0]["observation"] == OBSERVATION
    assert "source_id" not in result.evidence[0]


def test_checked_visual_evidence_id_keeps_original_observation_after_repair():
    visual = {"evidence_id": "visual-original", **VISUAL}

    def respond(payload, attempt):
        if attempt == 1:
            return [{"evidence_id": "visual-original", "observation": "原批次未核验的改写观察"}], {}
        assert "$.evidence[0].observation" in payload["validation_feedback"]["error"]
        return [{"evidence_id": "visual-original"}], {}

    gateway = RepairGateway(respond)
    page = {**PAGE, "image_url": "data:image/png;base64,AAAA", "image_sha256": "a" * 64}
    result = review._call(gateway, {"name": "签章核验", "score_type": "match"}, [page],
                          data_class="synthetic", previous=None, round_no=1, use_vision=True,
                          summaries=[{"evidence": [visual]}], send_images=False)
    assert len(gateway.calls) == 2 and result.evidence[0] == {**visual, "quote": ""}


def test_unknown_visual_id_is_repaired_with_sent_page_observation_without_generated_id():
    def respond(payload, attempt):
        reference = {"document_id": 7, "page_no": 3, "kind": "visual", "observation": OBSERVATION}
        if attempt == 1:
            reference["source_id"] = "d7p3_visual"
        else:
            feedback = payload["validation_feedback"]["error"]
            assert "$.evidence[0].source_id" in feedback and '原ID "d7p3_visual"' in feedback
            assert "不得生成或改写ID" in feedback and "不填source_id/evidence_id" in feedback
            assert "合法候选ID" not in feedback and len(feedback) <= 500
        return [reference], {}

    gateway = RepairGateway(respond)
    page = {**PAGE, "image_url": "data:image/png;base64,AAAA", "image_sha256": "a" * 64}
    result = review._call(gateway, {"name": "签章核验", "score_type": "match"}, [page],
                          data_class="synthetic", previous=None, round_no=1, use_vision=True)
    assert len(gateway.calls) == 2 and result.evidence[0]["kind"] == "visual"
    assert result.evidence[0]["observation"] == OBSERVATION and "source_id" not in result.evidence[0]
