"""Collection checkpoints preserve completed work without accepting stale results."""
from __future__ import annotations

import json
from copy import deepcopy

import pytest
from sqlalchemy import select

from app.db.models import (
    Bidder, Criterion, EvaluationRun, Project, ReviewCollectionBatch,
    ReviewCollectionSet, RulePackage,
)
from app.db.session import session_scope
from app.llm.base import LLMError
from app.services.review_collections import (
    CollectionCheckpoint, recover_interrupted_batches, review_runtime,
    source_unit, verify_text_coverage,
)


@pytest.fixture
def checkpoint_env(app_env):
    with session_scope() as session:
        session.add(Project(id="checkpoint", name="Checkpoint"))
        session.flush()
        package = RulePackage(project_id="checkpoint", version=1, status="frozen")
        session.add(package)
        session.flush()
        bidder = Bidder(project_id="checkpoint", name="A")
        criterion = Criterion(package_id=package.id, code="Q1", name="Evidence",
                              category="qualification", score_type="match")
        session.add_all([bidder, criterion])
        session.flush()
        run = EvaluationRun(project_id="checkpoint", package_id=package.id, package_version=1)
        session.add(run)
        session.flush()
        values = {"run_id": run.id, "bidder_id": bidder.id, "criterion_id": criterion.id}
    values["manifest"] = {"executor_version": 2, "model": "offline", "prompt_version": "frozen"}
    return values


def _page(number=1, text="Original evidence", **extra):
    return {"document_id": 101, "page_no": number, "bidder_id": 1,
            "sha256": "original-file", "text": text, "source": "text", **extra}


def _checkpoint(env, pages=None, *, generation=1, kind="v1-batch", repair=None):
    pages = pages or [_page()]
    with review_runtime(env["run_id"], env["bidder_id"], env["criterion_id"], generation, env["manifest"]):
        return CollectionCheckpoint({"name": "Evidence"}, [[page] for page in pages],
                                    kind=kind, shared=kind.startswith("v2-"),
                                    data_class="synthetic", extra={"repair": repair})


def _result(unresolved=None):
    return {"valid": True, "unresolved": unresolved or []}


def _validate(result):
    if not isinstance(result, dict) or result.get("valid") is not True:
        raise LLMError("invalid evidence")


def _batches(checkpoint):
    with session_scope() as session:
        return session.execute(select(ReviewCollectionBatch).where(
            ReviewCollectionBatch.collection_id == checkpoint.collection_id).order_by(ReviewCollectionBatch.id)).scalars().all()


def _collection(checkpoint):
    with session_scope() as session:
        return session.get(ReviewCollectionSet, checkpoint.collection_id)


def test_restart_reuses_committed_result_and_true_completion_time(checkpoint_env):
    checkpoint = _checkpoint(checkpoint_env)
    checkpoint.execute(0, _result, _validate)
    checkpoint.ready()
    completed_at = _batches(checkpoint)[0].completed_at
    ready_at = _collection(checkpoint).completed_at
    recover_interrupted_batches(checkpoint_env["run_id"], 2)
    restarted = _checkpoint(checkpoint_env, generation=2)
    assert restarted.collection_id == checkpoint.collection_id
    restarted.execute(0, lambda: pytest.fail("committed work must not call the model"), _validate)
    restarted.ready()
    batch = _batches(restarted)[0]
    assert batch.completed_at == completed_at
    assert _collection(restarted).completed_at == ready_at
    assert batch.attempt_count == 1
    assert batch.metrics_json == {"reused_batches": 1, "queued_requests": 0, "inflight_requests": 0}


def test_last_failure_preserves_earlier_batch_and_recovers_only_unfinished(checkpoint_env):
    pages = [_page(), _page(2)]
    checkpoint = _checkpoint(checkpoint_env, pages)
    checkpoint.execute(0, _result, _validate)
    first = _batches(checkpoint)[0]

    def fail():
        raise TimeoutError("temporary model failure")

    with pytest.raises(TimeoutError):
        checkpoint.execute(1, fail, _validate)
    rows = _batches(checkpoint)
    assert rows[0].result_json == first.result_json
    assert rows[0].completed_at == first.completed_at
    assert rows[1].completed_at is None
    assert _collection(checkpoint).state == "failed"
    assert _collection(checkpoint).completed_at is None
    with pytest.raises(LLMError):
        checkpoint.ready()
    recover_interrupted_batches(checkpoint_env["run_id"], 2)
    restarted = _checkpoint(checkpoint_env, pages, generation=2)
    restarted.execute(0, lambda: pytest.fail("completed batch repeated"), _validate)
    restarted.execute(1, _result, _validate)
    restarted.ready()
    assert _collection(restarted).state == "ready"
    assert [row.attempt_count for row in _batches(restarted)] == [1, 2]


def test_semantic_failure_history_is_saved_without_publishing_invalid_result(checkpoint_env):
    checkpoint = _checkpoint(checkpoint_env)
    history = [{"attempt": n, "state": "failed", "error": "引文不属于来源范围",
                "response_sha256": str(n) * 64} for n in range(3)]

    def fail():
        error = LLMError("证据定向修复已耗尽")
        error.review_attempts_exhausted = True
        error.validation_history = deepcopy(history)
        raise error

    with pytest.raises(LLMError, match="耗尽"):
        checkpoint.execute(0, fail, _validate)
    saved = _batches(checkpoint)[0]
    assert saved.state == "failed" and saved.result_json is None
    assert saved.error_json["validation_history"] == history
    assert saved.completed_at is None


@pytest.mark.parametrize("failure", [False, True])
def test_recovery_rejects_late_success_and_failure(checkpoint_env, failure):
    checkpoint = _checkpoint(checkpoint_env)

    def interrupted():
        recover_interrupted_batches(checkpoint_env["run_id"], 2)
        if failure:
            raise TimeoutError("late failure")
        return _result()

    with pytest.raises(TimeoutError if failure else LLMError):
        checkpoint.execute(0, interrupted, _validate)
    collection, batch = _collection(checkpoint), _batches(checkpoint)[0]
    assert collection.generation == batch.generation == 2
    assert collection.state == batch.state == "pending"
    assert batch.attempt_token is None
    assert batch.result_json is None and batch.completed_at is None
    assert batch.error_json == {"code": "interrupted_before_commit"}
    with pytest.raises(LLMError, match="代次"):
        checkpoint.ready()
    with pytest.raises(LLMError, match="代次"):
        recover_interrupted_batches(checkpoint_env["run_id"], 1)


def test_corrupt_result_revalidated_and_recomputed(checkpoint_env):
    checkpoint = _checkpoint(checkpoint_env)
    checkpoint.execute(0, _result, _validate)
    checkpoint.ready()
    with session_scope() as session:
        batch = session.get(ReviewCollectionBatch, _batches(checkpoint)[0].id)
        batch.result_json = {"valid": False}

    def retry():
        row = _batches(checkpoint)[0]
        assert row.completed_at is None
        assert _collection(checkpoint).completed_at is None
        return _result()

    checkpoint.execute(0, retry, _validate)
    assert _batches(checkpoint)[0].attempt_count == 2
    assert _batches(checkpoint)[0].result_json["valid"] is True


@pytest.mark.parametrize("field", ["input_data", "units", "batch_keys"])
def test_immutable_plan_tampering_is_rejected(checkpoint_env, field):
    checkpoint = _checkpoint(checkpoint_env)
    with session_scope() as session:
        row = session.get(ReviewCollectionSet, checkpoint.collection_id)
        plan = deepcopy(row.plan_json)
        plan[field] = {} if field == "input_data" else []
        row.plan_json = plan
    with pytest.raises(LLMError, match="冻结输入"):
        _checkpoint(checkpoint_env)
    with pytest.raises(LLMError, match="冻结输入"):
        checkpoint.execute(0, _result, _validate)


def test_repartition_reuses_exact_independent_batches_with_provenance(checkpoint_env):
    old = _checkpoint(checkpoint_env, [_page(), _page(2)], kind="v1-text")
    for index in (0, 1):
        old.execute(index, _result, _validate)
    old.ready()
    original = _batches(old)
    revised = _checkpoint(checkpoint_env, [_page(2), _page(), _page(3)], kind="v1-text")
    for index in (0, 1):
        revised.execute(index, lambda: pytest.fail("unchanged source batch called model"), _validate)
    revised.execute(2, _result, _validate)
    revised.ready()
    rows = _batches(revised)
    assert [row.attempt_count for row in rows] == [0, 0, 1]
    assert rows[0].metrics_json["reuse_provenance"]["source_batch_id"] == original[1].id
    assert rows[1].metrics_json["reuse_provenance"]["source_batch_id"] == original[0].id
    after = _batches(old)
    assert [(r.result_json, r.attempt_count, r.completed_at, r.metrics_json) for r in after] == [
        (r.result_json, r.attempt_count, r.completed_at, r.metrics_json) for r in original]


def test_repartition_revalidates_source_result_without_modifying_it(checkpoint_env):
    old = _checkpoint(checkpoint_env, [_page(), _page(2)], kind="v1-text")
    old.execute(0, _result, _validate)
    old_id = _batches(old)[0].id
    with session_scope() as session:
        session.get(ReviewCollectionBatch, old_id).result_json = {"valid": False}
    revised = _checkpoint(checkpoint_env, [_page(), _page(2), _page(3)], kind="v1-text")
    revised.execute(0, _result, _validate)
    assert _batches(revised)[0].attempt_count == 1
    assert _batches(old)[0].result_json == {"valid": False}


@pytest.mark.parametrize("change", ["model", "feedback", "text"])
def test_repartition_does_not_reuse_changed_semantics_or_source(checkpoint_env, change):
    old = _checkpoint(checkpoint_env, [_page(), _page(2)], kind="v1-text")
    old.execute(0, _result, _validate)
    revised_env = deepcopy(checkpoint_env)
    if change == "model":
        revised_env["manifest"]["model"] = "different-frozen-model"
    pages = [_page(text="Different original evidence" if change == "text" else "Original evidence"),
             _page(2), _page(3)]
    revised = _checkpoint(revised_env, pages, kind="v1-text", repair="new feedback" if change == "feedback" else None)
    calls = []

    def perform():
        calls.append(True)
        return _result()

    revised.execute(0, perform, _validate)
    assert calls == [True] and _batches(revised)[0].attempt_count == 1


def test_http_cache_metrics_do_not_double_count_queue_or_heartbeat(checkpoint_env):
    from app.llm.request_control import _emit, emit_cache_hit, request_scope

    checkpoint = _checkpoint(checkpoint_env)
    outer_events = []

    def perform():
        _emit({"event": "started", "queue_ms": 13})
        _emit({"event": "finished", "queue_ms": 13, "service_ms": 29,
               "tokens_in": 100, "tokens_out": 20, "error_type": None})
        _emit({"event": "started", "queue_ms": 7})
        _emit({"event": "finished", "queue_ms": 7, "service_ms": 11,
               "tokens_in": None, "tokens_out": None, "error_type": "TimeoutError"})
        emit_cache_hit("disk")
        _emit({"event": "heartbeat", "service_ms": 99999})
        return _result()

    with request_scope(on_event=outer_events.append):
        checkpoint.execute(0, perform, _validate)
    metrics = _batches(checkpoint)[0].metrics_json
    assert metrics == {"http_attempts": 2, "http_completed": 1, "http_failed": 1,
                       "queue_wait_ms": 20, "request_ms": 40, "tokens_in": 100,
                       "tokens_out": 20, "cache_hits": 1, "queued_requests": 0, "inflight_requests": 0}
    assert len(outer_events) == 6
    checkpoint.execute(0, lambda: pytest.fail("repeated model call"), _validate)
    assert _batches(checkpoint)[0].metrics_json == {**metrics, "reused_batches": 1}


def test_failure_preserves_real_request_metrics(checkpoint_env):
    from app.llm.request_control import _emit

    checkpoint = _checkpoint(checkpoint_env)

    def fail():
        _emit({"event": "started", "queue_ms": 2})
        _emit({"event": "finished", "service_ms": 3, "error_type": "TimeoutError"})
        raise TimeoutError("model")

    with pytest.raises(TimeoutError):
        checkpoint.execute(0, fail, _validate)
    assert _batches(checkpoint)[0].metrics_json == {
        "http_attempts": 1, "queue_wait_ms": 2, "http_failed": 1, "request_ms": 3,
        "queued_requests": 0, "inflight_requests": 0}


def _unresolved(page):
    return [{"source_unit_ids": [source_unit(page)["unit_id"]], "required_action": "ocr"}]


def test_v1_missing_evidence_is_collection_result_not_ready_failure(checkpoint_env):
    checkpoint = _checkpoint(checkpoint_env)
    checkpoint.execute(0, lambda: {**_result(), "missing_evidence": ["contract"]}, _validate)
    checkpoint.ready()
    assert _collection(checkpoint).state == "ready"


def test_v2_unresolved_blocks_ready_until_same_page_supplement_ready(checkpoint_env):
    original = _page(text="Unreadable", unreadable=True)
    requests = _unresolved(original)
    base = _checkpoint(checkpoint_env, [original], kind="v2-shared")
    base.execute(0, lambda: _result(requests), _validate)
    with pytest.raises(LLMError, match="未解决"):
        base.ready()
    replacement = _page(text="OCR evidence", source="ocr")
    supplement = _checkpoint(checkpoint_env, [replacement], kind="v2-shared", repair=json.dumps(requests))
    supplement.execute(0, _result, _validate)
    mapping = {source_unit(original)["unit_id"]: [source_unit(replacement)["unit_id"]]}
    with pytest.raises(LLMError, match="尚未就绪"):
        base.ready(resolution_collection_ids=[supplement.collection_id], resolution_unit_map=mapping)
    supplement.ready()
    base.ready(resolution_collection_ids=[supplement.collection_id], resolution_unit_map=mapping)
    plan = _collection(base).plan_json
    assert plan["resolution_collection_ids"] == [supplement.collection_id]
    assert plan["resolution_unit_map"] == mapping
    assert plan["input_data"] == base.input_data


@pytest.mark.parametrize("change", [{"page_no": 2}, {"bidder_id": 2}, {"sha256": "changed-file"}])
def test_v2_rejects_foreign_supplement_page_or_owner(checkpoint_env, change):
    original = _page()
    requests = _unresolved(original)
    base = _checkpoint(checkpoint_env, [original], kind="v2-shared")
    base.execute(0, lambda: _result(requests), _validate)
    replacement = {**_page(text="OCR evidence"), **change}
    supplement = _checkpoint(checkpoint_env, [replacement], kind="v2-shared", repair=json.dumps(requests))
    supplement.execute(0, _result, _validate)
    supplement.ready()
    with pytest.raises(LLMError, match="归属"):
        base.ready(resolution_collection_ids=[supplement.collection_id],
                   resolution_unit_map={source_unit(original)["unit_id"]: [source_unit(replacement)["unit_id"]]})
    assert _collection(base).state == "completed"


def test_v2_multiple_supplement_rounds_have_verified_transitive_lineage(checkpoint_env):
    original, middle, final = _page(text="scan"), _page(text="partial OCR"), _page(text="verified OCR")
    base = _checkpoint(checkpoint_env, [original], kind="v2-shared")
    base.execute(0, lambda: _result(_unresolved(original)), _validate)
    intermediate = _checkpoint(checkpoint_env, [middle], kind="v2-shared", repair=json.dumps(_unresolved(original)))
    intermediate.execute(0, lambda: _result(_unresolved(middle)), _validate)
    last = _checkpoint(checkpoint_env, [final], kind="v2-shared", repair=json.dumps(_unresolved(middle)))
    last.execute(0, _result, _validate)
    last.ready()
    intermediate.ready(resolution_collection_ids=[last.collection_id],
                       resolution_unit_map={source_unit(middle)["unit_id"]: [source_unit(final)["unit_id"]]})
    base.ready(resolution_collection_ids=[intermediate.collection_id],
               resolution_unit_map={source_unit(original)["unit_id"]: [source_unit(final)["unit_id"]]})
    assert _collection(base).state == "ready"


def test_image_payload_changes_source_identity_even_with_same_claimed_image_hash():
    first = _page(image_url="data:image/png;base64,first", image_sha256="claimed-hash")
    second = {**first, "image_url": "data:image/png;base64,second"}
    assert source_unit(first)["unit_id"] != source_unit(second)["unit_id"]
    assert "data:image" not in json.dumps(source_unit(first))


@pytest.mark.parametrize("case", ["missing", "gap", "overlap", "out_of_range", "foreign", "duplicate_original", "duplicate_blank"])
def test_coverage_rejects_gaps_overlaps_duplicates_and_foreign_pages(case):
    original = _page(text="abcdef")
    pages, batches = [original], [[dict(original)]]
    if case == "missing":
        batches = []
    elif case == "gap":
        batches = [[{**original, "text": "abc", "_text_start": 0, "_text_end": 3}]]
    elif case == "overlap":
        batches = [[dict(original)], [dict(original)]]
    elif case == "out_of_range":
        batches = [[{**original, "_text_end": 7}]]
    elif case == "foreign":
        batches = [[{**original, "page_no": 2}]]
    elif case == "duplicate_original":
        pages = [original, dict(original)]
    else:
        original = _page(text="")
        pages, batches = [original], [[dict(original)], [dict(original)]]
    with pytest.raises(LLMError):
        verify_text_coverage(pages, batches)


def test_coverage_accepts_exact_split_and_unique_blank_page():
    original, blank = _page(text="abcdef"), _page(2, text="")
    verify_text_coverage([original, blank], [
        [{**original, "text": "abc", "_text_start": 0, "_text_end": 3}],
        [{**original, "text": "def", "_text_start": 3, "_text_end": 6}, blank],
    ])


def test_real_shared_collector_supplement_links_are_saved_with_database_checkpoint(checkpoint_env, monkeypatch):
    from app.engine import auto_review as review, shared_review as shared
    from app.services import automatic_scoring_service as scoring

    original = _page(text="扫描页面需要重新识别原始证据内容。", unreadable=True)
    replacement = {**original, "text": "重新识别后可读取的原始证据内容完整且清晰。", "unreadable": False, "source": "ocr"}
    rule = {"name": "Evidence", "_frozen_bidders": [{"id": original["bidder_id"]}],
            "_comparison_documents": [{"document_id": original["document_id"], "bidder_id": original["bidder_id"], "page_count": 1}]}
    monkeypatch.setattr(shared, "execution_config_value", lambda name: 1)
    monkeypatch.setattr(shared, "assert_request_fits", lambda *args, **kwargs: None)
    monkeypatch.setattr(review, "_batches", lambda pages, **kwargs: [pages])
    monkeypatch.setattr(scoring, "_supplement_pages", lambda *args, **kwargs: [replacement])
    calls = []

    class Gateway:
        def chat_json(self, system, user, schema, **kwargs):
            payload = json.loads(user.split("<collection>\n", 1)[1].rsplit("\n</collection>", 1)[0])
            calls.append(payload)
            requests = [] if payload["repair"] else [{
                "source_unit_ids": [payload["source_units"][0]["source_unit_id"]],
                "required_action": "ocr", "target": "original scan", "reason_code": "unreadable"}]
            return schema.model_validate({"schema_version": 2, "evidence": [], "facts": [], "unresolved": requests}), None

    with review_runtime(checkpoint_env["run_id"], checkpoint_env["bidder_id"], checkpoint_env["criterion_id"], 1, checkpoint_env["manifest"]):
        result = shared.SharedReviewCollector()._collect(Gateway(), rule, [original], "synthetic")
    assert len(calls) == 2
    assert result.pages == [replacement]
    with session_scope() as session:
        collections = session.execute(select(ReviewCollectionSet).order_by(ReviewCollectionSet.id)).scalars().all()
        assert all(row.state == "ready" for row in collections)
        assert collections[0].plan_json["resolution_unit_map"] == {
            source_unit(original)["unit_id"]: [source_unit(replacement)["unit_id"]]}


def test_live_queue_and_http_gauges_do_not_advance_completion(checkpoint_env):
    from app.llm.request_control import _emit
    from app.services.automation_service import execution_progress

    with session_scope() as session:
        run = session.get(EvaluationRun, checkpoint_env["run_id"])
        run.status = "running"
        run.config_json = {"automation_version": 1, "executor_version": 2}
    checkpoint = _checkpoint(checkpoint_env)

    def perform():
        _emit({"event": "queued"})
        progress = execution_progress(checkpoint_env["run_id"])
        assert (progress["requests_queued"], progress["requests_inflight"]) == (1, 0)
        assert progress["last_progress_at"] is None
        _emit({"event": "started", "queue_ms": 2})
        progress = execution_progress(checkpoint_env["run_id"])
        assert (progress["requests_queued"], progress["requests_inflight"]) == (0, 1)
        assert progress["last_progress_at"] is None
        _emit({"event": "finished", "service_ms": 4, "error_type": None})
        assert execution_progress(checkpoint_env["run_id"])["requests_inflight"] == 0
        return _result()

    checkpoint.execute(0, perform, _validate)
    progress = execution_progress(checkpoint_env["run_id"])
    assert (progress["read_pages_completed"], progress["read_pages_total"]) == (1, 1)
    assert progress["last_progress_at"] is not None


def test_page_progress_waits_for_all_slices_and_reports_exact_failure_location(checkpoint_env):
    from app.services.automation_service import execution_progress

    original = _page(text="abcdef")
    slices = [{**original, "text": "abc", "_text_start": 0, "_text_end": 3},
              {**original, "text": "def", "_text_start": 3, "_text_end": 6}]
    with session_scope() as session:
        run = session.get(EvaluationRun, checkpoint_env["run_id"])
        run.status, run.config_json = "running", {"automation_version": 1}
    checkpoint = _checkpoint(checkpoint_env, slices)
    checkpoint.execute(0, _result, _validate)
    progress = execution_progress(checkpoint_env["run_id"])
    assert progress["read_units_completed"] == 1
    assert progress["read_pages_total"] == 1 and progress["read_pages_completed"] == 0
    with pytest.raises(TimeoutError):
        checkpoint.execute(1, lambda: (_ for _ in ()).throw(TimeoutError("specific batch timeout")), _validate)
    progress = execution_progress(checkpoint_env["run_id"])
    failure = progress["failures"][0]
    assert failure["batch_index"] == 1 and failure["code"] == "TimeoutError"
    assert failure["documents"] == [{"document_id": original["document_id"], "filename": None, "page_numbers": [1]}]
    assert failure["message"] == "specific batch timeout"
    with session_scope() as session:
        session.get(EvaluationRun, checkpoint_env["run_id"]).status = "failed"
        batch = session.get(ReviewCollectionBatch, _batches(checkpoint)[1].id)
        batch.state, batch.metrics_json = "running", {"queued_requests": 1, "inflight_requests": 2}
    progress = execution_progress(checkpoint_env["run_id"])
    assert progress["requests_queued"] == progress["requests_inflight"] == progress["batches_running"] == 0


def test_cancel_before_http_dispatch_clears_queue_without_counting_request(checkpoint_env, monkeypatch):
    from app.llm import request_control as control

    events = []
    monkeypatch.setattr(control, "_reserve_budget", lambda: (_ for _ in ()).throw(RuntimeError("budget denied")))
    with control.request_scope(on_event=events.append), pytest.raises(RuntimeError, match="budget"):
        with control.http_attempt(provider="offline", model="offline"):
            pytest.fail("budget rejection must not dispatch HTTP")
    assert [event["event"] for event in events] == ["queued", "queue_cancelled"]


def test_http_slots_never_exceed_eight_even_when_configuration_is_larger(checkpoint_env, monkeypatch):
    from app import config
    from app.llm import request_control as control

    monkeypatch.setattr(config, "llm_max_concurrency", lambda: 999)
    monkeypatch.setattr(control.threading, "get_ident", lambda: 8)
    opened = []
    original_open = control.os.open

    def record_open(path, *args):
        opened.append(str(path))
        return original_open(path, *args)

    monkeypatch.setattr(control.os, "open", record_open)
    with control._slot():
        assert opened[-1].endswith("slot-0.lock")


def test_run_observer_covers_synthesis_and_does_not_double_count_nested_batch(checkpoint_env):
    from app.llm.request_control import _emit, request_scope
    from app.services.automation_service import execution_progress
    from app.services.review_collections import run_activity_observer

    with session_scope() as session:
        run = session.get(EvaluationRun, checkpoint_env["run_id"])
        run.status = "running"
        run.config_json = {"automation_version": 1, "automation_manifest": {"criteria": [{"id": checkpoint_env["criterion_id"]}]}}
    observer = run_activity_observer(checkpoint_env["run_id"], 1)
    checkpoint = _checkpoint(checkpoint_env)

    def perform():
        _emit({"event": "queued", "request_id": "collection"})
        _emit({"event": "started", "request_id": "collection", "queue_ms": 2})
        _emit({"event": "finished", "request_id": "collection", "service_ms": 4, "error_type": None})
        return _result()

    with request_scope(on_event=observer):
        checkpoint.execute(0, perform, _validate)
        _emit({"event": "queued", "request_id": "synthesis"})
        progress = execution_progress(checkpoint_env["run_id"])
        assert progress["requests_queued"] == 1 and progress["requests_inflight"] == 0
        _emit({"event": "started", "request_id": "synthesis", "queue_ms": 3})
        progress = execution_progress(checkpoint_env["run_id"])
        assert progress["requests_queued"] == 0 and progress["requests_inflight"] == 1
        _emit({"event": "finished", "request_id": "synthesis", "service_ms": 5, "error_type": None})
    progress = execution_progress(checkpoint_env["run_id"])
    assert progress["request_scope"] == "run"
    assert progress["collections_total"] == progress["batches_total"] == 1
    assert progress["metrics"]["http_attempts"] == 2
    assert progress["metrics"]["queue_wait_ms"] == 5 and progress["metrics"]["request_ms"] == 9
    assert progress["requests_queued"] == progress["requests_inflight"] == 0
    repeated = {"event": "started", "event_id": "same-event", "request_id": "same-request", "queue_ms": 1}
    observer(repeated)
    observer(repeated)
    assert execution_progress(checkpoint_env["run_id"])["metrics"]["http_attempts"] == 3
    run_activity_observer(checkpoint_env["run_id"], 2)
    observer({"event": "started", "event_id": "late-old", "request_id": "late-old"})
    assert execution_progress(checkpoint_env["run_id"])["requests_inflight"] == 0


def test_completed_score_counts_as_progress_without_collection_batches(checkpoint_env):
    from app.db.models import ScoreRecord
    from app.services.automation_service import execution_progress

    with session_scope() as session:
        run = session.get(EvaluationRun, checkpoint_env["run_id"])
        run.status, run.config_json = "running", {"automation_version": 1}
        row = ScoreRecord(run_id=run.id, bidder_id=checkpoint_env["bidder_id"],
            criterion_id=checkpoint_env["criterion_id"], review_status="pending", machine_status="execution_failed",
            ai_raw_json={"auto_review": {"state": "failed"}})
        session.add(row)
        session.flush()
        row_id = row.id
    assert execution_progress(checkpoint_env["run_id"])["last_progress_at"] is None
    with session_scope() as session:
        row = session.get(ScoreRecord, row_id)
        row.review_status, row.machine_status = "auto_confirmed", "pass"
        row.ai_raw_json = {"auto_review": {"state": "completed"}}
    progress = execution_progress(checkpoint_env["run_id"])
    assert progress["batches_total"] == 0 and progress["last_progress_at"].endswith("Z")
