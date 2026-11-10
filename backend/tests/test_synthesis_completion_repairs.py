"""Final synthesis repairs preserve collected work, provenance and exact limits."""
from copy import deepcopy
import json

import pytest

from app.engine import auto_review as review
from app.llm.base import LLMError
from app.services.execution_version import bind_run_execution
from app.services.automatic_scoring_service import _verified_price_limit


def test_packed_groups_restore_conflicting_facts_missing_requests_and_audit_exactly():
    originals = []
    groups = []
    for index in range(70):
        missing = f"第{index}批未见法定代表人授权委托书及投标人代表签字（或盖章）页面"
        history = [{"attempt": 0, "error": "原引用必须重新核验", "state": "failed"}]
        originals.append({"reasoning": f"第{index}批完整原始推理", "validation_history": history})
        groups.append({"group_id": index, "complete_results": [{
            "verdict": "unverifiable", "reasoning": {"original_reasoning_ref": index},
            "facts": {"amount": str(index), "conflict": ["1", "2"], "zero": 0, "flag": False},
            "missing_evidence": [missing, "法定代表人授权委托书及投标人代表签字（或盖章）页面"],
            "evidence": [index], "validation_history": history, "confidence": 0,
        }], "verification": {"reasoning": "核验本组法定代表人授权委托书及投标人代表签字（或盖章）页面，保留未决项",
                              "missing_evidence": [missing]}})
    original_groups = deepcopy(groups)
    packed = review._pack_synthesis_groups(groups, originals)[0]
    assert review._restore_packed_synthesis_groups(packed, originals) == original_groups
    assert groups == original_groups
    assert packed["text_fragments"]
    assert len(json.dumps(packed, ensure_ascii=False).encode()) < len(json.dumps(groups, ensure_ascii=False).encode())
    for group in packed["groups"]:
        fields = packed["result_fields"][group["complete_result_rows"][0][0]]
        assert "facts" in fields and "original_validation_history_index" in fields


def test_shared_text_parts_preserve_whitespace_control_characters_and_unicode():
    text = "必须逐字保存授权委托书与原始证据，" * 10
    values = [text + suffix for suffix in ("\n\t", "\x00", "😀", "", "\r\n")]
    packed, fragments = review._pack_synthesis_texts(values)
    assert [review._restore_synthesis_text(value, fragments) for value in packed] == values


def test_many_distinct_repeated_requests_fit_without_losing_any_request():
    import random
    import string

    randomizer = random.Random(2649)
    values = ["".join(randomizer.choices(string.ascii_letters, k=40)) * 4 for _ in range(48)]
    packed, fragments = review._pack_synthesis_texts(values)
    assert [review._restore_synthesis_text(value, fragments) for value in packed] == values
    assert len(json.dumps([packed, fragments])) < len(json.dumps(values)) * 0.55


def test_visual_synthesis_repairs_text_only_result_without_resending_images():
    quote = "投标人已经提供有效授权委托书原件及签章页。"
    page = {"document_id": 7, "page_no": 3, "text": quote,
            "image_url": "checkpoint://image", "image_sha256": "a" * 64}
    evidence = [
        {"evidence_id": "b0e0", "document_id": 7, "page_no": 3, "kind": "text", "quote": quote},
        {"evidence_id": "b0e1", "document_id": 7, "page_no": 3, "kind": "visual",
         "observation": "授权委托书原页可见公司公章与双方签名。", "image_sha256": "a" * 64},
    ]
    calls = []

    class Gateway:
        def chat_json(self, system, user, response_model, **kwargs):
            payload = json.loads(user.removeprefix("<review>\n").removesuffix("\n</review>"))
            calls.append(payload)
            assert not kwargs.get("images")
            if len(calls) > 1:
                assert "verified_visual_evidence_ids" in payload["validation_feedback"]["error"]
            return response_model(verdict="pass", reasoning="原页签章及授权已核验",
                evidence=[{"evidence_id": "b0e0" if len(calls) == 1 else "b0e1"}]), None

    result = review._call(Gateway(), {"name": "盖章签字及授权", "score_type": "veto"}, [page],
        data_class="synthetic", previous=None, round_no=1, use_vision=True, send_images=False,
        summaries=[{"evidence": evidence}])
    assert len(calls) == 2 and result.evidence[0]["kind"] == "visual"
    assert calls[0]["verified_visual_evidence_ids"] == ["b0e1"]


def test_unknown_synthesis_reference_is_rejected_with_actual_allowed_examples():
    with pytest.raises(LLMError) as failure:
        review._resolve_source_ids({"evidence": [{"evidence_id": "b20e0"}]}, {
            "b19e0": {"document_id": 7, "page_no": 3, "kind": "text", "quote": "合法原页的完整证据内容。"}})
    assert "必须删除这一无来源引用" in str(failure.value)
    assert "b19e0" in str(failure.value) and '$.evidence[0].evidence_id' in str(failure.value)


def test_dense_synthesis_ids_bind_original_sources_and_packed_row_references():
    ids = ["b2e0", "b19e1", "b120e0"]
    sources = {key: {"evidence_id": key, "document_id": 7, "page_no": index + 1,
                     "kind": "text", "quote": f"第{index + 1}页独立而完整的原始投标证据。"}
               for index, key in enumerate(ids)}
    payload = {"allowed_evidence_ids": ids, "verified_visual_evidence_ids": [ids[1]], "batch_results": [{
        "format": "complete-result-rows-v1", "result_fields": [["evidence", "facts"]],
        "groups": [{"complete_result_rows": [[0, [0, 2], {"fact_evidence": {
            "amount": [{"evidence_id": ids[1]}]}}]], "verification": {"evidence": [1]}}],
    }]}
    original = deepcopy(payload)
    encoded, lookup = review._synthesis_request_aliases(payload, ids, sources)
    assert payload == original and encoded["allowed_evidence_ids"] == ["R0", "R1", "R2"]
    group = encoded["batch_results"][0]["groups"][0]
    assert group["complete_result_rows"][0][1] == ["R0", "R2"]
    assert group["verification"]["evidence"] == ["R1"]
    resolved = review._resolve_source_ids({"evidence": [{"evidence_id": "R2"}]}, lookup)
    assert resolved["evidence"] == [sources[ids[2]]]
    with pytest.raises(LLMError):
        review._resolve_source_ids({"evidence": [{"evidence_id": "b20e0"}]}, lookup)


def test_compact_reference_spelling_keeps_exact_ledger_binding():
    source = {"evidence_id": "b12e0", "document_id": 7, "page_no": 3, "bidder_id": 58,
              "kind": "text", "quote": "原件中已经核实的投标人及报价来源。"}
    result = review.AutoReviewResponse(verdict="pass", reasoning="来源已核验", evidence=["RA"])
    restored = review._resolve_source_ids(result.model_dump(), {"RA": source})
    assert restored["evidence"] == [source]
    unknown = review.AutoReviewResponse(verdict="pass", reasoning="待来源检查", evidence=["RB"])
    with pytest.raises(LLMError, match="原ID未知"):
        review._resolve_source_ids(unknown.model_dump(), {"RA": source})


@pytest.mark.parametrize("reference", [0, "原文引文不等于证据编号", "b20e0"])
def test_compact_references_never_infer_positions_or_unregistered_text(reference):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        review.AutoReviewResponse(verdict="pass", reasoning="不能猜测来源", evidence=[reference])


def price_rules():
    return [
        {"id": 11, "name": "投标报价不得超过最高限价", "score_type": "veto", "check_type": "math_relation",
         "source_page": 7, "source_quote": "最高限价共300元，每年100元。",
         "params": {"max_amount_yuan": "300", "yearly_max_amount_yuan": ["100", "100", "100"]}},
        {"id": 27, "name": "投标报价最高限价及清单分项报价", "score_type": "veto", "check_type": "math_relation",
         "params": {"relation": "报价不得超最高限价；工程项目还需检查清单分项报价"},
         "_frozen_bidder": {"price_status": "confirmed", "bid_price": "240"}},
    ]


def test_generic_ceiling_resolves_only_frozen_explicit_limits_without_mutation():
    rules = price_rules()
    original = deepcopy(rules)
    with bind_run_execution({"criteria": rules}, 1):
        resolved = review.resolve_price_limit_requirement(rules[1])
    assert rules == original and review.is_price_limit_requirement(resolved)
    assert resolved["params"]["yearly_max_amount_yuan"] == ["100", "100", "100"]
    assert resolved["_price_limit_rule_sources"][0]["source_page"] == 7
    missing = review.AutoReviewDecision(verdict="fail", reasoning="完整查阅确认没有分年度报价",
        confirmed_absent=True, coverage_complete=True)
    assert _verified_price_limit(resolved, missing)[0] == "fail"
    missing.confirmed_absent = False
    with pytest.raises(LLMError, match="不能以总价代替年度报价"):
        _verified_price_limit(resolved, missing)
    missing.facts = {f"yearly_bid_price_{index}": "80" for index in (1, 2, 3)}
    # Numeric compliance cannot override another applicable condition's failure.
    assert _verified_price_limit(resolved, missing)[0] == "fail"


def test_conflicting_frozen_limits_fail_instead_of_selecting_one():
    rules = price_rules()
    rules.append({**rules[0], "id": 12, "params": {"max_amount_yuan": "400"}})
    with bind_run_execution({"criteria": rules}, 1):
        with pytest.raises(LLMError, match="不同限额"):
            review.resolve_price_limit_requirement(rules[1])


def test_collection_never_inherits_new_final_price_context():
    rules = price_rules()
    payloads = []

    class Gateway:
        def chat_json(self, system, user, response_model, **kwargs):
            payload = json.loads(user.removeprefix("<review>\n").removesuffix("\n</review>"))
            payloads.append(payload)
            return response_model(verdict="unverifiable", reasoning="本批仅采集，不推定全文缺失"), None

    with bind_run_execution({"criteria": rules, "prompt_version": "frozen-test"}, 1):
        review._call(Gateway(), rules[1], [], data_class="synthetic", previous=None,
                     round_no=1, use_vision=False, collection_only=True)
    assert payloads[0]["rule"] == rules[1] and payloads[0]["fact_schema"] == {}


@pytest.mark.parametrize("unreadable,full,imaged,expected", [
    (False, True, False, True), (True, True, False, False),
    (False, False, False, False), (True, True, True, True),
])
def test_current_reading_state_never_hides_incomplete_or_unreadable_material(unreadable, full, imaged, expected):
    pages = [{"document_id": 7, "page_no": 1, "text": "已提交核验的原文", "unreadable": unreadable},
             {"document_id": 8, "page_no": 1, "text": "第二家投标人的原文"}]
    if imaged:
        pages[0]["image_url"] = "checkpoint://original-image"
    selected = pages if full else pages[:1]
    state = review._material_review_state(pages, selected, full=full, use_vision=imaged)
    assert state["all_required_pages_readable"] is expected
    assert state["required_page_count"] == 2 and len(state["documents"]) == 2
    assert state["reviewed_page_count"] == len(selected)
    assert len(state["unresolved_read_pages"]) == int(unreadable and not imaged)


def test_final_review_sees_current_reading_while_collection_and_prior_stay_frozen():
    page = {"document_id": 7, "page_no": 1, "text": "已经重新读取核验过的原始材料。"}
    state = review._material_review_state([page], [page], full=True, use_vision=False)
    previous = {"error": "关联材料存在不可读页的读取故障，先补做 OCR 或视觉核验再评审", "round": 0}
    payloads = []

    class Gateway:
        def chat_json(self, system, user, response_model, **kwargs):
            payloads.append(json.loads(user.removeprefix("<review>\n").removesuffix("\n</review>")))
            return response_model(verdict="unverifiable", reasoning="本轮已核查材料，仅缺外部平台记录"), None

    rule = {"score_type": "veto", "name": "串通投标"}
    parameters = dict(data_class="synthetic", previous=previous, round_no=1,
                      use_vision=False, material_state=state)
    review._call(Gateway(), rule, [page], collection_only=True, **parameters)
    review._call(Gateway(), rule, [page], summaries=[{"evidence": []}], **parameters)
    assert "current_material_state" not in payloads[0]
    assert payloads[1]["current_material_state"] == state
    assert payloads[0]["prior"] == payloads[1]["prior"] == previous


def test_known_unreadable_source_enters_visual_recovery_without_spending_semantic_retries():
    pages = [{"document_id": 80, "page_no": 61, "text": "OCR只有少量文字", "unreadable": True}]
    state = review._material_review_state(pages, pages, full=True, use_vision=False)

    class Gateway:
        def chat_json(self, *args, **kwargs):
            pytest.fail("known unreadable source must be recovered before model synthesis")

    with pytest.raises(LLMError, match="doc80第61页") as failure:
        review.synthesis_decision(Gateway(), {"score_type": "veto", "name": "与招标人存在利害关系"},
            pages, data_class="synthetic", previous={}, round_no=1, use_vision=False,
            summaries=[], decisions=[], material_state=state)
    assert not failure.value.review_attempts_exhausted
    from app.services.automatic_scoring_service import _requested_visual_pages

    requested, targets = _requested_visual_pages({"error": str(failure.value)}, pages)
    assert requested and targets == {(80, 61)}


def test_ocr_with_unresolved_pages_can_reach_last_visual_round(monkeypatch):
    from app.services import automatic_scoring_service as scoring

    monkeypatch.setenv("BID_EVAL_AUTO_REVIEW_ROUNDS", "2")
    pages = [{"document_id": 80, "page_no": 61, "text": "扫描正文尚未读全", "unreadable": True}]
    rounds, supplements = [], []

    def supplement(rule, current, *, visual, **kwargs):
        supplements.append((visual, rule.get("_visual_page_keys")))
        return [{**page, **({"image_url": "checkpoint://original-image"} if visual else {})} for page in current]

    def reviewer(gateway, rule, current, **kwargs):
        rounds.append((kwargs["round_no"], kwargs["use_vision"]))
        state = review._material_review_state(current, current, full=True, use_vision=kwargs["use_vision"])
        if not state["all_required_pages_readable"]:
            return review.synthesis_decision(gateway, rule, current, data_class="synthetic",
                previous=kwargs["previous"], round_no=kwargs["round_no"],
                use_vision=False, summaries=[], decisions=[], material_state=state)
        return review.AutoReviewDecision(verdict="unverifiable", reasoning="原页已核验，仅缺外部关联登记记录",
                                         coverage_complete=True)

    monkeypatch.setattr(scoring, "_supplement_pages", supplement)
    outcome = scoring._assess_cell({"name": "与招标人存在利害关系", "score_type": "veto"},
                                  pages, "synthetic", lambda: object(), reviewer=reviewer)
    assert rounds == [(1, False), (2, True)]
    assert supplements == [(False, None), (True, [(80, 61)])]
    assert outcome.basis == "external" and outcome.attempts == 3


def test_unresolved_final_is_repaired_from_complete_batches_without_recollection():
    calls = []

    class Gateway:
        def chat_json(self, system, user, response_model, **kwargs):
            payload = json.loads(user.removeprefix("<review>\n").removesuffix("\n</review>"))
            calls.append(payload)
            if len(calls) == 1:
                return response_model(verdict="fail", reasoning="尚需区分真实缺失与读取故障",
                                      missing_evidence=["年度报价明细"]), None
            assert "真实缺失" in payload["validation_feedback"]["error"]
            return response_model(verdict="fail", reasoning="全部可读批次确认未提供年度报价明细",
                                  confirmed_absent=True), None

    result = review._call(Gateway(), {"score_type": "veto", "name": "必需报价明细"}, [],
        data_class="synthetic", previous=None, round_no=2, use_vision=False, summaries=[{"evidence": []}])
    assert result.confirmed_absent and len(calls) == 2
    assert calls[0]["batch_results"] == calls[1]["batch_results"]


@pytest.mark.parametrize("unreadable", [False, True])
def test_external_record_boundary_reaches_coverage_gate_before_completion(unreadable):
    rule = {"score_type": "veto", "name": "与招标人存在利害关系"}
    page = {"document_id": 7, "page_no": 1, "text": "投标文件中的工商及主体信息已核查。",
            "unreadable": unreadable}

    class Gateway:
        def chat_json(self, system, user, response_model, **kwargs):
            return response_model(verdict="unverifiable", reasoning="已核查文件内主体关系，外部关联登记未提供",
                                  missing_evidence=["招标人关联方及利害关系登记记录"]), None

    result = review._call(Gateway(), rule, [page], data_class="synthetic", previous=None,
                         round_no=2, use_vision=False, summaries=[{"evidence": []}])
    parameters = dict(selected=[page], batches=[[page]], full=True, use_vision=False)
    if unreadable:
        with pytest.raises(LLMError, match="全部可读原页"):
            review.finish_decision(result, rule, [page], **parameters)
    else:
        final = review.finish_decision(result, rule, [page], **parameters)
        assert final.verdict == "unverifiable" and final.coverage_complete
        assert final.missing_evidence == [] and "利害关系登记记录" in final.reasoning


def test_external_boundary_does_not_disguise_model_reported_reading_failure():
    rule = {"score_type": "veto", "name": "与招标人存在利害关系"}
    page = {"document_id": 7, "page_no": 1, "text": "投标文件中的工商及主体信息。"}
    result = review.AutoReviewDecision(verdict="unverifiable", reasoning="仍存在未核验图像",
                                      missing_evidence=["关联材料中不可读页的OCR或视觉核验结果"])
    with pytest.raises(LLMError, match="读取或证据核验故障"):
        review.finish_decision(result, rule, [page], selected=[page], batches=[[page]], full=True, use_vision=False)


@pytest.mark.parametrize("unreadable", [False, True])
def test_confirmed_price_absence_is_finalized_only_after_complete_readable_coverage(unreadable):
    rules = price_rules()
    page = {"document_id": 7, "page_no": 1, "text": "完整可读的本次投标文件内容。", "unreadable": unreadable}
    missing = ["第一年度、第二年度、第三年度投标报价明细原页", "工程量清单分项报价明细表（如采用清单招标）"]

    class Gateway:
        def chat_json(self, system, user, response_model, **kwargs):
            return response_model(verdict="fail", reasoning="完整查阅确认未提供必需分年度报价",
                                  confirmed_absent=True, missing_evidence=missing), None

    with bind_run_execution({"criteria": rules, "prompt_version": "frozen-test"}, 1):
        result = review._call(Gateway(), rules[1], [page], data_class="synthetic", previous=None,
                             round_no=1, use_vision=False, summaries=[{"evidence": []}])
        if unreadable:
            with pytest.raises(LLMError, match="全部可读原页"):
                review.finish_decision(result, rules[1], [page], selected=[page], batches=[[page]], full=True, use_vision=False)
        else:
            final = review.finish_decision(result, rules[1], [page], selected=[page], batches=[[page]], full=True, use_vision=False)
            assert final.confirmed_missing_evidence == missing and not final.missing_evidence
            assert _verified_price_limit(review.resolve_price_limit_requirement(rules[1]), final)[0] == "fail"


@pytest.mark.parametrize("full,unreadable", [(True, False), (False, False), (True, True)])
def test_confirmed_certificate_absence_requires_full_readable_coverage(full, unreadable):
    rule = {"name": "投标人资质实力", "score_type": "match"}
    pages = [{"document_id": 7, "page_no": 1, "text": "已查阅证书目录和全部资质材料。",
              "unreadable": unreadable}]
    missing = ["信息技术服务认证证书及全国认证认可信息公共服务平台查询截图（全文未提供）",
               "音视频集成能力证书在指定平台的查询截图"]

    class Gateway:
        def chat_json(self, system, user, response_model, **kwargs):
            return response_model(verdict="fail", reasoning="全文查阅确认未提供指定证明材料",
                                  confirmed_absent=True, missing_evidence=missing), None

    result = review._call(Gateway(), rule, pages, data_class="synthetic", previous=None,
                         round_no=1, use_vision=False, summaries=[{"evidence": []}])
    if not full or unreadable:
        with pytest.raises(LLMError, match="全部可读原页"):
            review.finish_decision(result, rule, pages, selected=pages, batches=[pages],
                                   full=full, use_vision=False)
    else:
        final = review.finish_decision(result, rule, pages, selected=pages, batches=[pages],
                                       full=True, use_vision=False)
        assert final.verdict == "fail" and final.confirmed_missing_evidence == missing
        assert not final.missing_evidence and final.coverage_complete and final.score is None


@pytest.mark.parametrize("missing", ["证书案例核验失败，需重新核验", "证书扫描页图尚未视觉核验",
                                    "证书查询截图引文错误", "证书OCR超时", "证书无法定位"])
def test_certificate_technical_obligation_cannot_be_confirmed_absence(missing):
    calls = []

    class Gateway:
        def chat_json(self, system, user, response_model, **kwargs):
            calls.append(user)
            return response_model(verdict="fail", reasoning="存在未解决技术故障",
                                  confirmed_absent=True, missing_evidence=[missing]), None

    with pytest.raises(LLMError, match="仍有未解决证据"):
        review._call(Gateway(), {"name": "投标人资质实力", "score_type": "match"}, [],
                     data_class="synthetic", previous=None, round_no=1, use_vision=False,
                     summaries=[{"evidence": []}])
    assert len(calls) == 3


def test_non_scoring_synthesis_repairs_model_score_without_recollecting():
    calls = []

    class Gateway:
        def chat_json(self, system, user, response_model, **kwargs):
            payload = json.loads(user.removeprefix("<review>\n").removesuffix("\n</review>"))
            calls.append(payload)
            return response_model(verdict="fail", reasoning="完整查阅确认缺少必需证书",
                                  confirmed_absent=True, score=2 if len(calls) == 1 else None), None

    result = review._call(Gateway(), {"name": "投标人资质实力", "score_type": "match"}, [],
                         data_class="synthetic", previous=None, round_no=1, use_vision=False,
                         summaries=[{"evidence": []}])
    assert len(calls) == 2 and result.score is None
    assert calls[0]["batch_results"] == calls[1]["batch_results"]
    assert "score必须为null" in calls[0]["final_document_policy"]


def test_certificate_with_company_seal_requires_original_page_visual_evidence():
    from app.services.automatic_scoring_service import _is_visual_requirement

    rule = {"name": "投标人资质实力", "score_type": "match",
            "description": "提供有效资质证书扫描件，并加盖投标人公章"}
    assert _is_visual_requirement(rule)
    page = {"document_id": 7, "page_no": 1, "text": "资质证书及查询截图正文"}
    result = review.AutoReviewDecision(verdict="fail", reasoning="证明材料未提供", confirmed_absent=True)
    with pytest.raises(LLMError, match="全部非空白原页图像"):
        review.finish_decision(result, rule, [page], selected=[page], batches=[[page]],
                               full=True, use_vision=False)


def test_visual_certificate_batch_repairs_text_only_evidence_in_same_batch():
    page = {"document_id": 7, "page_no": 1, "text": "资质证书及查询截图正文，须核验公章图像。",
            "image_url": "checkpoint://original-page", "image_sha256": "a" * 64}
    calls = []

    class Gateway:
        def chat_json(self, system, user, response_model, **kwargs):
            payload = json.loads(user.removeprefix("<review>\n").removesuffix("\n</review>"))
            calls.append(payload)
            assert kwargs["images"] == [page["image_url"]]
            evidence = ([{"source_id": payload["allowed_source_ids"][0]}] if len(calls) == 1
                        else [{"document_id": 7, "page_no": 1, "kind": "visual",
                               "observation": "原页可见证书表格及右下角红色公司公章。"}])
            return response_model(verdict="pass", reasoning="按原件记录本批证书信息", evidence=evidence), None

    result = review._call(Gateway(), {"name": "投标人资质实力", "score_type": "match",
                                     "description": "提供证书扫描件并加盖投标人公章"}, [page],
                         data_class="synthetic", previous=None, round_no=1, use_vision=True,
                         collection_only=True)
    assert len(calls) == 2 and result.evidence[0]["kind"] == "visual"
    assert "图像观察证据" in calls[1]["validation_feedback"]["error"]


def test_mixed_visual_batch_binds_images_to_original_pages_in_attachment_order():
    pages = [
        {"document_id": 7, "page_no": 16, "bidder_id": 61, "text": "上下文文字页，未发送该页图像。"},
        {"document_id": 7, "page_no": 17, "bidder_id": 61, "text": "第二页另有多段文字。" * 80,
         "image_url": "checkpoint://page17", "image_sha256": "a" * 64},
        {"document_id": 8, "page_no": 18, "bidder_id": 62, "text": "本批另一个投标人的实际原件页。",
         "image_url": "checkpoint://page18", "image_sha256": "b" * 64},
    ]
    seen = []

    class Gateway:
        def chat_json(self, system, user, response_model, **kwargs):
            payload = json.loads(user.removeprefix("<review>\n").removesuffix("\n</review>"))
            seen.append(payload)
            assert kwargs["images"] == ["checkpoint://page17", "checkpoint://page18"]
            assert payload["image_sources"] == [
                {"image_index": 1, "document_id": 7, "page_no": 17, "bidder_id": 61},
                {"image_index": 2, "document_id": 8, "page_no": 18, "bidder_id": 62},
            ]
            return response_model(verdict="unverifiable", reasoning="本批仅记录实际图像观察，待综合判断",
                                  evidence=[{"document_id": 7, "page_no": 17, "kind": "visual",
                                             "observation": "本页为方案文字，未见资质证书图像。"}]), None

    result = review._call(Gateway(), {"name": "资质公章核验", "score_type": "match"}, pages,
                         data_class="synthetic", previous=None, round_no=1, use_vision=True,
                         collection_only=True)
    assert len(seen) == 1 and result.evidence[0]["page_no"] == 17


def test_text_only_call_keeps_existing_request_without_image_contract():
    class Gateway:
        def chat_json(self, system, user, response_model, **kwargs):
            payload = json.loads(user.removeprefix("<review>\n").removesuffix("\n</review>"))
            assert "image_sources" not in payload and "original_image_policy" not in payload
            assert "images" not in kwargs
            return response_model(verdict="pass", reasoning="本批文字依据", evidence=[]), None

    review._call(Gateway(), {"name": "文字条款", "score_type": "match"}, [],
                 data_class="synthetic", previous=None, round_no=0, use_vision=False,
                 collection_only=True)


def test_material_batches_split_before_dispatch_when_repairs_would_exceed_context(monkeypatch):
    from app.llm.request_sizing import ReviewContextExceeded

    pages = [{"document_id": 7, "page_no": number, "text": "投标文件中的原页实际响应文字。" * 35}
             for number in (1, 2)]
    calls = []

    def fits(system, user, images=None):
        payload = json.loads(user.split("</review>", 1)[0].removeprefix("<review>\n"))
        # The original request fits; only the required repair reserve makes
        # the unsplit batch too large. No failed HTTP should be necessary.
        if (payload.get("validation_feedback")
                and sum(len(p["text"]) for p in payload["pages"]) > 700):
            raise ReviewContextExceeded("The complete repair request does not fit")

    class Gateway:
        def chat_json(self, system, user, response_model, **kwargs):
            payload = json.loads(user.removeprefix("<review>\n").removesuffix("\n</review>"))
            calls.append(payload)
            if payload["batch_results"] is None:
                assert {p["page_no"] for p in payload["pages"]} in ({1}, {2})
                evidence = [{"source_id": payload["allowed_source_ids"][0]}]
            else:
                evidence = [{"evidence_id": payload["allowed_evidence_ids"][0]}]
            return response_model(verdict="pass", reasoning="按本批完整原文核验响应", evidence=evidence), None

    monkeypatch.setattr("app.llm.request_sizing.assert_request_fits", fits)
    result = review.review_criterion(Gateway(), {"name": "响应条款", "score_type": "match"}, pages)
    assert len(calls) == 3 and result.coverage_complete
    assert {p["page_no"] for p in result.reviewed_pages} == {1, 2}
