"""Oversized synthesis verifies all original groups without losing source links."""
from __future__ import annotations

import json
import threading
import types
from contextlib import nullcontext
from copy import deepcopy

import pytest
from pydantic import BaseModel

from app.engine import auto_review as review
from app.errors import LLMNotConfigured
from app.llm import batch_scheduler
from app.llm.base import LLMError, LLMResult
from app.llm.gateway import Gateway
from app.llm.request_sizing import ReviewContextExceeded, assert_request_fits
from app.services.execution_version import (
    bind_run_execution, current_execution_manifest, execution_config_value,
)


def frozen_manifest(**settings):
    return {"executor_version": 2, "prompt_version": "frozen-test-prompt",
            "scoring_config": {"llm_default_max_tokens": 8192, "llm_context_tokens": 200000,
                               "llm_thinking": "disabled", "llm_disable_thinking": False,
                               "llm_seed": 42, **settings}}


def test_complete_request_budget_includes_rule_schema_and_output(monkeypatch):
    monkeypatch.setenv("BID_EVAL_LLM_CONTEXT_TOKENS", "10000")
    monkeypatch.setenv("BID_EVAL_LLM_DEFAULT_MAX_TOKENS", "8192")
    with pytest.raises(ReviewContextExceeded):
        assert_request_fits("规则" * 1000, "短材料", [])


def test_grouped_synthesis_preserves_all_original_facts_and_evidence(monkeypatch):
    monkeypatch.setenv("BID_EVAL_LLM_DEFAULT_MAX_TOKENS", "512")
    monkeypatch.setenv("BID_EVAL_LLM_CONCURRENCY", "2")
    pages, summaries, decisions = [], [], []
    for index in range(16):
        quote = f"原页{index}的有效证据：" + "完整响应及保障措施。" * 180
        page = {"document_id": 1, "page_no": index + 1, "text": quote, "source": "text"}
        evidence = {"evidence_id": f"e{index}", "document_id": 1, "page_no": index + 1,
                    "kind": "text", "quote": quote}
        pages.append(page)
        summaries.append({"evidence": [evidence], "facts": {"original_value": str(index)}})
        decisions.append(review.AutoReviewDecision(verdict="pass", reasoning="原页采集", evidence=[evidence]))
    original = deepcopy(summaries)
    calls = []
    leaf_threads = []
    verification_facts = []
    caller_thread = threading.get_ident()

    def call(gateway, criterion, material, *, summaries, preflight_only=False,
             collection_only=False, source_ledger=None, **kwargs):
        if len(json.dumps(summaries, ensure_ascii=False)) > 14000:
            raise ReviewContextExceeded("test context")
        if preflight_only:
            return None
        calls.append((collection_only, deepcopy(summaries), deepcopy(source_ledger)))
        assert current_execution_manifest()["executor_version"] == 2
        assert execution_config_value("llm_default_max_tokens") == 8192
        if collection_only:
            assert batch_scheduler._IN_BATCH.get()
            assert threading.get_ident() != caller_thread
            leaf_threads.append(threading.get_ident())
            first = summaries[0]["evidence"][0]
            facts = {"group_values": [item["facts"]["original_value"] for item in summaries],
                     "original_fact_link": {"document_id": first["document_id"], "page_no": first["page_no"],
                                            "quote": first["quote"][:60]}}
            verification_facts.append(deepcopy(facts))
            return review.AutoReviewDecision(verdict="pass", reasoning="本组原始事实均已核验",
                                             evidence=[first], facts=facts)
        assert not batch_scheduler._IN_BATCH.get()
        assert threading.get_ident() == caller_thread
        assert source_ledger is not None and len(source_ledger) == len(pages)
        return review.AutoReviewDecision(verdict="pass", reasoning="所有组完整综合", evidence=[source_ledger["e0"]])

    monkeypatch.setattr(review, "_call", call)
    try:
        with bind_run_execution(frozen_manifest()):
            result = review.synthesis_decision(None, {"name": "核验条款", "score_type": "veto"}, pages,
                data_class="synthetic", previous=None, round_no=0, use_vision=False,
                summaries=summaries, decisions=decisions)
    finally:
        batch_scheduler.reset_batch_schedulers()
    assert result.verdict == "pass" and summaries == original
    assert sum(collecting for collecting, _, _ in calls) >= 2
    final = calls[-1][1]
    retained = [item for group in final for item in group["complete_results"]]
    assert [item["facts"] for item in retained] == [item["facts"] for item in original]
    assert [item["evidence"][0]["evidence_id"] for item in retained] == [f"e{i}" for i in range(16)]
    assert all(set(item["evidence"][0]) == {"evidence_id"} for item in retained)
    assert leaf_threads and len(json.dumps(final, ensure_ascii=False)) <= 14000
    assert sorted((group["verification"]["facts"]["group_values"] for group in final)) == sorted(
        item["group_values"] for item in verification_facts)
    assert all("quote" not in group["verification"]["evidence"][0] for group in final)
    assert all(group["verification"]["facts"]["original_fact_link"]["quote"] for group in final)


def test_irreducible_complete_result_fails_without_silent_truncation(monkeypatch):
    sent = []

    def call(*args, **kwargs):
        sent.append(deepcopy(kwargs["summaries"]))
        raise ReviewContextExceeded("irreducible")

    monkeypatch.setattr(review, "_call", call)
    summary = {"facts": {"full": "必须完整保留" * 1000}, "evidence": []}
    with pytest.raises(ReviewContextExceeded):
        review.synthesis_decision(None, {}, [], data_class="synthetic", previous=None,
            round_no=0, use_vision=False, summaries=[summary], decisions=[])
    assert all(value[0]["facts"] == summary["facts"] for value in sent)


def test_final_grouped_result_must_match_immutable_ledger_and_evict_bad_response(monkeypatch):
    quote = "本项目的响应时间为二小时，且所有服务要求均已完整承诺。"
    page = {"document_id": 1, "page_no": 1, "text": quote}
    evidence = {"evidence_id": "e0", "document_id": 1, "page_no": 1, "kind": "text", "quote": quote}
    summary = {"facts": {"record": "complete"}, "evidence": [evidence]}
    evicted = []

    def call(*args, summaries, collection_only=False, preflight_only=False, source_ledger=None, **kwargs):
        if not collection_only and source_ledger is None:
            raise ReviewContextExceeded("force grouped verification")
        if preflight_only:
            return None
        if collection_only:
            return review.AutoReviewDecision(verdict="pass", reasoning="核验本组", evidence=[evidence])
        result = review.AutoReviewDecision(verdict="pass", reasoning="伪造了新引文", evidence=[{
            **evidence, "quote": "原始台账中并不存在的伪造响应和承诺内容。"}])
        result._invalidate_response = lambda: evicted.append(True)
        return result

    monkeypatch.setattr(review, "_call", call)
    try:
        with pytest.raises(LLMError, match="证据"):
            review.synthesis_decision(None, {}, [page], data_class="synthetic", previous=None,
                round_no=0, use_vision=False, summaries=[summary],
                decisions=[review.AutoReviewDecision(verdict="pass", reasoning="采集", evidence=[evidence])])
    finally:
        batch_scheduler.reset_batch_schedulers()
    assert evicted == [True]


def _large_sourced_results(count, *, single_summary=False, v2=False):
    pages, summaries, decisions, cases = [], [], [], []
    for number in range(count):
        quote = f"第{number}项已签订真实合同，合同金额为{100000 + number}元。" + "项目服务及验收证明原件齐备。" * 70
        evidence = {"evidence_id": f"case-{number}", "document_id": 7, "page_no": number + 1,
                    "kind": "text", "quote": quote}
        case = {"name": f"项目{number}", "amount_yuan": str(100000 + number),
                "contract": True, "acceptance": True, "invoice": None,
                "quote": quote, "evidence": [deepcopy(evidence)]}
        pages.append({"document_id": 7, "page_no": number + 1, "text": quote, "source": "text"})
        cases.append(case)
        summaries.append({"verdict": "pass", "reasoning": f"第{number}页事实及金额已核验",
                          "facts": {"verified_cases": [case]}, "evidence": [evidence]})
        decisions.append(review.AutoReviewDecision(verdict="pass", reasoning="原页采集",
            facts={"verified_cases": [deepcopy(case)]}, evidence=[deepcopy(evidence)]))
    if single_summary:
        combined = {"verdict": "pass", "reasoning": "全部记录按原始事实保留，不以摘要计数",
                    "evidence": [item for summary in summaries for item in summary["evidence"]]}
        if v2:
            combined["collected_facts"] = [
                {"fact_id": f"f{number}", "field": "verified_cases", "value": deepcopy(case),
                 "evidence_ids": [f"case-{number}"]} for number, case in enumerate(cases)]
        else:
            combined["facts"] = {"verified_cases": cases}
        summaries = [combined]
    return pages, summaries, decisions


@pytest.mark.parametrize("executor,single_summary,v2", [(1, False, False), (2, False, False),
                                                       (1, True, False), (2, True, True)])
def test_real_65536_budget_keeps_every_case_value_and_source(monkeypatch, executor, single_summary, v2):
    from app.llm.request_sizing import request_usage

    monkeypatch.setenv("BID_EVAL_LLM_CONCURRENCY", "2")
    pages, summaries, decisions = _large_sourced_results(40, single_summary=single_summary, v2=v2)
    original = deepcopy(summaries)
    final_payloads, collection_calls, usages = [], [], []

    class OfflineGateway:
        def chat_json(self, system, user, response_model, **kwargs):
            assert system == review.SYSTEM_PROMPT
            payload = json.loads(user.removeprefix("<review>\n").removesuffix("\n</review>"))
            usage = request_usage(system, user)
            assert usage["output_tokens"] == 8192
            assert usage["text_token_upper_bound"] + 8192 + 1024 <= 65536
            usages.append(usage)
            if payload["collection_only"]:
                collection_calls.append(payload)
            else:
                assert all("complete_results" in group for group in payload["batch_results"])
                final_payloads.append(payload)
            # This fake validates the complete request path and source binding;
            # it deliberately does not claim a final count or scoring outcome.
            return response_model(verdict="pass", reasoning="只验证payload完整性和来源绑定",
                facts={"new_verification_marker": "additional-value-987654321"} if payload["collection_only"] else {},
                evidence=[{"evidence_id": payload["allowed_evidence_ids"][0]}]), None

    manifest = frozen_manifest(llm_context_tokens=65536)
    try:
        with bind_run_execution(manifest, executor):
            result = review.synthesis_decision(OfflineGateway(), {"name": "合同计数", "score_type": "count"}, pages,
                data_class="synthetic", previous={"error": "上一轮请求超限，保留合法输出预算"},
                round_no=2, use_vision=False, summaries=summaries, decisions=decisions)
    finally:
        batch_scheduler.reset_batch_schedulers()
    assert result.verdict == "pass" and summaries == original
    assert len(collection_calls) >= 2 and len(final_payloads) == 1
    final = final_payloads[0]
    retained = [item for group in final["batch_results"] for item in group["complete_results"]]
    if v2:
        kept = [fact["value"] for item in retained for fact in item.get("collected_facts", [])]
    else:
        kept = [case for item in retained for case in item.get("facts", {}).get("verified_cases", [])]
    assert [(item["name"], item["amount_yuan"], item["contract"], item["acceptance"], item["invoice"])
            for item in kept] == [(f"项目{number}", str(100000 + number), True, True, None) for number in range(40)]
    assert [item["evidence_id"] for item in kept] == final["allowed_evidence_ids"]
    assert len(set(final["allowed_evidence_ids"])) == 40
    # Request-local aliases restore the original checked source in the result.
    assert result.evidence[0]["evidence_id"] == "case-0"
    assert result.evidence[0]["document_id"] == 7 and result.evidence[0]["page_no"] == 1
    assert result.evidence[0]["quote"] == original[0]["evidence"][0]["quote"]
    assert all(group["verification"]["facts"]["new_verification_marker"] == "additional-value-987654321"
               for group in final["batch_results"])
    assert len(json.dumps(original, ensure_ascii=False).encode()) > 65536
    assert usages[-1]["text_token_upper_bound"] < 65536 - 8192 - 1024


def test_fact_factoring_is_reversible_and_preserves_conflicts_and_zero():
    case = {"name": "完整案例名" * 30, "amount_yuan": "123456.78", "contract": True,
            "evidence": [{"evidence_id": "original-case"}]}
    originals = [{"facts": {"verified_cases": [case], "negative": 0, "has_invoice": False}}]
    changed = {**deepcopy(case), "amount_yuan": "987654.32"}
    verification = {"facts": {"verified_cases": [deepcopy(case), changed], "negative": 0,
                               "has_invoice": False, "new_value": "preserve-me"}}
    compact = review._factor_verification_facts(verification, originals)

    def restore(value):
        if isinstance(value, dict) and set(value) == {"complete_result_ref"}:
            resolved = originals
            for key in value["complete_result_ref"]:
                resolved = resolved[key]
            return deepcopy(resolved)
        if isinstance(value, dict):
            return {key: restore(item) for key, item in value.items()}
        if isinstance(value, list):
            return [restore(item) for item in value]
        return value

    assert "complete_result_ref" in compact["facts"]["verified_cases"][0]
    assert compact["facts"]["verified_cases"][1]["amount_yuan"] == "987654.32"
    assert restore(compact) == verification
    defaults = review._omit_synthesis_defaults({"score": 0, "facts": {"zero": 0, "false": False},
                                               "confirmed_absent": False, "missing_evidence": []})
    assert defaults["score"] == 0 and defaults["facts"] == {"zero": 0, "false": False}


def test_compact_links_preserve_short_quotes_and_do_not_rebind_wrong_owner():
    quote = "这是原件中连续的十二个汉字及其后续的真实合同内容。"
    ledger = {"e0": {"evidence_id": "e0", "document_id": 1, "page_no": 2,
                     "bidder_id": 3, "kind": "text", "quote": quote}}
    reference = {"document_id": 1, "page_no": 2, "quote": quote[:20], "amount_yuan": "12.34"}
    compact = review._compact_synthesis_links(reference, ledger)
    assert compact == {"evidence_id": "e0", "quote": quote[:20], "amount_yuan": "12.34"}
    wrong = {**reference, "bidder_id": 4}
    assert review._compact_synthesis_links(wrong, ledger) == wrong


def test_semantic_source_retry_repairs_only_current_input_and_evicts_bad_cache(monkeypatch):
    monkeypatch.setenv("BID_EVAL_LLM_DISK_CACHE", "0")
    quote = "本页为本投标人出具的真实服务承诺，响应时间不超过二小时。"
    pages = [{"document_id": 1, "page_no": 1, "text": quote, "source": "text"}]
    provider = types.SimpleNamespace(name="offline", model="offline", host="localhost")
    gateway = Gateway(provider)
    calls, keys = [], []

    def chat(system, user, **kwargs):
        payload = json.loads(user.removeprefix("<review>\n").removesuffix("\n</review>"))
        calls.append(payload)
        key = gateway._key_for(system, user, kwargs["prompt_version"], kwargs["temperature"], kwargs["max_tokens"], kwargs.get("images"))
        keys.append(key)
        source_id = payload["allowed_source_ids"][0]
        reference = {"source_id": source_id, "document_id": 1, "page_no": 99 if len(calls) == 1 else 1}
        result = LLMResult(text=json.dumps({"verdict": "pass", "reasoning": "真实承诺", "evidence": [reference]}),
                           provider="offline", model="offline")
        gateway._store_memory(key, result)
        return result

    monkeypatch.setattr(gateway, "chat", chat)
    manifest = frozen_manifest(llm_context_tokens=65536)
    with bind_run_execution(manifest, 1):
        result = review._call(gateway, {"name": "服务承诺", "score_type": "match"}, pages,
                              data_class="synthetic", previous={"error": "保留既有补评反馈"},
                              round_no=2, use_vision=False)
    assert len(calls) == 2
    assert calls[0].get("validation_feedback") is None
    assert calls[1]["validation_feedback"]["attempt"] == 1
    assert "文件或页码不一致" in calls[1]["validation_feedback"]["error"]
    for field in ("rule", "pages", "prior", "round", "allowed_source_ids", "response_schema"):
        assert calls[0][field] == calls[1][field]
    assert result.evidence[0]["page_no"] == 1 and result.evidence[0]["quote"] == quote
    assert [item["state"] for item in result.model_dump()["validation_history"]] == ["failed", "completed"]
    assert keys[0] not in gateway._cache and keys[1] in gateway._cache
    result._invalidate_response()
    assert keys[1] not in gateway._cache


def test_semantic_source_retry_stops_after_two_repairs_with_no_verdict():
    pages = [{"document_id": 1, "page_no": 1, "text": "本页原件载明真实的投标响应和服务保障承诺。", "source": "text"}]
    calls, evicted = [], []

    class IncorrectGateway:
        def chat_json(self, system, user, response_model, **kwargs):
            payload = json.loads(user.removeprefix("<review>\n").removesuffix("\n</review>"))
            calls.append(payload)
            return response_model(verdict="pass", reasoning="错页不会被接受", evidence=[{
                "source_id": payload["allowed_source_ids"][0], "document_id": 1, "page_no": 2}]), None

        def _key_for(self, system, user, *args):
            return user

        def _evict_failed(self, key):
            evicted.append(key)

    with bind_run_execution(frozen_manifest(llm_context_tokens=65536), 1):
        with pytest.raises(LLMError, match="定向补抽 2 次") as failed:
            review._call(IncorrectGateway(), {"name": "服务响应", "score_type": "match"}, pages,
                data_class="synthetic", previous=None, round_no=0, use_vision=False)
    assert len(calls) == len(evicted) == 3
    assert [item.get("validation_feedback", {}).get("attempt", 0) for item in calls] == [0, 1, 2]
    assert len(failed.value.validation_history) == 3
    assert all(item["state"] == "failed" for item in failed.value.validation_history)
    assert failed.value.review_attempts_exhausted is True
    assert len(set(evicted)) == 3


@pytest.mark.parametrize("field,nested", [("facts", {"complete_result_ref": [0, "facts"]}),
                                          ("facts", {"verified_cases": [{"nested": {"complete_result_ref": [0, "facts"]}}]}),
                                          ("sub_scores", [{"name": "子项", "evidence": [{"complete_result_ref": [0, "facts"]}]}]),
                                          ("facts", {"nested": {"original_reasoning_ref": 1}}),
                                          ("sub_scores", [{"name": "子项", "value": {"group_result_defaults_ref": 0}}])])
def test_reserved_synthesis_references_require_sourced_expansion(field, nested):
    quote = "真实案例合同载明金额为100元，并保留合同及验收证明材料。"
    pages = [{"document_id": 1, "page_no": 1, "text": quote, "source": "text"}]
    calls = []

    class Gateway:
        def chat_json(self, system, user, response_model, **kwargs):
            payload = json.loads(user.removeprefix("<review>\n").removesuffix("\n</review>"))
            calls.append(payload)
            reference = {"source_id": payload["allowed_source_ids"][0]}
            values = {field: deepcopy(nested)} if len(calls) == 1 else {
                "facts": {"verified_cases": [{"name": "真实案例", "amount_yuan": "100", "contract": True,
                                               "evidence": [reference]}]}}
            return response_model(verdict="pass", reasoning="保留真实事实及原页证据", evidence=[reference], **values), None

    with bind_run_execution(frozen_manifest(llm_context_tokens=65536), 1):
        result = review._call(Gateway(), {"name": "合同计数", "score_type": "count"}, pages,
            data_class="synthetic", previous=None, round_no=0, use_vision=False)
    assert len(calls) == 2 and "complete_result_ref" in calls[1]["validation_feedback"]["error"]
    assert not review._contains_synthesis_reference(result.facts)
    assert not review._contains_synthesis_reference(result.sub_scores)
    assert result.facts["verified_cases"][0]["amount_yuan"] == "100"
    assert result.facts["verified_cases"][0]["evidence"][0]["quote"] == quote


def test_final_reasoning_cannot_return_host_audit_reference():
    page = {"document_id": 1, "page_no": 1, "text": "原页包含真实且完整的服务承诺及证明材料。"}
    calls = []

    class AuditReferenceGateway:
        def chat_json(self, system, user, response_model, **kwargs):
            payload = json.loads(user.removeprefix("<review>\n").removesuffix("\n</review>"))
            calls.append(payload)
            return response_model(verdict="pass", reasoning='{"original_reasoning_ref":0}' if len(calls) == 1 else "原页服务承诺完整，符合细则要求",
                evidence=[{"source_id": payload["allowed_source_ids"][0]}]), None

    with bind_run_execution(frozen_manifest(llm_context_tokens=65536), 1):
        decision = review._call(AuditReferenceGateway(), {"name": "承诺", "score_type": "match"}, [page],
            data_class="synthetic", previous=None, round_no=0, use_vision=False)
    assert len(calls) == 2 and "original_reasoning_ref" not in decision.reasoning


def test_verified_synthesis_audit_factoring_restores_every_field_and_rejects_tampering():
    from app.services.review_collections import stable_hash

    originals = [{"reasoning": "第一批原始推理" * 50, "verdict": "fail", "score": 0,
                  "facts": {"zero": 0, "missing": False, "conflicting_amount": "199.99"},
                  "missing_evidence": ["签章位置缺少原页证明"], "evidence": [{"evidence_id": "a"}]},
                 {"reasoning": "第二批原始推理" * 50, "verdict": "pass", "facts": {},
                  "evidence": [{"evidence_id": "b"}]}]
    ledger = {"a": {"evidence_id": "a"}, "b": {"evidence_id": "b"}}
    groups = [{"group_id": index, "complete_results": [deepcopy(original)],
               "verification": {"reasoning": f"第{index}组全部原文核验，保留原始冲突", "facts": {"new": "untouched"},
                                "evidence": deepcopy(original["evidence"])},
               "evidence": deepcopy(original["evidence"]), "result_defaults": deepcopy(review._SYNTHESIS_DEFAULTS),
               "ledger_policy": "同一不可变来源策略"} for index, original in enumerate(originals)]
    original_groups = deepcopy(groups)
    compact = review._compact_verified_synthesis_groups(groups, originals, ledger)
    assert groups == original_groups
    assert review._restore_verified_synthesis_groups(compact, originals, ledger) == groups
    assert compact[0]["original_reasoning_audit"]["original_results_sha256"] == stable_hash(originals)
    assert compact[0]["complete_results"][0]["facts"] == originals[0]["facts"]
    assert compact[0]["complete_results"][0]["verdict"] == "fail"
    assert compact[0]["complete_results"][0]["score"] == 0
    assert compact[0]["complete_results"][0]["missing_evidence"] == originals[0]["missing_evidence"]
    assert compact[1]["verification"]["reasoning"] == groups[1]["verification"]["reasoning"]
    assert compact[0]["complete_results"][0]["evidence"] == [0]
    for corrupt in ("hash", "reasoning_index", "evidence_index"):
        changed = deepcopy(compact)
        if corrupt == "hash":changed[0]["original_reasoning_audit"]["original_results_sha256"] = "wrong"
        elif corrupt == "reasoning_index":changed[0]["complete_results"][0]["reasoning"]["original_reasoning_ref"] = -1
        else:changed[0]["complete_results"][0]["evidence"] = [999]
        with pytest.raises(LLMError):
            review._restore_verified_synthesis_groups(changed, originals, ledger)


def test_final_synthesis_reserves_combined_semantic_and_json_repair_before_dispatch(monkeypatch):
    from app.llm.request_sizing import request_usage

    page = {"document_id": 1, "page_no": 1, "text": "真实原页已经完整提供项目承诺和响应内容。"}
    usages = []

    def capture(system, user, images=None):
        usages.append(request_usage(system, user, images))
        return usages[-1]

    parameters = dict(data_class="synthetic", previous=None, round_no=0, use_vision=False, reserve_repairs=True)
    with bind_run_execution(frozen_manifest(llm_context_tokens=65536), 1), monkeypatch.context() as scoped:
        scoped.setattr("app.llm.request_sizing.assert_request_fits", capture)
        review._call(None, {"name": "承诺", "score_type": "match"}, [page], preflight_only=True, **parameters)
    assert len(usages) == 2 and usages[1]["text_token_upper_bound"] > usages[0]["text_token_upper_bound"] + 2000

    class NeverCalled:
        def chat_json(self, *args, **kwargs):raise AssertionError("must preflight the reserved repairs before dispatch")

    limit = usages[0]["text_token_upper_bound"] + 8192 + 1024 + 1
    with bind_run_execution(frozen_manifest(llm_context_tokens=limit), 1):
        with pytest.raises(ReviewContextExceeded):
            review._call(NeverCalled(), {"name": "承诺", "score_type": "match"}, [page], **parameters)


@pytest.mark.parametrize("error", [LLMError("timeout exhausted", transient=True),
                                  ReviewContextExceeded("context exceeded"),
                                  LLMNotConfigured("model is not configured"),
                                  LLMError("JSON repair exhausted")])
def test_semantic_retry_does_not_repeat_exhausted_gateway_failure(error):
    calls = []

    class FailedGateway:
        def chat_json(self, *args, **kwargs):
            calls.append(True)
            raise error

    with bind_run_execution(frozen_manifest(llm_context_tokens=65536), 1):
        with pytest.raises(type(error), match=str(error)):
            review._call(FailedGateway(), {"name": "承诺", "score_type": "match"},
                [{"document_id": 1, "page_no": 1, "text": "真实原件中的完整投标服务承诺。"}],
                data_class="synthetic", previous=None, round_no=0, use_vision=False)
    assert calls == [True]


def test_semantic_retry_does_not_repeat_ready_sibling_or_retrieve_full_document(monkeypatch):
    pages = [{"document_id": 1, "page_no": number,
              "text": f"第{number}页为真实投标原件中的服务承诺及完整响应内容。", "source": "text"}
             for number in (1, 2)]
    cached = {0: review.AutoReviewDecision(verdict="pass", reasoning="原先已完成的第一批", evidence=[{
        "document_id": 1, "page_no": 1, "kind": "text", "quote": pages[0]["text"]}]).model_dump(mode="json")}
    dispatched, checkpoint_performs = [], []
    monkeypatch.setattr(review, "_batches", lambda selected, images=False: [[page] for page in selected])

    class Checkpoint:
        def __init__(self, *args, **kwargs):
            pass

        def execute(self, index, perform, validate):
            if index not in cached:
                checkpoint_performs.append(index)
                raw = perform()
                validate(raw)
                cached[index] = deepcopy(raw)
            validate(cached[index])
            return deepcopy(cached[index])

        def ready(self):
            assert len(cached) == 2

    class Gateway:
        def chat_json(self, system, user, response_model, **kwargs):
            payload = json.loads(user.removeprefix("<review>\n").removesuffix("\n</review>"))
            dispatched.append(payload)
            if payload["collection_only"]:
                assert {item["page_no"] for item in payload["pages"]} == {2}
                reference = {"source_id": payload["allowed_source_ids"][0],
                             "page_no": 1 if not payload.get("validation_feedback") else 2}
            else:
                reference = {"evidence_id": payload["allowed_evidence_ids"][0]}
            return response_model(verdict="pass", reasoning="当前批原页响应已经核验", evidence=[reference]), None

    monkeypatch.setattr("app.services.review_collections.CollectionCheckpoint", Checkpoint)
    try:
        with bind_run_execution(frozen_manifest(llm_context_tokens=65536), 1):
            for _ in range(2):
                result = review.review_criterion(Gateway(), {"name": "承诺", "score_type": "match"}, pages)
                assert result.coverage_complete and result.verdict == "pass"
    finally:
        batch_scheduler.reset_batch_schedulers()
    assert checkpoint_performs == [1]
    assert sum(payload["collection_only"] for payload in dispatched) == 2
    assert len(dispatched) == 4  # Initial bad/corrected batch, then one synthesis per invocation.
    assert [item["state"] for item in cached[1]["validation_history"]] == ["failed", "completed"]
    assert "validation_history" not in cached[0]


class Answer(BaseModel):
    answer: str


def _offline_gateway(monkeypatch, responses):
    provider = types.SimpleNamespace(name="offline", model="offline", host="localhost")
    gateway = Gateway(provider)
    calls = []

    def chat(system, user, **kwargs):
        calls.append({"system": system, "user": user, **kwargs})
        return LLMResult(text=next(responses), provider="offline", model="offline")

    monkeypatch.setattr(gateway, "chat", chat)
    monkeypatch.setattr(gateway, "_evict_failed", lambda key: None)
    monkeypatch.setenv("BID_EVAL_LLM_DISK_CACHE", "0")
    return gateway, calls


def test_gateway_checks_actual_json_repair_budget_before_dispatch(monkeypatch):
    from app.llm.request_sizing import request_usage

    gateway, calls = _offline_gateway(monkeypatch, iter(["not JSON", '{"answer":"unused"}']))
    system, user = "评审", "完整的原始输入不能被截短。"
    manifest = frozen_manifest()
    with bind_run_execution(manifest):
        initial = request_usage(system, user)
    manifest["scoring_config"]["llm_context_tokens"] = initial["text_token_upper_bound"] + 8192 + 1024 + 1
    with bind_run_execution(manifest):
        with pytest.raises(ReviewContextExceeded):
            gateway.chat_json(system, user, Answer, max_tokens=8192)
    assert len(calls) == 1 and calls[0]["user"] == user
    assert calls[0]["max_tokens"] == 8192


def test_gateway_keeps_frozen_output_allowance_for_successful_json_repair(monkeypatch):
    gateway, calls = _offline_gateway(monkeypatch, iter(["not JSON", '{"answer":"ok"}']))
    monkeypatch.setenv("BID_EVAL_LLM_DEFAULT_MAX_TOKENS", "512")
    with bind_run_execution(frozen_manifest()):
        result, _ = gateway.chat_json("评审", "完整原始输入。", Answer, max_tokens=8192)
    assert result.answer == "ok"
    assert len(calls) == 2 and "JSON契约修复" in calls[1]["user"]
    assert all(call["max_tokens"] == 8192 for call in calls)


def test_gateway_reserves_explicit_output_above_frozen_default(monkeypatch):
    gateway, calls = _offline_gateway(monkeypatch, iter(['{"answer":"unused"}']))
    with bind_run_execution(frozen_manifest(llm_context_tokens=12000)):
        with pytest.raises(ReviewContextExceeded):
            gateway.chat_json("评审", "完整原始输入。", Answer, max_tokens=16000)
    assert not calls


def test_leaf_provider_inherits_frozen_output_and_refuses_context_clamp(monkeypatch):
    from app.llm import openai_compat

    calls = []
    monkeypatch.setenv("BID_EVAL_LLM_DEFAULT_MAX_TOKENS", "512")
    monkeypatch.setenv("BID_EVAL_LLM_THINKING", "enabled")
    monkeypatch.setenv("BID_EVAL_LLM_TRANSIENT_RETRIES", "0")
    monkeypatch.setattr(openai_compat, "http_attempt", lambda **kwargs: nullcontext(types.SimpleNamespace()))

    def create(**kwargs):
        assert batch_scheduler._IN_BATCH.get()
        assert current_execution_manifest()["executor_version"] == 2
        calls.append(kwargs)
        raise RuntimeError("maximum context length is 10000; request has 6000 input tokens")

    provider = openai_compat.OpenAICompatProvider.__new__(openai_compat.OpenAICompatProvider)
    provider.name, provider.model = "offline", "frozen-offline"
    provider._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    try:
        with bind_run_execution(frozen_manifest()):
            with pytest.raises(ReviewContextExceeded, match="冻结输出预算"):
                batch_scheduler.run_batches([1], lambda _: provider.complete("评审", "原始输入"))
    finally:
        batch_scheduler.reset_batch_schedulers()
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 8192
    assert calls[0]["extra_body"]["thinking"] == {"type": "disabled"}
