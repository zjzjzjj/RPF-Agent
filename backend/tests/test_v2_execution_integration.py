"""The actual root, worker and DB checkpoints share one frozen V2 identity."""
from __future__ import annotations

import json

from sqlalchemy import select

from app.db.models import EvaluationRun, ReviewCollectionBatch, ReviewCollectionSet, ScoreRecord
from app.db.session import session_scope
from app.engine import auto_review as review
from app.services import automation_service as automation, scoring_service
from app.services.execution_version import V2_PROMPT_VERSION, current_execution_manifest
from app.services.review_collections import current_review_runtime
from .test_automatic_scoring import managed_run  # noqa: F401


def test_v2_actual_run_uses_shared_collector_and_commits_final_evidence(managed_run, monkeypatch):
    criterion = {**managed_run["criteria"][0], "name": "串通投标情形", "check_type": "human_only"}
    with session_scope() as session:
        run = session.get(EvaluationRun, managed_run["id"])
        run.config_json = {"automation_version": 1, "executor_version": 2, "bidder_ids": [managed_run["bidder"]]}
        run.prompt_version = None
    frozen = automation.initialize_run(managed_run["id"], [criterion])
    calls = {"collect": 0, "synthesis": 0}

    class Gateway:
        _repair_user = staticmethod(review.Gateway._repair_user)

        def __init__(self, provider):
            pass

        def chat_json(self, system, user, schema, **kwargs):
            calls["collect"] += 1
            assert current_execution_manifest() == frozen
            runtime = current_review_runtime()
            assert runtime["run_id"] == managed_run["id"]
            assert runtime["manifest"] == frozen and runtime["generation"] > 1
            assert kwargs["prompt_version"] == V2_PROMPT_VERSION
            payload = json.loads(user.split("<collection>\n", 1)[1].rsplit("\n</collection>", 1)[0])
            return schema.model_validate({"schema_version": 2,
                "evidence": [{"evidence_id": "original", "source_unit_id": payload["source_units"][0]["source_unit_id"],
                              "kind": "text", "quote": managed_run["text"]}],
                "facts": [], "unresolved": []}), None

    def synthesize(gateway, criterion, pages, *, summaries, **kwargs):
        calls["synthesis"] += 1
        assert current_execution_manifest() == frozen
        assert current_review_runtime()["manifest"] == frozen
        return review.AutoReviewDecision(verdict="unverifiable", reasoning="现有材料无法证实，缺少平台日志及外部协商记录。",
                                         evidence=summaries[0]["evidence"])

    monkeypatch.setattr("app.llm.registry.get_provider", lambda *args: object())
    monkeypatch.setattr("app.llm.gateway.Gateway", Gateway)
    monkeypatch.setattr(review, "synthesis_decision", synthesize)
    result = scoring_service.execute_run(managed_run["id"])
    assert result["automation"]["complete"] and result["need_human"] == 0
    assert calls == {"collect": 1, "synthesis": 1}
    assert current_execution_manifest() is None and current_review_runtime() is None
    with session_scope() as session:
        collection = session.execute(select(ReviewCollectionSet).where(
            ReviewCollectionSet.plan_json["kind"].as_string() == "v2-shared")).scalar_one()
        batch = session.execute(select(ReviewCollectionBatch)).scalar_one()
        score = session.execute(select(ScoreRecord)).scalar_one()
        assert collection.state == "ready" and batch.state == "completed"
        assert collection.plan_json["input_data"]["execution_identity"]["executor_version"] == 2
        assert score.machine_status == "unverifiable" and score.review_status == "auto_confirmed"
