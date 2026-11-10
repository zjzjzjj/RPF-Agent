"""Shared collection preserves ownership, raw conflicts and independent verdicts."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from app.engine import auto_review as review
from app.engine import shared_review as shared
from app.llm.base import LLMError
from app.llm.batch_scheduler import reset_batch_schedulers
from app.services.review_collections import source_unit, stable_hash


class MemoryCheckpoint:
    next_id = 1

    def __init__(self, rule, batches, **kwargs):
        self.collection_id = MemoryCheckpoint.next_id
        MemoryCheckpoint.next_id += 1
        self.input_signature = stable_hash({"batches": batches, **kwargs})
        self.ready_calls = []

    def execute(self, index, perform, validate):
        result = perform()
        validate(result)
        return result

    def ready(self, *, resolution_collection_ids=None, resolution_unit_map=None):
        self.ready_calls.append(list(resolution_collection_ids or []))


@pytest.fixture(autouse=True)
def collection_env(monkeypatch):
    monkeypatch.setattr(shared, "CollectionCheckpoint", MemoryCheckpoint)
    monkeypatch.setattr(shared, "assert_request_fits", lambda *args, **kwargs: None)
    monkeypatch.setattr(shared, "execution_config_value", lambda name: 2)
    monkeypatch.setenv("BID_EVAL_LLM_CONCURRENCY", "4")
    yield
    reset_batch_schedulers()


def page(bidder=1, number=1, text=None):
    return {"document_id": bidder, "bidder_id": bidder, "page_no": number,
            "text": text or f"投标人{bidder}的第{number}页联系人为张三，联系电话为12345678901。",
            "sha256": f"document-{bidder}", "source": "pdf-text"}


def rule(bidders=1, count=1):
    return {"id": 7, "description": "核验所有投标人的原始事实", "type": "qualification",
            "_frozen_bidders": [{"id": bidder} for bidder in range(1, bidders + 1)],
            "_comparison_documents": [{"document_id": bidder, "bidder_id": bidder, "page_count": count}
                                      for bidder in range(1, bidders + 1)]}


def raw_fact(pages, *, value="张三", qualifiers=None, predicate="联系人"):
    return {"schema_version": 2,
            "evidence": [{"evidence_id": f"e{index}", "source_unit_id": source_unit(p)["unit_id"],
                          "kind": "text", "quote": p["text"]} for index, p in enumerate(pages)],
            "facts": [{"fact_id": f"f{index}", "subject_bidder_ids": [p["bidder_id"]],
                       "predicate": predicate, "value": value, "qualifiers": qualifiers or {},
                       "evidence_ids": [f"e{index}"]} for index, p in enumerate(pages)],
            "unresolved": []}


def test_wrong_owner_and_wrong_quote_cannot_enter_shared_facts():
    pages = [page()]
    wrong_owner = raw_fact(pages)
    wrong_owner["facts"][0]["subject_bidder_ids"] = [2]
    with pytest.raises(LLMError, match="主体"):
        shared._check_result(wrong_owner, pages)
    wrong_quote = raw_fact(pages)
    wrong_quote["evidence"][0]["quote"] = "原始材料根本没有出现的伪造联系人和电话号码。"
    with pytest.raises(LLMError, match="引文"):
        shared._check_result(wrong_quote, pages)


def test_quote_is_bound_to_selected_span_not_just_same_page():
    p = page(text="甲" * 600 + "第二段的原始联系人张三及联系电话12345678901。")
    material, _ = review._source_material([p])
    raw = raw_fact([p])
    raw["evidence"][0].update(source_id=material[0]["source_id"], quote=material[-1]["text"])
    with pytest.raises(LLMError, match="source_id"):
        shared._check_result(raw, [p])


def test_source_id_only_binds_exact_original_text_and_literal_context():
    p = page(text="2025年联系人：张三，联系电话12345678901，以下为完整原始投标材料。")
    material, _ = review._source_material([p])
    raw = raw_fact([p], qualifiers={"year": "2025年", "role": "联系人"})
    raw["evidence"][0].update(source_id=material[0]["source_id"], quote=None)
    checked = shared._check_result(raw, [p])
    assert checked["evidence"][0]["quote"] == p["text"]
    assert checked["evidence"][0]["bidder_id"] == p["bidder_id"]
    assert checked["facts"][0]["qualifiers"] == {"year": "2025年", "role": "联系人"}
    raw["facts"][0]["qualifiers"]["role"] = "项目联系人"
    with pytest.raises(LLMError, match=r"facts\[0\].qualifiers.*限定条件"):
        shared._check_result(raw, [p])


def test_targeted_semantic_repair_preserves_request_and_records_only_checked_result():
    pages = [page(text="2025年联系人：张三，联系电话12345678901，以下为完整原始投标材料。")]
    repair = json.dumps([{"target": "保留2025年的联系人原始值", "required_action": "reextract"}], ensure_ascii=False)
    users = []

    class Gateway:
        def chat_json(self, system, user, schema, **kwargs):
            users.append(json.loads(user.split("<collection>\n", 1)[1].rsplit("\n</collection>", 1)[0]))
            raw = raw_fact(pages, qualifiers={"role": "项目联系人"} if len(users) == 1 else {"role": "联系人"})
            raw["evidence"][0].update(source_id=users[-1]["source_units"][0]["spans"][0]["source_id"], quote=None)
            return schema.model_validate(raw), None

    results, _, _ = shared.SharedReviewCollector()._pass(Gateway(), rule(), pages, "public", repair=repair)
    assert len(users) == 2
    feedback = json.loads(users[1]["repair"])
    assert feedback["targeted_repair"] == repair
    assert "facts[0].qualifiers" in feedback["error"]
    checked = results[0]
    assert checked["facts"][0]["qualifiers"] == {"role": "联系人"}
    assert [item["state"] for item in checked["validation_history"]] == ["failed", "completed"]
    assert len(checked["validation_history"][0]["response_sha256"]) == 64
    shared._validate_checked(checked, pages)


def test_exhausted_shared_batch_does_not_restart_whole_cell_or_request_vision(monkeypatch):
    from app.services import automatic_scoring_service as scoring

    pages = [page()]
    calls = []

    class Gateway:
        def chat_json(self, system, user, schema, **kwargs):
            calls.append(user)
            return schema.model_validate(raw_fact(pages, qualifiers={"year": "2049"})), None

    collector = shared.SharedReviewCollector()

    def reviewer(gateway, criterion, selected, **kwargs):
        return collector._pass(gateway, criterion, selected, kwargs["data_class"])

    monkeypatch.setattr(scoring, "_supplement_pages", lambda *a, **kw: pytest.fail("已耗尽的采集批次不得重读全文或启动视觉"))
    with pytest.raises(LLMError, match="限定条件") as failure:
        scoring._assess_cell({**rule(), "score_type": "veto"}, pages, "public", Gateway, reviewer=reviewer)
    assert failure.value.review_attempts_exhausted is True
    assert len(calls) == 3
    assert len(failure.value.validation_history) == 3
    assert failure.value.attempt_history[-1]["round"] == 0


@pytest.mark.parametrize("tamper", ["schema", "empty_subject", "duplicate_evidence", "duplicate_fact",
                                    "bad_owner", "bad_id", "bad_action", "string_subject", "extra"])
def test_persisted_complete_flag_cannot_bypass_checked_schema(tamper):
    pages = [page()]
    checked = shared._check_result(raw_fact(pages), pages)
    if tamper == "schema":
        checked["schema_version"] = 1
    elif tamper == "empty_subject":
        checked["facts"][0]["subject_bidder_ids"] = []
    elif tamper == "duplicate_evidence":
        checked["evidence"].append(deepcopy(checked["evidence"][0]))
    elif tamper == "duplicate_fact":
        checked["facts"].append(deepcopy(checked["facts"][0]))
    elif tamper == "bad_owner":
        checked["evidence"][0]["bidder_id"] = 2
    elif tamper == "bad_id":
        checked["facts"][0]["fact_id"] = "replace-with-new-value"
    elif tamper == "bad_action":
        checked["unresolved"] = [{"source_unit_ids": [source_unit(pages[0])["unit_id"]],
            "target": "联系人", "required_action": "human", "reason_code": "test"}]
    elif tamper == "string_subject":
        checked["facts"][0]["subject_bidder_ids"] = ["1"]
    else:
        checked["final_score"] = 100
    with pytest.raises(LLMError):
        shared._validate_checked(checked, pages)


def test_qualifier_cannot_invent_context_to_hide_conflict():
    pages = [page()]
    with pytest.raises(LLMError, match="限定条件"):
        shared._check_result(raw_fact(pages, qualifiers={"year": "2049"}), pages)


def test_persisted_visual_hash_cannot_be_replaced_after_completion():
    p = {**page(), "image_url": "data:image/png;base64,aGVsbG8="}
    raw = raw_fact([p])
    raw["evidence"][0] = {"evidence_id": "visual", "source_unit_id": source_unit(p)["unit_id"],
        "kind": "visual", "observation": "扫描页的联系人为张三，联系电话为12345678901。"}
    raw["facts"][0]["evidence_ids"] = ["visual"]
    checked = shared._check_result(raw, [p])
    checked["evidence"][0]["image_sha256"] = "changed"
    with pytest.raises(LLMError, match="页图哈希"):
        shared._validate_checked(checked, [p])


def test_seven_bidders_share_44_collection_calls_and_synthesize_independently(monkeypatch):
    pages = [page(bidder, number) for number in range(1, 45) for bidder in range(1, 8)]
    monkeypatch.setattr(review, "_batches", lambda selected, **kw: [selected[i:i + 7] for i in range(0, len(selected), 7)])
    calls = {"collect": 0, "synthesis": []}
    lock = threading.Lock()

    class Gateway:
        def chat_json(self, system, user, schema, **kwargs):
            payload = json.loads(user.split("<collection>\n", 1)[1].rsplit("\n</collection>", 1)[0])
            units = payload["source_units"]
            selected = [page(unit["bidder_id"], unit["page_no"]) for unit in units]
            with lock:
                calls["collect"] += 1
            return schema.model_validate(raw_fact(selected)), None

    def synthesize(gateway, criterion, selected, *, summaries, decisions, **kwargs):
        bidder = criterion["_frozen_bidder"]["id"]
        assert len(summaries[0]["collected_facts"]) == 308
        assert len(decisions[0].evidence) == 308
        with lock:
            calls["synthesis"].append(bidder)
        evidence = next(item for item in summaries[0]["evidence"] if item["bidder_id"] == bidder)
        return review.AutoReviewDecision(verdict="pass", reasoning=f"独立核验投标人{bidder}", evidence=[evidence])

    monkeypatch.setattr(review, "synthesis_decision", synthesize, raising=False)
    monkeypatch.setattr(review, "finish_decision", lambda decision, *args, **kwargs: decision)
    collector = shared.SharedReviewCollector()
    barrier = threading.Barrier(7)
    gateway = Gateway()

    def evaluate(bidder):
        barrier.wait(timeout=5)
        return collector.review_criterion(gateway, {**rule(7, 44), "_frozen_bidder": {"id": bidder}},
                                          pages, data_class="public")

    with ThreadPoolExecutor(max_workers=7) as executor:
        decisions = list(executor.map(evaluate, range(1, 8)))
    assert calls["collect"] == 44
    assert sorted(calls["synthesis"]) == list(range(1, 8))
    assert len({decision.reasoning for decision in decisions}) == 7
    assert all(decision.facts["_collection_provenance"] for decision in decisions)


def _conflict_collector(monkeypatch, *, repair_contexts):
    pages = [page(number=1, text="2024年联系人为张三，联系电话12345678901，原始材料如下。"),
             page(number=2, text="2025年联系人为李四，联系电话12345678902，原始材料如下。"),
             page(number=3, text="本页是与联系人无关的其他材料，已经完成原始事实采集。")]
    checkpoints, passed_pages, repairs = [], [], []
    collector = shared.SharedReviewCollector()

    def collect_pass(gateway, criterion, selected, data_class, *, repair=None):
        passed_pages.append([p["page_no"] for p in selected])
        repairs.append(json.loads(repair) if repair else None)
        checkpoint = MemoryCheckpoint(criterion, [selected], repair=repair)
        checkpoints.append(checkpoint)
        results = []
        for p in selected:
            if p["page_no"] == 3:
                results.append({"schema_version": 2, "evidence": [], "facts": [], "unresolved": []})
                continue
            value, year = ("张三", "2024") if p["page_no"] == 1 else ("李四", "2025")
            qualifiers = {"year": year} if repair and repair_contexts else {}
            results.append(shared._check_result(raw_fact([p], value=value, qualifiers=qualifiers), [p]))
        return results, checkpoint, [selected]

    monkeypatch.setattr(collector, "_pass", collect_pass)
    return collector, pages, passed_pages, checkpoints, repairs


def test_conflicts_target_only_related_pages_and_preserve_all_values(monkeypatch):
    collector, pages, passed, checkpoints, repairs = _conflict_collector(monkeypatch, repair_contexts=False)
    result = collector._collect(object(), rule(count=3), pages, "public")
    assert passed == [[1, 2, 3], [1, 2], [1, 2]]
    assert {fact["value"] for fact in result.facts} == {"张三", "李四"}
    assert len(result.conflicts) == 1
    assert all(repair[0]["required_action"] == "reextract" for repair in repairs[1:])
    assert checkpoints[-1].ready_calls == [[]]
    assert checkpoints[0].ready_calls == [[checkpoints[2].collection_id, checkpoints[1].collection_id]]
    assert len(result.provenance) == 3


def test_evidence_backed_distinct_contexts_resolve_conflict_without_overwriting_history(monkeypatch):
    collector, pages, passed, checkpoints, repairs = _conflict_collector(monkeypatch, repair_contexts=True)
    result = collector._collect(object(), rule(count=3), pages, "public")
    assert passed == [[1, 2, 3], [1, 2]]
    assert not result.conflicts
    assert len(result.facts) == 4
    assert len(result.refinements) == 2
    assert {tuple(f["qualifiers"].items()) for f in result.facts} == {(), (("year", "2024"),), (("year", "2025"),)}


def test_unresolved_conflicts_are_visible_to_synthesis_and_cannot_produce_fake_score(monkeypatch):
    collector, pages, passed, checkpoints, repairs = _conflict_collector(monkeypatch, repair_contexts=False)
    seen = []

    def synthesize(*args, summaries, **kwargs):
        seen.extend(summaries[0]["collection_conflicts"])
        return review.AutoReviewDecision(verdict="pass", reasoning="模型擅自忽略了矛盾", score=100)

    monkeypatch.setattr(review, "synthesis_decision", synthesize, raising=False)
    with pytest.raises(LLMError, match="原始值冲突"):
        collector.review_criterion(object(), {**rule(count=3), "_frozen_bidder": {"id": 1}}, pages, data_class="public")
    assert len(seen) == 1


def test_same_canonical_id_with_changed_payload_never_overwrites():
    target = {"e1": {"evidence_id": "e1", "quote": "original"}}
    with pytest.raises(LLMError, match="拒绝覆盖"):
        shared._merge_checked(target, [{"evidence_id": "e1", "quote": "replacement"}], "evidence_id")
    assert target["e1"]["quote"] == "original"
