"""Immutable inputs, durable progress and finality for managed automatic runs.

Legacy runs deliberately retain their original semantics.  A managed run must
account for every frozen bidder/criterion pair, including records not yet made.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select, update

from app import config
from app.db.models import (
    AuditLog, Bidder, Criterion, Document, DocumentPage, EvaluationRun, EvidenceCitation, Job, RulePackage,
    ScoreRecord, utcnow,
    ReviewCollectionSet, ReviewCollectionBatch,
)
from app.db.session import session_scope
from app.errors import Conflict, NotFound, ValidationFailed


def _json(value):
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def _file_hash(path: str) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    digest = hashlib.sha256()
    with p.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scoring_config(*, prompt_version: str | None = None) -> dict:
    # Credentials can be repaired without changing the evaluation itself.
    values = {
        name: getattr(config, name)()
        for name in (
            "committee_size", "review_mode", "review_clusters", "selection_mode",
            "benchmark_exclude_suspect", "objective_route_enabled",
            "auto_review_rounds", "llm_seed", "llm_thinking", "llm_disable_thinking",
            "llm_default_max_tokens", "llm_max_output_tokens", "llm_context_tokens",
            "case_unverified_count_enabled", "leader_table_proof_enabled",
            "cert_backing_per_cert_enabled", "case_tag_prior_enabled",
            "case_evidence_pages_per_material", "case_digest_chars", "case_max_batches",
            "case_max_batches_ceiling", "case_section_max_pages", "case_table_scan_pages",
            "window_extend_enabled", "material_index_enabled", "material_ocr_max_pages",
            "ocr_enabled", "ocr_engine", "paddle_ocr_model", "paddle_ocr_prompt",
            "paddle_ocr_max_tokens", "calibration_profile", "vision_model", "render_scale",
        )
        if hasattr(config, name)
    }
    for name in ("llm_base_url", "vision_base_url", "paddle_ocr_url", "embed_url"):
        endpoint = getattr(config, name)() if hasattr(config, name) else None
        if endpoint:
            split = urlsplit(endpoint)
            # Never persist embedded URL credentials or signed query strings.
            host = split.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            netloc = host + (f":{split.port}" if split.port else "")
            values[name] = urlunsplit((split.scheme, netloc, split.path, "", ""))
        else:
            values[name] = None
    values["prompt_version"] = prompt_version or config.llm_prompt_version()
    return values


def is_managed(run: EvaluationRun) -> bool:
    return (run.config_json or {}).get("automation_version") == 1


def failure_policy(run: EvaluationRun) -> str:
    policy = (run.config_json or {}).get("failure_policy", "stop")
    if policy not in ("stop", "continue"):
        raise ValidationFailed("不支持的故障处理策略")
    return policy


def qualification_mode(run: EvaluationRun) -> str:
    """运行级资格审核模式；旧运行缺键回退 full（逐字保持历史语义）。"""
    mode = (run.config_json or {}).get("qualification_mode", "full")
    if mode not in ("light", "full"):
        raise ValidationFailed("不支持的资格审核模式")
    return mode


def count_deterministic_mode(run: EvaluationRun) -> str:
    """运行级客观计数快路径模式；旧运行缺键回退 off。"""
    mode = (run.config_json or {}).get("count_deterministic_mode", "off")
    if mode not in ("off", "shadow", "on"):
        raise ValidationFailed("不支持的计数快路径模式")
    return mode


def set_failure_policy(run_id: int, policy: str, actor: str) -> dict:
    if policy not in ("stop", "continue"):
        raise ValidationFailed("故障处理策略只能为 stop 或 continue")
    with session_scope() as s:
        s.execute(update(EvaluationRun).where(EvaluationRun.id == run_id).values(status=EvaluationRun.status))
        run = s.get(EvaluationRun, run_id)
        if run is None:
            raise NotFound("评分运行不存在")
        if not is_managed(run):
            raise Conflict("历史人工运行不支持自动容错策略")
        if run.status not in ("queued", "running", "failed"):
            raise Conflict("已结束的评分结果不能修改执行策略")
        old = failure_policy(run)
        if old != policy:
            run.config_json = {**(run.config_json or {}), "failure_policy": policy}
            s.add(AuditLog(project_id=run.project_id, actor=actor, action="run.failure_policy",
                           subject_type="run", subject_id=str(run_id),
                           detail_json={"before": old, "after": policy}))
    return progress(run_id)


def failed_keys(run_id: int) -> set[tuple[int, int]]:
    with session_scope() as s:
        return {(row.bidder_id, row.criterion_id) for row in s.execute(
            select(ScoreRecord).where(ScoreRecord.run_id == run_id)
        ).scalars() if ((row.ai_raw_json or {}).get("auto_review") or {}).get("state") == "failed"}


def public_auto_review(score: ScoreRecord | None) -> dict | None:
    """Expose the review trail without request payloads or embedded page images."""
    if score is None:
        return None
    raw = score.ai_raw_json or {}
    meta = raw.get("auto_review")
    if not isinstance(meta, dict):
        return None

    def clean(value):
        if isinstance(value, dict):
            return {str(k): clean(v) for k, v in value.items()
                    if "base64" not in str(k).lower() and str(k).lower() not in (
                        "image_url", "image", "images", "image_data", "image_bytes", "data_url",
                    )}
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, str):
            value = re.sub(r"data:image/[^\s\"']+", "[页图数据已省略]", value)
            return re.sub(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{512,}={0,2}(?![A-Za-z0-9+/])",
                          "[图像数据已省略]", value)
        return value

    result = {key: clean(meta[key]) for key in (
        "state", "basis", "attempts", "reasoning", "error", "error_code",
        "confirmed_absent", "coverage_complete", "calculation", "lightweight",
    ) if key in meta}
    result["visual_evidence"] = [
        {key: clean(item[key]) for key in (
            "document_id", "page_no", "observation", "quote", "sha256", "image_sha256",
        ) if key in item}
        for item in (meta.get("visual_evidence") or []) if isinstance(item, dict)
    ]
    result["attempt_history"] = [
        {key: clean(item[key]) for key in ("round", "state", "error") if key in item}
        for item in (raw.get("attempt_history") or []) if isinstance(item, dict)
    ]
    if not result.get("reasoning") and score.ai_reasoning:
        result["reasoning"] = clean(score.ai_reasoning)
    return result


def _inputs(s, run: EvaluationRun, criteria: list[dict], provider, model) -> dict:
    from app.services.execution_version import prompt_for_executor, run_executor_version

    cfg = run.config_json or {}
    executor_version = run_executor_version(cfg)
    prompt_version = prompt_for_executor(executor_version, frozen_prompt=run.prompt_version)
    bidder_ids = list(dict.fromkeys(int(x) for x in cfg.get("bidder_ids", [])))
    bidders = list(s.execute(select(Bidder).where(Bidder.id.in_(bidder_ids)).order_by(Bidder.id)).scalars())
    if len(bidders) != len(bidder_ids) or any(b.project_id != run.project_id for b in bidders):
        raise ValidationFailed("参评投标人已变化，请新建评分运行")
    docs = list(s.execute(select(Document).where(
        Document.project_id == run.project_id,
        (Document.bidder_id.in_(bidder_ids)) | (Document.bidder_id.is_(None)),
    ).order_by(Document.id)).scalars())
    pkg = s.get(RulePackage, run.package_id)
    if pkg is None:
        raise NotFound("规则包不存在")
    known = {c.code: c.id for c in s.execute(select(Criterion).where(Criterion.package_id == pkg.id)).scalars()}
    frozen = []
    for c in criteria:
        if c.get("status") == "retired":
            continue
        item = deepcopy(c)
        item["id"] = item.get("id") or item.get("criterion_id") or known.get(item.get("code"))
        if item["id"] is None:
            raise ValidationFailed(f"评分项 {item.get('code')} 无法绑定冻结条款")
        frozen.append(item)
    return _json({
        "version": 1,
        "bidders": [{"id": b.id, "name": b.name, "unified_code": b.unified_code,
                     "bid_price": b.bid_price, "price_status": b.price_status,
                     "bid_price_upper": b.bid_price_upper, "price_breakdown_json": b.price_breakdown_json}
                    for b in bidders],
        "documents": [{"id": d.id, "sha256": d.sha256, "file_sha256": _file_hash(d.stored_path),
                       "stored_path": d.stored_path, "kind": d.kind, "bidder_id": d.bidder_id,
                       "data_class": d.data_class, "parse_status": d.parse_status,
                       "page_count": d.page_count,
                       "parse_warnings_sha256": hashlib.sha256(json.dumps(
                           _json(d.warnings_json), sort_keys=True, ensure_ascii=False,
                       ).encode("utf-8")).hexdigest(),
                       "parsed_pages_sha256": hashlib.sha256(json.dumps([
                           [p.page_no, p.text, p.source, p.has_text_layer]
                           for p in s.execute(select(DocumentPage).where(DocumentPage.document_id == d.id)
                                              .order_by(DocumentPage.page_no)).scalars()
                       ], ensure_ascii=False).encode("utf-8")).hexdigest()}
                      for d in docs],
        "criteria": frozen,
        "package": {"id": pkg.id, "version": pkg.version, "content_hash": pkg.content_hash,
                    "snapshot_sha256": hashlib.sha256(json.dumps(
                        _json(pkg.snapshot_json), sort_keys=True, ensure_ascii=False,
                    ).encode("utf-8")).hexdigest()},
        "stages": run.stages_json or ["objective", "price", "subjective"],
        "provider": provider or run.llm_provider or config.llm_provider(),
        "model": model or run.llm_model or config.llm_model(),
        "prompt_version": prompt_version,
        "scoring_config": _scoring_config(prompt_version=prompt_version),
        **({"executor_version": executor_version} if executor_version != 1 else {}),
    })


def initialize_run(run_id: int, criteria: list[dict], *, provider=None, model=None,
                   excluded_criteria: list[dict] | None = None) -> dict:
    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        if run is None:
            raise NotFound("评分运行不存在")
        old = (run.config_json or {}).get("automation_manifest")
        candidate = _inputs(s, run, criteria, provider, model)
        if old:
            if old != candidate:
                raise Conflict("运行输入或评审配置已变化，请新建评分运行")
            if excluded_criteria is not None:
                run.config_json = {**(run.config_json or {}),
                                   "excluded_criteria": _json(excluded_criteria)}
                s.add(run)
            return deepcopy(old)
        run.llm_provider = candidate["provider"]
        run.llm_model = candidate["model"]
        run.prompt_version = candidate["prompt_version"]
        run.config_json = {**(run.config_json or {}), "automation_version": 1,
                           "automation_manifest": candidate}
        if excluded_criteria is not None:
            run.config_json["excluded_criteria"] = _json(excluded_criteria)
        s.add(run)
        return deepcopy(candidate)


def _valid_cell(score: ScoreRecord, criterion: dict, citations: list[EvidenceCitation]) -> bool:
    meta = (score.ai_raw_json or {}).get("auto_review", {})
    if meta.get("state") != "completed" or score.review_status != "auto_confirmed":
        return False
    if not (score.ai_reasoning or meta.get("reasoning")):
        return False
    if score.machine_status in ("need_human", "conflict_flagged", "execution_failed", "failed"):
        return False
    basis = meta.get("basis")
    for warning in score.warnings_json or []:
        if isinstance(warning, dict) and (warning.get("fallback_score") or warning.get("unresolved")):
            # A closure fallback is a settled provisional score, not an
            # unresolved evidence claim.  It owns its own basis branch below.
            if not (basis == "fallback" and warning.get("fallback_score")):
                return False
    lightweight = basis == "lightweight"
    scoring = criterion.get("score_type") not in ("veto", "match")
    if lightweight and scoring:
        return False
    if not scoring and score.machine_status not in (
        "pass", "fail", "unverifiable",
        *({"concern", "not_deeply_reviewed"} if lightweight or basis == "fallback" else ()),
    ):
        return False
    if any((score.ai_raw_json or {}).get(k) for k in ("fallback_score", "unresolved", "missing_evidence")):
        return False
    if scoring:
        try:
            value = Decimal(str(score.final_score))
            maximum = Decimal(str(criterion.get("max_score")))
        except (InvalidOperation, TypeError, ValueError):
            return False
        if not value.is_finite() or not maximum.is_finite() or not Decimal(0) <= value <= maximum:
            return False
    if score.machine_status == "unverifiable":
        from app.engine.auto_review import is_external_requirement
        return (not scoring and basis == "external" and is_external_requirement(criterion)
                and bool(meta.get("external_requirement"))
                and meta.get("coverage_complete") is True
                and bool(score.ai_reasoning or meta.get("reasoning")))
    if basis == "external":
        return False
    if basis == "fallback":
        # Closure policy: the cell is settled with an explicit provisional
        # score (or a non-scoring concern conclusion).  The fallback payload
        # is mandatory so report/UI can label it and a later run can refresh.
        fallback = meta.get("fallback")
        if not isinstance(fallback, dict) or not fallback.get("kind"):
            return False
        if scoring:
            return bool(score.ai_reasoning or meta.get("reasoning"))
        return score.machine_status in ("concern", "not_deeply_reviewed", "unverifiable") and bool(
            score.ai_reasoning or meta.get("reasoning"))
    if basis == "model":
        # Direct subjective scoring: a finite in-range score with reasoning is
        # enough; citations are optional and recorded when they verify.
        return scoring and bool(score.ai_reasoning or meta.get("reasoning"))
    if basis == "lightweight":
        light = meta.get("lightweight") or {}
        return (isinstance(light, dict)
                and light.get("schema_version") == 1
                and light.get("quick_state") in ("completed", "skipped", "failed")
                and bool(score.ai_reasoning or meta.get("reasoning")))
    if basis == "visual":
        visuals = meta.get("visual_evidence") or []
        return bool(visuals) and all(
            isinstance(v, dict) and v.get("document_id") and v.get("page_no")
            and (v.get("observation") or v.get("quote")) for v in visuals
        )
    if basis == "evidence":
        return any(c.verified and c.document_id and c.page_no and c.quote.strip() for c in citations)
    if basis == "deterministic":
        return bool(meta.get("calculation") or meta.get("facts") or meta.get("reasoning")
                    or score.ai_reasoning)
    if basis == "absence":
        return bool(meta.get("confirmed_absent") and meta.get("coverage_complete"))
    return False


def _state(s, run: EvaluationRun) -> tuple[dict | None, set[tuple[int, int]]]:
    if not is_managed(run):
        return None, set()
    cfg = run.config_json or {}
    manifest = cfg.get("automation_manifest") or {}
    criteria = {int(c["id"]): c for c in manifest.get("criteria", [])}
    expected = {(int(b["id"]), cid) for b in manifest.get("bidders", []) for cid in criteria}
    rows = list(s.execute(select(ScoreRecord).where(ScoreRecord.run_id == run.id)).scalars())
    citations = list(s.execute(select(EvidenceCitation).where(
        EvidenceCitation.score_record_id.in_([x.id for x in rows] or [-1]))).scalars())
    by_score = {}
    for c in citations:
        by_score.setdefault(c.score_record_id, []).append(c)
    completed = set()
    failed = 0
    fallback = 0
    latest_error = None
    for row in rows:
        key = (row.bidder_id, row.criterion_id)
        if key not in expected:
            continue
        if _valid_cell(row, criteria[row.criterion_id], by_score.get(row.id, [])):
            completed.add(key)
            _auto_meta = (row.ai_raw_json or {}).get("auto_review") or {}
            if (_auto_meta.get("basis") == "fallback"
                    and ((_auto_meta.get("fallback") or {}).get("kind")) != "duplicate_summary"):
                fallback += 1
        elif ((row.ai_raw_json or {}).get("auto_review") or {}).get("state") == "failed":
            failed += 1
            latest_error = ((row.ai_raw_json or {}).get("auto_review") or {}).get("error")
    remaining = len(expected - completed)
    skip_errors = failure_policy(run) == "continue"
    skipped = failed if skip_errors else 0
    data = {
        "expected": len(expected), "completed": len(completed), "failed": failed,
        "fallback": fallback,
        "remaining": remaining, "resumable": run.status == "failed" and bool(manifest),
        "last_error": cfg.get("error") or latest_error,
        "complete": bool(expected) and remaining == 0,
        "is_final": run.status == "succeeded" and bool(expected) and remaining == 0,
        "failure_policy": failure_policy(run), "skipped": skipped,
        "processed": len(completed) + skipped, "pending": remaining - skipped,
        "execution_finished": run.status in ("succeeded", "partial", "failed", "cancelled"),
        "result_available": run.status in ("succeeded", "partial"),
    }
    return data, completed


def progress(run_id: int, session=None) -> dict | None:
    if session is None:
        with session_scope() as s:
            return progress(run_id, session=s)
    run = session.get(EvaluationRun, run_id)
    if run is None:
        raise NotFound("评分运行不存在")
    state = _state(session, run)[0]
    if state is not None:
        state["execution_progress"] = execution_progress(run_id, session=session)
    return state


def execution_progress(run_id: int, session=None) -> dict | None:
    """Collection activity is separate from durable bidder/criterion completion."""
    if session is None:
        with session_scope() as s:
            return execution_progress(run_id, session=s)
    run = session.get(EvaluationRun, run_id)
    if run is None or not is_managed(run):
        return None
    from app.services.execution_version import run_executor_version

    collections = list(session.execute(select(ReviewCollectionSet).where(
        ReviewCollectionSet.run_id == run_id,
    )).scalars())
    activity_sets = [item for item in collections if (item.plan_json or {}).get("kind") == "execution-metrics"]
    collections = [item for item in collections if (item.plan_json or {}).get("kind") != "execution-metrics"]
    ids = [item.id for item in collections]
    batches = list(session.execute(select(ReviewCollectionBatch).where(
        ReviewCollectionBatch.collection_id.in_(ids or [-1]),
    )).scalars())
    final_scores = session.execute(select(ScoreRecord).where(
        ScoreRecord.run_id == run_id, ScoreRecord.review_status == "auto_confirmed",
        ScoreRecord.ai_raw_json["auto_review"]["state"].as_string() == "completed",
    )).scalars().all()
    last = max([item.completed_at for item in batches if item.completed_at]
               + [item.updated_at for item in final_scores if item.updated_at], default=None)
    metric_names = ("cache_hits", "reused_batches", "queue_wait_ms", "request_ms", "ocr_ms", "render_ms",
                    "http_attempts", "http_completed", "http_failed", "tokens_in", "tokens_out",
                    "ocr_local_ms", "ocr_network_ms", "pdf_worker_ms", "pdf_queue_wait_ms", "pdf_prepare_ms",
                    "pdf_tasks", "pdf_failed")
    metrics = {key: sum(float((item.metrics_json or {}).get(key, 0) or 0)
                        for item in batches if isinstance((item.metrics_json or {}).get(key, 0), (int, float)))
               for key in metric_names}
    active = run.status in ("queued", "running")
    failed = [item for item in batches if item.state == "failed"]
    by_collection = {item.id: item for item in collections}
    batches_by_key = {(item.collection_id, item.batch_key): item for item in batches}
    pages_total, pages_completed = set(), set()
    unit_lookup, batch_indices = {}, {}
    for collection in collections:
        plan = collection.plan_json or {}
        page_batches = {}
        for index, (key, units) in enumerate(zip(plan.get("batch_keys", []), plan.get("units", []))):
            batch_indices[collection.id, key] = index
            for unit in units:
                unit_lookup[collection.id, unit.get("unit_id")] = unit
                page = (unit.get("document_id"), unit.get("page_no"))
                pages_total.add(page)
                page_batches.setdefault(page, set()).add(key)
        for page, keys in page_batches.items():
            if all((batches_by_key.get((collection.id, key)) is not None
                    and batches_by_key[collection.id, key].state == "completed") for key in keys):
                pages_completed.add(page)
    document_ids = {page[0] for page in pages_total if isinstance(page[0], int)}
    names = dict(session.execute(select(Document.id, Document.original_filename).where(
        Document.id.in_(document_ids or [-1]))).all())
    failures = []
    for batch in failed:
        collection = by_collection[batch.collection_id]
        documents = {}
        for unit_id in batch.unit_ids_json or []:
            unit = unit_lookup.get((collection.id, unit_id))
            if unit is not None:
                documents.setdefault(unit["document_id"], set()).add(unit["page_no"])
        error = batch.error_json if isinstance(batch.error_json, dict) else {}
        failures.append({
            "collection_id": collection.id, "batch_key": batch.batch_key,
            "batch_index": batch_indices.get((collection.id, batch.batch_key)),
            "criterion_id": collection.criterion_id, "bidder_id": collection.bidder_id,
            "documents": [{"document_id": document_id, "filename": names.get(document_id),
                           "page_numbers": sorted(numbers)} for document_id, numbers in documents.items()],
            "code": str(error.get("code") or "batch_failed"),
            "message": str(error.get("message") or "材料采集失败"),
        })
    queued_requests = sum(max(0, int((item.metrics_json or {}).get("queued_requests", 0) or 0))
                          for item in batches if item.state == "running") if active else 0
    inflight_requests = sum(max(0, int((item.metrics_json or {}).get("inflight_requests", 0) or 0))
                            for item in batches if item.state == "running") if active else 0
    if activity_sets:
        run_metrics = (max(activity_sets, key=lambda item: item.generation).metrics_json or {})
        queued_requests = max(0, int(run_metrics.get("queued_requests", 0) or 0)) if active else 0
        inflight_requests = max(0, int(run_metrics.get("inflight_requests", 0) or 0)) if active else 0
        for key in ("cache_hits", "queue_wait_ms", "request_ms", "http_attempts", "http_completed",
                    "http_failed", "tokens_in", "tokens_out", "ocr_ms", "render_ms", "ocr_local_ms",
                    "ocr_network_ms", "pdf_worker_ms", "pdf_queue_wait_ms", "pdf_prepare_ms", "pdf_tasks", "pdf_failed"):
            metrics[key] = max(0, int(run_metrics.get(key, 0) or 0))
    return {
        "executor_version": run_executor_version(run),
        "qualification_mode": (run.config_json or {}).get("qualification_mode", "full"),
        "count_deterministic_mode": (run.config_json or {}).get("count_deterministic_mode", "off"),
        "phase": "partial" if run.status == "partial" else "failed" if run.status == "failed" else "cancelled" if run.status == "cancelled" else "completed" if run.status == "succeeded"
                 else "synthesizing" if collections and all(item.state == "ready" for item in collections)
                 else "collecting" if batches and any(item.state in ("pending", "running") for item in batches)
                 else "reviewing" if active else "preparing",
        "collections_total": len(collections),
        "collections_ready": sum(item.state == "ready" for item in collections),
        "batches_total": len(batches),
        "batches_completed": sum(item.state == "completed" for item in batches),
        "batches_running": sum(item.state == "running" for item in batches) if active else 0,
        "batches_pending": sum(item.state == "pending" for item in batches) if active else 0,
        "batches_failed": len(failed),
        "requests_queued": queued_requests,
        "requests_inflight": inflight_requests,
        "request_scope": "run" if activity_sets else "collection",
        "read_units_total": sum(len(item.unit_ids_json or []) for item in batches),
        "read_units_completed": sum(len(item.unit_ids_json or []) for item in batches if item.state == "completed"),
        "read_pages_total": len(pages_total),
        "read_pages_completed": len(pages_completed),
        "failures": failures,
        "last_progress_at": last.isoformat() + "Z" if last else None,
        "metrics": metrics,
    }


def completed_keys(run_id: int) -> set[tuple[int, int]]:
    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        if run is None:
            raise NotFound("评分运行不存在")
        return _state(s, run)[1]


def is_cell_completed(run_id: int, bidder_id: int, criterion_id: int) -> bool:
    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        if run is None:
            raise NotFound("评分运行不存在")
        if not is_managed(run):
            return False
        manifest = (run.config_json or {}).get("automation_manifest") or {}
        if bidder_id not in {int(b["id"]) for b in manifest.get("bidders", [])}:
            return False
        criterion = next((c for c in manifest.get("criteria", []) if int(c["id"]) == criterion_id), None)
        if criterion is None:
            return False
        row = s.execute(select(ScoreRecord).where(
            ScoreRecord.run_id == run_id, ScoreRecord.bidder_id == bidder_id,
            ScoreRecord.criterion_id == criterion_id,
        )).scalar_one_or_none()
        if row is None:
            return False
        citations = s.execute(select(EvidenceCitation).where(
            EvidenceCitation.score_record_id == row.id,
        )).scalars().all()
        return _valid_cell(row, criterion, citations)


def finish_run(run_id: int) -> dict:
    error = None
    with session_scope() as s:
        # Take SQLite's write lock before reading inputs so an API upload,
        # reparse or quote update cannot interleave with the final verification.
        s.execute(update(EvaluationRun).where(EvaluationRun.id == run_id)
                  .values(status=EvaluationRun.status))
        run = s.get(EvaluationRun, run_id)
        if run is None:
            raise NotFound("评分运行不存在")
        state, _ = _state(s, run)
        if state is None:
            raise Conflict("历史运行不支持自动完成校验，请新建运行")
        manifest = (run.config_json or {}).get("automation_manifest")
        try:
            if not manifest:
                raise ValidationFailed("运行缺少冻结输入清单")
            current = _inputs(s, run, manifest["criteria"], manifest.get("provider"), manifest.get("model"))
            if current != manifest:
                error = "运行输入或评审配置已变化，请新建评分运行；本次结果不得发布为正式结论"
        except Exception as exc:
            error = f"完成前无法验证冻结输入，请新建评分运行：{exc}"
        run.finished_at = utcnow()
        if state["complete"] and error is None:
            run.status = "succeeded"
            run.config_json = {
                k: v for k, v in (run.config_json or {}).items() if k != "error"
            }
            if state.get("fallback"):
                run.config_json["fallback_count"] = state["fallback"]
        elif error is None and failure_policy(run) == "continue" and state["expected"] > 0 and state["pending"] == 0:
            run.status = "partial"
            run.config_json = {**(run.config_json or {}),
                               "error": f"评审已结束：已评分 {state['completed']} 项，故障跳过 {state['skipped']} 项"}
        else:
            error = error or (run.config_json or {}).get("error") or f"自动评审尚有 {state['remaining']} 项未完成"
            run.status = "failed"
            run.config_json = {**(run.config_json or {}), "error": error}
        s.add(run)
    if error:
        raise ValidationFailed(error)
    return progress(run_id)


def resume_run(run_id: int, actor: str) -> tuple[int, int]:
    # A conditional write takes SQLite's write lock before inspecting jobs.  It
    # serializes competing resume requests across processes as well as threads.
    with session_scope() as s:
        s.execute(update(EvaluationRun).where(EvaluationRun.id == run_id)
                  .values(status=EvaluationRun.status))
        run = s.get(EvaluationRun, run_id)
        if run is None:
            raise NotFound("评分运行不存在")
        if not is_managed(run) or not (run.config_json or {}).get("automation_manifest"):
            raise Conflict("历史运行没有冻结输入，请新建自动评审运行")
        active = s.execute(select(Job).where(
            Job.kind == "evaluate", Job.status.in_(("queued", "running")),
            Job.payload_json["run_id"].as_integer() == run_id,
        ).order_by(Job.id.desc()).limit(1)).scalar_one_or_none()
        if active is not None:
            return run_id, active.id
        if run.status in ("succeeded", "partial") and int((run.config_json or {}).get("resume_count", 0)) > 0:
            resumed_job = s.execute(select(Job).where(
                Job.kind == "evaluate", Job.payload_json["run_id"].as_integer() == run_id,
                Job.payload_json["resume"].as_boolean().is_(True),
            ).order_by(Job.id.desc()).limit(1)).scalar_one_or_none()
            if resumed_job is not None:
                # A delayed HTTP retry may arrive after the resumed job has
                # finished. Return the original receipt without another job.
                return run_id, resumed_job.id
        if run.status != "failed":
            raise Conflict("仅失败的自动评审运行可以续评")
        manifest = (run.config_json or {})["automation_manifest"]
        current = _inputs(s, run, manifest["criteria"], manifest.get("provider"), manifest.get("model"))
        manifest = _bind_unused_capabilities(s, run, manifest, current, actor)
        if current != manifest:
            raise Conflict("运行输入或评审配置已变化，请新建评分运行")
        job = Job(kind="evaluate", project_id=run.project_id, status="queued", payload_json={
            "run_id": run.id, "provider": manifest.get("provider"), "model": manifest.get("model"),
            "actor": actor, "resume": True,
        })
        s.add(job)
        run.status = "queued"
        run.finished_at = None
        run.config_json = {**(run.config_json or {}),
                           "resume_count": int((run.config_json or {}).get("resume_count", 0)) + 1}
        s.add(run)
        s.flush()
        return run.id, job.id


def _bind_unused_capabilities(s, run, manifest: dict, current: dict, actor: str) -> dict:
    """An unavailable, never-used capability may be configured for the first time.

    Completed decisions remain pinned.  This is not a model-switch escape hatch:
    already configured identities and any unrelated input changes still reject.
    """
    rows = s.execute(select(ScoreRecord).where(ScoreRecord.run_id == run.id)).scalars().all()
    used = {
        meta.get("basis") for row in rows
        if (meta := ((row.ai_raw_json or {}).get("auto_review") or {})).get("state") == "completed"
    }
    rebound = deepcopy(manifest)
    old = rebound.get("scoring_config", {})
    new = current.get("scoring_config", {})
    changes = {}
    if old.get("vision_model") is None and new.get("vision_model") and "visual" not in used:
        for key in ("vision_model", "vision_base_url"):
            old[key] = new.get(key)
            changes[key] = new.get(key)
    main_binding = False
    if not used.intersection({"evidence", "visual"}):
        for key in ("provider", "model"):
            if rebound.get(key) is None and current.get(key):
                rebound[key] = current[key]
                changes[key] = current[key]
                main_binding = True
        if main_binding:
            old["llm_base_url"] = new.get("llm_base_url")
            changes["llm_base_url"] = new.get("llm_base_url")
            if old.get("vision_model") is None and "visual" not in used:
                old["vision_base_url"] = new.get("vision_base_url")
                changes["vision_base_url"] = new.get("vision_base_url")
    if not changes:
        return manifest
    # Do not save an allowed binding if a quote/document/rule or another setting
    # drifted at the same time. The surrounding transaction will roll back.
    if rebound != current:
        return manifest
    run.llm_provider = rebound.get("provider")
    run.llm_model = rebound.get("model")
    run.config_json = {**(run.config_json or {}), "automation_manifest": rebound}
    s.add(AuditLog(project_id=run.project_id, actor=actor, action="run.bind_unused_model",
                   subject_type="run", subject_id=str(run.id), detail_json=changes))
    return rebound
