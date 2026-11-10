"""The real executor binds immutable identity and shares collection across cells."""
from __future__ import annotations

import json
import threading
from decimal import Decimal

from sqlalchemy import select

from app.db.models import (Bidder, Document, DocumentPage, EvaluationRun, EvidenceCitation,
                           ReviewCollectionBatch, ReviewCollectionSet, ScoreRecord)
from app.db.session import session_scope
from app.llm.gateway import Gateway as ProductionGateway
from app.services import automation_service as automation
from app.services import automatic_scoring_service as scoring
from app.services.execution_version import current_execution_manifest, execution_config_value
from .test_automatic_scoring import managed_run


def test_real_v2_cells_share_validated_batches_and_keep_separate_verdicts(managed_run, monkeypatch):
    with session_scope() as s:
        old = s.get(EvaluationRun, managed_run["id"])
        original_doc = s.get(Document, managed_run["document"])
        peer = Bidder(project_id=old.project_id, name="乙", bid_price=Decimal("120"), price_status="confirmed")
        s.add(peer)
        s.flush()
        peer_doc = Document(project_id=old.project_id, bidder_id=peer.id, kind="bid", parse_status="parsed",
            original_filename="peer.txt", stored_path=original_doc.stored_path,
            sha256=original_doc.sha256, page_count=1, data_class="synthetic")
        s.add(peer_doc)
        s.flush()
        s.add(DocumentPage(document_id=peer_doc.id, page_no=1, text=managed_run["text"], source="text"))
        run = EvaluationRun(project_id=old.project_id, package_id=old.package_id, package_version=1,
            package_content_hash="frozen", status="queued", stages_json=["objective"],
            config_json={"automation_version": 1, "executor_version": 2,
                         "bidder_ids": [managed_run["bidder"], peer.id]})
        s.add(run)
        s.flush()
        run_id, peer_id = run.id, peer.id
    rule = {**managed_run["criteria"][0], "name": "不同投标人由同一单位编制的串通投标情形", "check_type": "human_only"}
    manifest = automation.initialize_run(run_id, [rule])
    monkeypatch.setenv("BID_EVAL_LLM_CONCURRENCY", "8")
    lock, calls = threading.Lock(), []

    class Provider:
        def __init__(self, **kwargs):
            pass

    class Gateway:
        _repair_user = staticmethod(ProductionGateway._repair_user)

        def __init__(self, provider):
            pass

        def chat_json(self, system, user, out_model, **kwargs):
            bound = current_execution_manifest()
            assert bound["executor_version"] == 2
            assert kwargs["prompt_version"] == manifest["prompt_version"]
            payload = json.loads(user.split("\n", 1)[1].rsplit("\n", 1)[0])
            with lock:
                calls.append(payload)
            if "source_units" in payload:
                refs = [{"evidence_id": f"e{i}", "source_unit_id": unit["source_unit_id"],
                         "kind": "text", "source_id": unit["spans"][0]["source_id"]}
                        for i, unit in enumerate(payload["source_units"])]
                reply = {"schema_version": 2, "evidence": refs, "facts": [], "unresolved": []}
            else:
                bidder = payload["rule"]["_frozen_bidder"]["id"]
                evidence = next(e for e in payload["batch_results"][0]["evidence"] if e["bidder_id"] == bidder)
                reply = {"verdict": "fail" if bidder == peer_id else "pass",
                         "reasoning": "已独立比较冻结材料并形成此投标人的结论",
                         "evidence": [{"evidence_id": evidence["evidence_id"]}]}
            return out_model.model_validate(reply), None

    monkeypatch.setattr("app.llm.registry.get_provider", lambda *args: Provider())
    monkeypatch.setattr("app.llm.gateway.Gateway", Gateway)
    result = scoring.execute_automatic_run(run_id)
    assert result["automation"]["complete"]
    assert len([p for p in calls if "source_units" in p]) == 1
    assert len(calls) == 3
    with session_scope() as s:
        scores = s.execute(select(ScoreRecord).where(ScoreRecord.run_id == run_id)).scalars().all()
        assert {row.bidder_id: row.machine_status for row in scores} == {managed_run["bidder"]: "pass", peer_id: "fail"}
        assert s.execute(select(ReviewCollectionSet).where(ReviewCollectionSet.run_id == run_id,
            ReviewCollectionSet.group_key.like("v2-shared:%"))).scalar_one().state == "ready"
        assert s.execute(select(ReviewCollectionBatch)).scalar_one().state == "completed"
        for row in scores:
            assert len(s.execute(select(EvidenceCitation).where(EvidenceCitation.score_record_id == row.id)).scalars().all()) == 1


def test_v1_worker_uses_frozen_prompt_after_global_prompt_upgrade(managed_run, monkeypatch):
    with session_scope() as s:
        manifest = s.get(EvaluationRun, managed_run["id"]).config_json["automation_manifest"]
    monkeypatch.setenv("BID_EVAL_PROMPT_VERSION", "new-v2-prompt-must-not-change-v1")
    seen = []

    def assess(rule, pages, data_class, factory):
        seen.append(current_execution_manifest()["prompt_version"])
        assert execution_config_value("auto_review_rounds") == manifest["scoring_config"]["auto_review_rounds"]
        return scoring.CellOutcome("pass", "已验证冻结规则与证据", Decimal("8") if rule["score_type"] == "subjective" else None,
            [{"document_id": managed_run["document"], "page_no": 1, "quote": managed_run["text"], "kind": "text"}], pages)

    monkeypatch.setattr(scoring, "_assess_cell", assess)
    assert scoring.execute_automatic_run(managed_run["id"])["automation"]["complete"]
    assert seen == [manifest["prompt_version"]] * 2
