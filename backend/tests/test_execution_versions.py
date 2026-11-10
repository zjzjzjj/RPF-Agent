"""Versioned execution keeps old runs pinned while enabling explicit V2 canaries."""
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.api.runs import RunCreate
from app.db.models import Bidder, Criterion, EvaluationRun, Job, Project, RulePackage
from app.db.session import session_scope
from app.errors import Conflict, ValidationFailed
from app.services import automation_service as auto, scoring_service
from app.services.execution_version import (
    V2_PROMPT_VERSION, bind_run_execution, default_executor_version,
    execution_config_value, execution_prompt_version, run_executor_version,
)


@pytest.fixture
def versioned(app_env, monkeypatch):
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    monkeypatch.delenv("BID_EVAL_EXECUTOR_DEFAULT_VERSION", raising=False)
    with session_scope() as s:
        s.add(Project(id="version-test", name="Version test"))
        s.flush()
        pkg = RulePackage(project_id="version-test", version=1, status="frozen", content_hash="frozen")
        s.add(pkg)
        s.flush()
        bidder = Bidder(project_id="version-test", name="A", bid_price=Decimal("100"), price_status="confirmed")
        criterion = Criterion(package_id=pkg.id, code="Q1", name="Document", category="qualification", score_type="match", status="verified")
        s.add_all([bidder, criterion])
        s.flush()
        ids = {"package_id": pkg.id, "bidder_id": bidder.id, "criterion_id": criterion.id}
    criteria = [{"id": ids["criterion_id"], "code": "Q1", "name": "Document", "score_type": "match", "max_score": None}]
    monkeypatch.setattr(scoring_service, "_snapshot_criteria", lambda _: ({}, criteria))
    return ids, criteria


def _create(values, version=None):
    ids, _ = values
    return scoring_service.create_run("version-test", ids["package_id"], [ids["bidder_id"]], ["objective"], "test", executor_version=version)[0]


def _fail_run(run_id):
    with session_scope() as s:
        s.get(EvaluationRun, run_id).status = "failed"
        for job in s.execute(select(Job)).scalars():
            if (job.payload_json or {}).get("run_id") == run_id:
                job.status = "failed"


def test_new_default_stays_v1_and_explicit_v2_has_separate_prompt(versioned):
    first = _create(versioned)
    second = _create(versioned, 2)
    with session_scope() as s:
        one, two = s.get(EvaluationRun, first), s.get(EvaluationRun, second)
        assert run_executor_version(one) == 1
        assert run_executor_version(two) == 2
        assert two.config_json["automation_manifest"]["executor_version"] == 2
        assert two.prompt_version == V2_PROMPT_VERSION
        assert one.prompt_version != two.prompt_version


def test_unknown_version_fails_before_creating_run(versioned):
    with pytest.raises(ValidationFailed, match="执行器版本"):
        _create(versioned, 99)
    with pytest.raises(ValidationError):
        RunCreate(bidder_ids=[1], executor_version=True)


def test_existing_missing_version_is_v1_and_global_prompt_does_not_change_it(versioned, monkeypatch):
    run_id = _create(versioned)
    _fail_run(run_id)
    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        run.status = "failed"
        run.config_json = {k: v for k, v in run.config_json.items() if k != "executor_version"}
        frozen = run.config_json["automation_manifest"]
    monkeypatch.setenv("BID_EVAL_PROMPT_VERSION", "new-unrelated-global-prompt")
    assert auto.initialize_run(run_id, versioned[1]) == frozen
    assert auto.resume_run(run_id, "test")[0] == run_id


def test_frozen_semantic_setting_change_still_rejects_resume(versioned, monkeypatch):
    run_id = _create(versioned)
    _fail_run(run_id)
    monkeypatch.setenv("BID_EVAL_LLM_SEED", "87291")
    with pytest.raises(Conflict, match="配置已变化"):
        auto.resume_run(run_id, "test")


def test_context_reads_frozen_parameters_and_resets(monkeypatch):
    monkeypatch.setenv("BID_EVAL_PROMPT_VERSION", "outside")
    manifest = {"prompt_version": "frozen", "scoring_config": {"llm_seed": 7}}
    with bind_run_execution(manifest, 1):
        assert execution_prompt_version() == "frozen"
        assert execution_config_value("llm_seed") == 7
        with bind_run_execution({"prompt_version": V2_PROMPT_VERSION}, 2):
            assert execution_prompt_version() == V2_PROMPT_VERSION
        assert execution_prompt_version() == "frozen"
    assert execution_prompt_version() == "outside"


def test_default_environment_gate_is_explicit(monkeypatch):
    monkeypatch.setenv("BID_EVAL_EXECUTOR_DEFAULT_VERSION", "2")
    assert default_executor_version() == 2
    monkeypatch.setenv("BID_EVAL_EXECUTOR_DEFAULT_VERSION", "bogus")
    with pytest.raises(ValidationFailed):
        default_executor_version()


def test_batch_activity_does_not_inflate_final_matrix_progress(versioned):
    from app.db.models import ReviewCollectionBatch, ReviewCollectionSet, utcnow

    run_id = _create(versioned, 2)
    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        run.status = "running"
        collection = ReviewCollectionSet(run_id=run_id, criterion_id=versioned[0]["criterion_id"],
                                         group_key="shared", input_signature="a" * 64, state="running")
        s.add(collection)
        s.flush()
        s.add_all([
            ReviewCollectionBatch(collection_id=collection.id, batch_key="first", state="completed",
                                  unit_ids_json=["p1", "p2"], completed_at=utcnow(), metrics_json={"cache_hits": 1}),
            ReviewCollectionBatch(collection_id=collection.id, batch_key="second", state="running", unit_ids_json=["p3"]),
        ])
    progress = auto.progress(run_id)
    assert progress["completed"] == 0
    assert progress["remaining"] == 1
    activity = progress["execution_progress"]
    assert (activity["batches_completed"], activity["batches_running"], activity["read_units_completed"]) == (1, 1, 2)
    assert activity["metrics"]["cache_hits"] == 1
    assert activity["last_progress_at"].endswith("Z")
    with session_scope() as s:
        s.get(EvaluationRun, run_id).status = "failed"
    assert auto.execution_progress(run_id)["batches_running"] == 0
