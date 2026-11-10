"""Validated review work checkpoints, separate from published score records.

The caller holds the run OS lock. HTTP and validation never hold a database
transaction. Completed batches are reusable only for their exact semantic input.
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from sqlalchemy import String, cast, select, update
from sqlalchemy.orm import defer

from app.db.session import session_scope
from app.llm.base import LLMError

_RUNTIME: ContextVar[dict | None] = ContextVar("review_work_runtime", default=None)
_FLIGHTS_LOCK = threading.Lock()
_FLIGHTS: dict[tuple, threading.Lock] = {}
VALIDATOR_VERSION = "2026-11-06.sources-v2.1"


def stable_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str, separators=(",", ":")).encode()).hexdigest()


def semantic_value(value):
    """Audit correlation IDs are not material; error feedback remains material."""
    if isinstance(value, dict):
        return {str(k): semantic_value(v) for k, v in value.items()
                if k not in ("execution_attempt", "_review_runtime")}
    if isinstance(value, (list, tuple)):
        return [semantic_value(v) for v in value]
    return value


def source_unit(page: dict) -> dict:
    text = str(page.get("text") or "")
    start = int(page.get("_text_start", 0))
    unit = {"document_id": int(page["document_id"]), "page_no": int(page["page_no"]),
            "bidder_id": page.get("bidder_id"), "file_sha256": page.get("sha256"),
            "text_start": start, "text_end": int(page.get("_text_end", start + len(text))),
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "image_sha256": page.get("image_sha256"),
            "image_payload_sha256": (hashlib.sha256(str(page["image_url"]).encode()).hexdigest()
                                     if page.get("image_url") else None),
            "known_blank": bool(page.get("known_blank")),
            "unreadable": bool(page.get("unreadable") or page.get("requires_vision")),
            "source": page.get("source"), "validator_version": VALIDATOR_VERSION}
    unit["unit_id"] = stable_hash(unit)
    return unit


def verify_text_coverage(pages: list[dict], batches: list[list[dict]]) -> None:
    """A page is covered only when every original character has been assigned."""
    expected = {(int(p["document_id"]), int(p["page_no"])): str(p.get("text") or "") for p in pages}
    if len(expected) != len(pages):
        raise LLMError("采集计划原页标识重复")
    intervals: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for batch in batches:
        for part in batch:
            key = int(part["document_id"]), int(part["page_no"])
            if key not in expected:
                raise LLMError("采集计划引用了范围外的原页")
            start = int(part.get("_text_start", 0))
            end = int(part.get("_text_end", start + len(str(part.get("text") or ""))))
            if (start < 0 or end < start or end > len(expected[key])
                    or (start == end and expected[key])
                    or expected[key][start:end] != str(part.get("text") or "")):
                raise LLMError("采集计划切片与原文区间不一致")
            intervals.setdefault(key, []).append((start, end))
    for key, text in expected.items():
        if not text and intervals.get(key) != [(0, 0)]:
            raise LLMError("采集计划空白页未唯一覆盖")
        cursor = 0
        for start, end in sorted(intervals.get(key, [])):
            if start != cursor:
                raise LLMError("采集计划存在重复或遗漏的原页文字区间")
            cursor = end
        if key not in intervals or cursor != len(text):
            raise LLMError("采集计划未覆盖完整原页文字")


def recover_interrupted_batches(run_id: int, generation: int) -> None:
    from app.db.models import ReviewCollectionBatch, ReviewCollectionSet, utcnow

    with session_scope() as s:
        collections = s.execute(select(ReviewCollectionSet).where(
            ReviewCollectionSet.run_id == run_id)).scalars().all()
        if any(row.generation > generation for row in collections):
            raise LLMError("采集执行代次已变化，拒绝旧代次恢复")
        for collection in collections:
            collection.generation = generation
            rows = s.execute(select(ReviewCollectionBatch).where(
                ReviewCollectionBatch.collection_id == collection.id)).scalars().all()
            for row in rows:
                if row.state == "running":
                    row.state, row.attempt_token, row.generation = "pending", None, generation
                    row.error_json = {"code": "interrupted_before_commit"}
                    row.completed_at, row.updated_at = None, utcnow()
                row.metrics_json = {**(row.metrics_json or {}), "queued_requests": 0, "inflight_requests": 0}
            _refresh_collection(collection, rows)


def _refresh_collection(collection, batches) -> None:
    """Only a validated ready transition assigns collection completion time."""
    from app.db.models import utcnow

    states = [row.state for row in batches]
    if "failed" in states:
        collection.state = "failed"
        collection.error_json = {"code": "batch_failed", "failed_batches": states.count("failed")}
    elif "running" in states:
        collection.state, collection.error_json = "running", None
    elif states and all(state == "completed" for state in states):
        if collection.state != "ready":
            collection.state = "completed"
        collection.error_json = None
    else:
        collection.state, collection.error_json = "pending", None
    if collection.state != "ready":
        collection.completed_at = None
    collection.updated_at = utcnow()


def _guard_generation(session, collection_id: int, generation: int):
    """Take the transaction's write lock before inspecting a current attempt."""
    from app.db.models import ReviewCollectionSet

    changed = session.execute(update(ReviewCollectionSet).where(
        ReviewCollectionSet.id == collection_id, ReviewCollectionSet.generation == generation,
    ).values(generation=generation))
    if changed.rowcount != 1:
        raise LLMError("采集执行代次已变化，拒绝迟到操作")
    # The immutable plan may contain megabytes of source units. Most guarded
    # writes only need execution metadata; load the plan explicitly when used.
    return session.get(ReviewCollectionSet, collection_id,
                       options=[defer(ReviewCollectionSet.plan_json)])


def _metric_observer(metrics: dict, lock, on_change=None):
    def observe(event):
        def add(name, value=1):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metrics[name] = metrics.get(name, 0) + max(0, int(value))
        with lock:
            if event.get("event") == "queued":
                add("queued_requests")
            elif event.get("event") == "queue_cancelled":
                metrics["queued_requests"] = max(0, metrics.get("queued_requests", 0) - 1)
            elif event.get("event") == "started":
                metrics["queued_requests"] = max(0, metrics.get("queued_requests", 0) - 1)
                add("inflight_requests")
                add("http_attempts")
                add("queue_wait_ms", event.get("queue_ms"))
            elif event.get("event") == "finished":
                metrics["inflight_requests"] = max(0, metrics.get("inflight_requests", 0) - 1)
                add("http_failed" if event.get("error_type") else "http_completed")
                add("request_ms", event.get("service_ms"))
                if event.get("provider") == "remote_paddle":
                    add("ocr_network_ms", event.get("service_ms"))
                    add("ocr_ms", event.get("service_ms"))
                add("tokens_in", event.get("tokens_in"))
                add("tokens_out", event.get("tokens_out"))
            elif event.get("event") == "cache_hit" and event.get("cache_kind") in {"memory", "disk"}:
                add("cache_hits")
            elif event.get("event") == "pdf_task_finished":
                add("pdf_tasks")
                if event.get("error_type"):
                    add("pdf_failed")
                for name in ("pdf_worker_ms", "pdf_queue_wait_ms", "pdf_prepare_ms", "render_ms", "ocr_local_ms"):
                    add(name, event.get(name))
                add("ocr_ms", event.get("ocr_local_ms"))
            else:
                return
            if on_change is not None:
                on_change(dict(metrics))
    return observe


def _merge_metrics(previous, current):
    result = dict(previous or {})
    for key, value in current.items():
        result[key] = value if key in {"queued_requests", "inflight_requests"} else int(result.get(key, 0)) + value
    return result


@contextmanager
def review_runtime(run_id: int, bidder_id: int | None, criterion_id: int,
                   generation: int, manifest: dict):
    token = _RUNTIME.set({"run_id": run_id, "bidder_id": bidder_id,
                          "criterion_id": criterion_id, "generation": generation,
                          "manifest": manifest})
    try:
        yield
    finally:
        _RUNTIME.reset(token)


def current_review_runtime() -> dict | None:
    return _RUNTIME.get()


def run_activity_observer(run_id: int, generation: int):
    """One observer per executor; nested batch scopes forward each event once."""
    from app.db.models import EvaluationRun, ReviewCollectionSet, utcnow

    lock = threading.Lock()
    with session_scope() as session:
        run = session.get(EvaluationRun, run_id)
        manifest = (run.config_json or {}).get("automation_manifest") or {}
        criterion_id = min(int(item["id"]) for item in manifest.get("criteria", []))
        signature = stable_hash({"run_id": run_id, "kind": "execution-metrics"})
        collection = session.execute(select(ReviewCollectionSet).where(
            ReviewCollectionSet.run_id == run_id, ReviewCollectionSet.group_key == "execution-metrics",
            ReviewCollectionSet.input_signature == signature)).scalar_one_or_none()
        if collection is None:
            collection = ReviewCollectionSet(run_id=run_id, group_key="execution-metrics",
                criterion_id=criterion_id, input_signature=signature,
                plan_json={"kind": "execution-metrics", "schema_version": 1}, state="running",
                generation=generation, metrics_json={})
            session.add(collection)
            session.flush()
        elif collection.generation > generation:
            raise LLMError("运行活动代次已变化，拒绝旧代次观察器")
        collection.generation = generation
        collection.metrics_json = {**(collection.metrics_json or {}), "queued_requests": 0,
                                   "inflight_requests": 0, "queued_ids": [], "inflight_ids": []}
        collection_id = collection.id

    def observer(event):
        if event.get("event") not in {"queued", "queue_cancelled", "started", "finished", "cache_hit", "pdf_task_finished"}:
            return
        event_id = event.get("event_id")
        request_id = event.get("request_id")
        if not event_id or (event.get("event") not in {"cache_hit", "pdf_task_finished"} and not request_id):
            return
        dedup_key = f"{request_id}:{event['event']}" if request_id else event_id
        with lock, session_scope() as session:
            collection = session.get(ReviewCollectionSet, collection_id)
            if collection.generation != generation:
                return
            metrics = dict(collection.metrics_json or {})
            seen = list(metrics.pop("recent_event_ids", []))
            if dedup_key in seen:
                return
            queued = set(metrics.pop("queued_ids", []))
            inflight = set(metrics.pop("inflight_ids", []))
            name = event["event"]
            if name == "queued":
                queued.add(request_id)
            elif name == "queue_cancelled":
                queued.discard(request_id)
            elif name == "started":
                queued.discard(request_id)
                inflight.add(request_id)
            elif name == "finished":
                queued.discard(request_id)
                inflight.discard(request_id)
            # Reuse numeric HTTP accounting, but derive gauges from request IDs.
            _metric_observer(metrics, threading.Lock())(event)
            metrics.update(queued_requests=len(queued), inflight_requests=len(inflight),
                           queued_ids=sorted(queued), inflight_ids=sorted(inflight),
                           recent_event_ids=(seen + [dedup_key])[-512:])
            changed = session.execute(update(ReviewCollectionSet).where(
                ReviewCollectionSet.id == collection_id, ReviewCollectionSet.generation == generation,
            ).values(metrics_json=metrics, updated_at=utcnow()))
            if changed.rowcount != 1:
                return
    observer._run_activity_observer = True
    return observer


class CollectionCheckpoint:
    def __init__(self, rule: dict, batches: list[list[dict]], *, kind: str,
                 data_class: str, shared: bool = False, extra: dict | None = None):
        self.runtime = deepcopy(_RUNTIME.get())
        self.collection_id: int | None = None
        self._validated_plan_json: str | None = None
        self._source_batches: dict[int, list[dict]] = {}
        self.units = [[source_unit(p) for p in batch] for batch in batches]
        self.batch_keys = [stable_hash({"index": index, "units": units})
                           for index, units in enumerate(self.units)]
        self.input_data = semantic_value({
            "rule": rule, "units": self.units, "kind": kind, "data_class": data_class,
            "validator_version": VALIDATOR_VERSION, "extra": extra or {},
        })
        self.input_signature = stable_hash(self.input_data)
        if self.runtime is None:
            return
        from app.db.models import ReviewCollectionBatch, ReviewCollectionSet

        run_id = self.runtime["run_id"]
        target = "all" if shared else str(self.runtime["bidder_id"])
        group_key = f"{kind}:{self.runtime['criterion_id']}:{target}"
        manifest = self.runtime["manifest"]
        self.input_data["execution_identity"] = {
            k: manifest.get(k) for k in ("provider", "model", "prompt_version", "scoring_config", "executor_version")
        }
        # Persist exactly the JSON representation whose signature is checked on restart.
        self.input_data = json.loads(json.dumps(self.input_data, ensure_ascii=False, default=str))
        self.input_signature = stable_hash(self.input_data)
        self.plan = {"input_data": deepcopy(self.input_data), "units": deepcopy(self.units),
                     "kind": kind, "validator_version": VALIDATOR_VERSION,
                     "batch_keys": list(self.batch_keys)}
        with _FLIGHTS_LOCK:
            lock = _FLIGHTS.setdefault((run_id, group_key, self.input_signature), threading.Lock())
        with lock, session_scope() as s:
            row = s.execute(select(ReviewCollectionSet).where(
                ReviewCollectionSet.run_id == run_id, ReviewCollectionSet.group_key == group_key,
                ReviewCollectionSet.input_signature == self.input_signature,
            )).scalar_one_or_none()
            if row is None:
                row = ReviewCollectionSet(run_id=run_id, group_key=group_key,
                    criterion_id=self.runtime["criterion_id"],
                    bidder_id=None if shared else self.runtime["bidder_id"],
                    input_signature=self.input_signature,
                    plan_json=deepcopy(self.plan),
                    state="pending", generation=self.runtime["generation"])
                s.add(row)
                s.flush()
            else:
                self._validate_plan(row)
                if row.generation > self.runtime["generation"]:
                    raise LLMError("采集执行代次已变化，拒绝旧代次计划")
                if row.generation < self.runtime["generation"]:
                    row.generation = self.runtime["generation"]
                    interrupted = s.execute(select(ReviewCollectionBatch).where(
                        ReviewCollectionBatch.collection_id == row.id,
                        ReviewCollectionBatch.state == "running")).scalars().all()
                    for batch in interrupted:
                        batch.state, batch.attempt_token = "pending", None
                        batch.generation, batch.completed_at = self.runtime["generation"], None
                        batch.error_json = {"code": "interrupted_before_commit"}
                        batch.metrics_json = {**(batch.metrics_json or {}), "queued_requests": 0, "inflight_requests": 0}
                    _refresh_collection(row, s.execute(select(ReviewCollectionBatch).where(
                        ReviewCollectionBatch.collection_id == row.id)).scalars().all())
            self.collection_id = row.id
            self._validate_plan(row, session=s)
            unfinished = []
            for index, units in enumerate(self.units):
                key = self.batch_keys[index]
                existing = s.execute(select(ReviewCollectionBatch).where(
                    ReviewCollectionBatch.collection_id == row.id, ReviewCollectionBatch.batch_key == key,
                )).scalar_one_or_none()
                if existing is None:
                    unfinished.append(index)
                    s.add(ReviewCollectionBatch(collection_id=row.id, batch_key=key,
                        unit_ids_json=[u["unit_id"] for u in units], state="pending",
                        attempt_count=0, generation=self.runtime["generation"]))
                elif existing.unit_ids_json != [u["unit_id"] for u in units]:
                    raise LLMError("持久采集单元与冻结计划不一致")
                elif existing.state != "completed":
                    unfinished.append(index)
            if unfinished and kind in ("v1-text", "v1-vision"):
                self._find_source_batches(s, group_key, unfinished)

    def _find_source_batches(self, session, group_key, unfinished):
        """Recover unchanged independent batches after a lossless plan split.

        Every non-unit input, including the rule, model settings and previous
        feedback, must match. Original checkpoints are never rewritten.
        """
        from app.db.models import ReviewCollectionBatch, ReviewCollectionSet

        context = {key: value for key, value in self.input_data.items() if key != "units"}
        targets = {}
        for index in unfinished:
            targets.setdefault(stable_hash(self.units[index]), []).append(index)
        peers = session.execute(select(ReviewCollectionSet).where(
            ReviewCollectionSet.run_id == self.runtime["run_id"],
            ReviewCollectionSet.group_key == group_key,
            ReviewCollectionSet.id != self.collection_id,
            ReviewCollectionSet.generation <= self.runtime["generation"],
        ).order_by(ReviewCollectionSet.id)).scalars()
        for peer in peers:
            plan = peer.plan_json or {}
            if not isinstance(plan, dict):
                continue
            data = plan.get("input_data") or {}
            if not isinstance(data, dict):
                continue
            units, keys = plan.get("units", []), plan.get("batch_keys", [])
            if (plan.get("kind") != self.input_data["kind"]
                    or plan.get("validator_version") != VALIDATOR_VERSION
                    or {key: value for key, value in data.items() if key != "units"} != context
                    or data.get("units") != units or len(units) != len(keys)
                    or stable_hash(data) != peer.input_signature):
                continue
            matches = {}
            for source_index, source_units in enumerate(units):
                candidates = targets.get(stable_hash(source_units), [])
                expected_key = stable_hash({"index": source_index, "units": source_units})
                if keys[source_index] != expected_key:
                    continue
                for index in candidates:
                    if source_units == self.units[index]:
                        matches.setdefault(expected_key, []).append(index)
            if not matches:
                continue
            original_plan_json = session.execute(select(cast(ReviewCollectionSet.plan_json, String)).where(
                ReviewCollectionSet.id == peer.id)).scalar_one()
            batches = session.execute(select(ReviewCollectionBatch).where(
                ReviewCollectionBatch.collection_id == peer.id,
                ReviewCollectionBatch.state == "completed",
                ReviewCollectionBatch.batch_key.in_(matches),
            ).order_by(ReviewCollectionBatch.id)).scalars()
            for batch in batches:
                for index in matches[batch.batch_key]:
                    if batch.unit_ids_json != [unit["unit_id"] for unit in self.units[index]]:
                        continue
                    self._source_batches.setdefault(index, []).append({
                        "id": batch.id, "result": deepcopy(batch.result_json),
                        "collection_id": peer.id, "input_signature": peer.input_signature,
                        "plan_json": original_plan_json,
                    })

    def _validate_plan(self, row, *, session=None):
        if row.input_signature != self.input_signature:
            raise LLMError("持久采集计划与冻结输入不一致")
        saved_json = getattr(self, "_validated_plan_json", None)
        if session is not None and saved_json is not None:
            from app.db.models import ReviewCollectionSet

            # Compare the exact previously validated persisted bytes in SQLite.
            # This still detects edits, without decoding and hashing the entire
            # plan again for every already-completed batch during recovery.
            same = session.execute(select(ReviewCollectionSet.id).where(
                ReviewCollectionSet.id == row.id,
                ReviewCollectionSet.input_signature == self.input_signature,
                cast(ReviewCollectionSet.plan_json, String) == saved_json,
            )).scalar_one_or_none()
            if same is not None:
                return
        plan = row.plan_json or {}
        if (any(plan.get(key) != value for key, value in self.plan.items())
                or stable_hash(plan.get("input_data")) != row.input_signature
                or row.input_signature != self.input_signature):
            raise LLMError("持久采集计划与冻结输入不一致")
        if session is not None:
            from app.db.models import ReviewCollectionSet

            stored = session.execute(select(cast(ReviewCollectionSet.plan_json, String)).where(
                ReviewCollectionSet.id == row.id)).scalar_one()
            if json.loads(stored) != plan:
                raise LLMError("持久采集计划与冻结输入不一致")
            self._validated_plan_json = stored

    def execute(self, index: int, perform, validate):
        if self.collection_id is None:
            result = perform()
            validate(result)
            return result
        from app.db.models import ReviewCollectionBatch, ReviewCollectionSet, utcnow
        from app.llm.request_control import request_scope, run_activity_metrics_active
        from app.services.review_activity import report

        key = self.batch_keys[index]
        with _FLIGHTS_LOCK:
            lock = _FLIGHTS.setdefault((self.collection_id, key), threading.Lock())
        with lock:
            with session_scope() as s:
                collection = _guard_generation(s, self.collection_id, self.runtime["generation"])
                self._validate_plan(collection, session=s)
                row = s.execute(select(ReviewCollectionBatch).where(
                    ReviewCollectionBatch.collection_id == self.collection_id,
                    ReviewCollectionBatch.batch_key == key,
                )).scalar_one()
                cached = deepcopy(row.result_json) if row.state == "completed" else None
            if cached is not None:
                try:
                    validate(cached)
                except Exception as exc:
                    with session_scope() as s:
                        collection = _guard_generation(s, self.collection_id, self.runtime["generation"])
                        s.execute(update(ReviewCollectionBatch).where(
                            ReviewCollectionBatch.collection_id == self.collection_id,
                            ReviewCollectionBatch.batch_key == key,
                        ).values(state="failed", result_json=None,
                                 completed_at=None, attempt_token=None,
                                 error_json={"code": "invalid_checkpoint", "message": str(exc)[:500]}, updated_at=utcnow()))
                        _refresh_collection(collection, s.execute(select(ReviewCollectionBatch).where(
                            ReviewCollectionBatch.collection_id == self.collection_id)).scalars().all())
                else:
                    with session_scope() as s:
                        _guard_generation(s, self.collection_id, self.runtime["generation"])
                        row = s.execute(select(ReviewCollectionBatch).where(
                            ReviewCollectionBatch.collection_id == self.collection_id,
                            ReviewCollectionBatch.batch_key == key,
                        )).scalar_one()
                        row.metrics_json = {**(row.metrics_json or {}),
                            "reused_batches": int((row.metrics_json or {}).get("reused_batches", 0)) + 1}
                    report(f"复用已核验采集批次 {index + 1}/{len(self.batch_keys)}")
                    return cached
            for source in getattr(self, "_source_batches", {}).get(index, []):
                reused = deepcopy(source["result"])
                try:
                    validate(reused)
                except Exception:
                    # A prior result may be invalid under the current source
                    # validator. Leave it intact and execute only this batch.
                    continue
                with session_scope() as s:
                    collection = _guard_generation(s, self.collection_id, self.runtime["generation"])
                    self._validate_plan(collection, session=s)
                    original = s.get(ReviewCollectionBatch, source["id"])
                    if (original is None or original.state != "completed"
                            or original.result_json != source["result"]):
                        continue
                    source_unchanged = s.execute(select(ReviewCollectionSet.id).where(
                        ReviewCollectionSet.id == source["collection_id"],
                        ReviewCollectionSet.input_signature == source["input_signature"],
                        cast(ReviewCollectionSet.plan_json, String) == source["plan_json"],
                    )).scalar_one_or_none()
                    if source_unchanged is None:
                        continue
                    row = s.execute(select(ReviewCollectionBatch).where(
                        ReviewCollectionBatch.collection_id == self.collection_id,
                        ReviewCollectionBatch.batch_key == key,
                    )).scalar_one()
                    row.state, row.result_json, row.error_json = "completed", reused, None
                    row.attempt_token, row.generation = None, self.runtime["generation"]
                    row.completed_at, row.updated_at = utcnow(), utcnow()
                    row.metrics_json = {**(row.metrics_json or {}), "queued_requests": 0,
                        "inflight_requests": 0, "reused_batches": 1,
                        "reuse_provenance": {"source_batch_id": source["id"],
                            "source_collection_id": source["collection_id"],
                            "input_signature": source["input_signature"],
                            "result_sha256": stable_hash(source["result"])}}
                    _refresh_collection(collection, s.execute(select(ReviewCollectionBatch).where(
                        ReviewCollectionBatch.collection_id == self.collection_id)).scalars().all())
                report(f"复用原计划已核验采集批次 {index + 1}/{len(self.batch_keys)}")
                return reused
            token = uuid.uuid4().hex
            with session_scope() as s:
                collection = _guard_generation(s, self.collection_id, self.runtime["generation"])
                row = s.execute(select(ReviewCollectionBatch).where(
                    ReviewCollectionBatch.collection_id == self.collection_id,
                    ReviewCollectionBatch.batch_key == key,
                )).scalar_one()
                row.state, row.attempt_token = "running", token
                row.generation = self.runtime["generation"]
                row.attempt_count = (row.attempt_count or 0) + 1
                row.started_at, row.updated_at = utcnow(), utcnow()
                row.completed_at, row.error_json, row.result_json = None, None, None
                row.metrics_json = {**(row.metrics_json or {}), "queued_requests": 0, "inflight_requests": 0}
                baseline_metrics = dict(row.metrics_json)
                collection.state, collection.updated_at = "running", utcnow()
                collection.completed_at, collection.error_json = None, None
            metrics = {"queued_requests": 0, "inflight_requests": 0}

            def persist_metrics(snapshot):
                with session_scope() as s:
                    # Short metric writes never advance a completion timestamp.
                    s.execute(update(ReviewCollectionBatch).where(
                        ReviewCollectionBatch.collection_id == self.collection_id,
                        ReviewCollectionBatch.batch_key == key, ReviewCollectionBatch.state == "running",
                        ReviewCollectionBatch.attempt_token == token,
                        ReviewCollectionBatch.generation == self.runtime["generation"],
                        ReviewCollectionBatch.collection_id.in_(select(ReviewCollectionSet.id).where(
                            ReviewCollectionSet.generation == self.runtime["generation"])),
                    ).values(metrics_json=_merge_metrics(baseline_metrics, snapshot)))
            live_metrics = None if run_activity_metrics_active() else persist_metrics
            try:
                with request_scope(metadata={"run_id": self.runtime["run_id"],
                        "bidder_id": self.runtime["bidder_id"], "criterion_id": self.runtime["criterion_id"],
                        "batch_index": index}, on_event=_metric_observer(metrics, threading.Lock(), live_metrics)):
                    result = perform()
                    validate(result)
            except BaseException as exc:
                with session_scope() as s:
                    # A recovered execution owns all later writes, including errors.
                    collection = s.get(ReviewCollectionSet, self.collection_id)
                    if collection is not None and collection.generation == self.runtime["generation"]:
                        collection = _guard_generation(s, self.collection_id, self.runtime["generation"])
                        row = s.execute(select(ReviewCollectionBatch).where(
                            ReviewCollectionBatch.collection_id == self.collection_id,
                            ReviewCollectionBatch.batch_key == key, ReviewCollectionBatch.state == "running",
                            ReviewCollectionBatch.attempt_token == token,
                            ReviewCollectionBatch.generation == self.runtime["generation"],
                        )).scalar_one_or_none()
                        if row is not None:
                            row.state, row.completed_at, row.attempt_token = "failed", None, None
                            row.error_json = {"code": type(exc).__name__, "message": str(exc)[:500]}
                            history = getattr(exc, "validation_history", None)
                            if history:
                                row.error_json = {**row.error_json, "validation_history": deepcopy(history)}
                            row.metrics_json = {**_merge_metrics(baseline_metrics, metrics),
                                                "queued_requests": 0, "inflight_requests": 0}
                            _refresh_collection(collection, s.execute(select(ReviewCollectionBatch).where(
                                ReviewCollectionBatch.collection_id == self.collection_id)).scalars().all())
                raise
            with session_scope() as s:
                collection = _guard_generation(s, self.collection_id, self.runtime["generation"])
                row = s.execute(select(ReviewCollectionBatch).where(
                    ReviewCollectionBatch.collection_id == self.collection_id,
                    ReviewCollectionBatch.batch_key == key)).scalar_one()
                changed = s.execute(update(ReviewCollectionBatch).where(
                    ReviewCollectionBatch.collection_id == self.collection_id,
                    ReviewCollectionBatch.batch_key == key, ReviewCollectionBatch.state == "running",
                    ReviewCollectionBatch.attempt_token == token,
                    ReviewCollectionBatch.generation == self.runtime["generation"],
                ).values(state="completed", result_json=result, error_json=None,
                         attempt_token=None, metrics_json={**_merge_metrics(baseline_metrics, metrics),
                                                          "queued_requests": 0, "inflight_requests": 0},
                         completed_at=utcnow(), updated_at=utcnow()))
                if changed.rowcount != 1:
                    raise LLMError("批次执行代次已变化，拒绝迟到结果提交")
                _refresh_collection(collection, s.execute(select(ReviewCollectionBatch).where(
                    ReviewCollectionBatch.collection_id == self.collection_id)).scalars().all())
            return result

    def ready(self, *, resolution_collection_ids: list[int] | None = None,
              resolution_unit_map: dict[str, list[str]] | None = None):
        if self.collection_id is None:
            return
        from app.db.models import ReviewCollectionBatch, ReviewCollectionSet, utcnow
        with session_scope() as s:
            row = _guard_generation(s, self.collection_id, self.runtime["generation"])
            self._validate_plan(row)
            plan, batches = _completed_plan(s, row)
            if self.input_data["kind"].startswith("v2-"):
                ids = (resolution_collection_ids if resolution_collection_ids is not None
                       else plan.get("resolution_collection_ids", []))
                mapping = (resolution_unit_map if resolution_unit_map is not None
                           else plan.get("resolution_unit_map", {}))
                ids, mapping = _validate_resolutions(s, row, batches, ids, mapping)
                row.plan_json = {**plan, "resolution_collection_ids": ids,
                                 "resolution_unit_map": mapping}
            row.state, row.updated_at = "ready", utcnow()
            if row.completed_at is None:
                row.completed_at = utcnow()
            row.error_json = None


def _completed_plan(session, collection):
    from app.db.models import ReviewCollectionBatch

    plan = collection.plan_json or {}
    data = plan.get("input_data")
    if (not isinstance(data, dict) or stable_hash(data) != collection.input_signature
            or any(plan.get(key) != data.get(key) for key in ("units", "kind", "validator_version"))):
        raise LLMError("持久采集计划校验失败")
    units = plan.get("units")
    if not isinstance(units, list):
        raise LLMError("持久采集计划缺少原页单元")
    keys = [stable_hash({"index": index, "units": batch}) for index, batch in enumerate(units)]
    if plan.get("batch_keys") != keys:
        raise LLMError("持久采集批次与冻结计划不一致")
    for batch in units:
        for unit in batch:
            if unit.get("unit_id") != stable_hash({k: v for k, v in unit.items() if k != "unit_id"}):
                raise LLMError("持久原页单元签名不一致")
    batches = session.execute(select(ReviewCollectionBatch).where(
        ReviewCollectionBatch.collection_id == collection.id)).scalars().all()
    by_key = {batch.batch_key: batch for batch in batches}
    if (set(by_key) != set(keys) or any(batch.state != "completed" or batch.completed_at is None
            or not isinstance(batch.result_json, dict) for batch in batches)):
        raise LLMError("采集工作单元尚未完整完成")
    for key, batch_units in zip(keys, units):
        if by_key[key].unit_ids_json != [unit["unit_id"] for unit in batch_units]:
            raise LLMError("持久采集单元与冻结计划不一致")
    return plan, batches


def _page_identity(unit):
    return tuple(unit.get(key) for key in ("document_id", "page_no", "bidder_id", "file_sha256"))


def _validate_resolutions(session, collection, batches, ids, mapping):
    """Validate all supplementary links together before publishing collection readiness."""
    from app.db.models import ReviewCollectionSet

    if (not isinstance(ids, list) or any(type(value) is not int for value in ids)
            or len(ids) != len(set(ids)) or not isinstance(mapping, dict)):
        raise LLMError("补采完成关系格式不合法")
    plans, visiting = {}, {collection.id}
    root_plan = collection.plan_json
    identity = root_plan["input_data"].get("execution_identity")

    def visit(collection_id):
        if collection_id in visiting:
            raise LLMError("补采完成关系存在循环")
        if collection_id in plans:
            return
        row = session.get(ReviewCollectionSet, collection_id)
        if (row is None or row.run_id != collection.run_id or row.criterion_id != collection.criterion_id
                or row.state != "ready" or row.completed_at is None):
            raise LLMError("补采集合尚未就绪或不属于当前运行和条款")
        plan, _ = _completed_plan(session, row)
        if (plan.get("kind") != root_plan.get("kind")
                or plan["input_data"].get("execution_identity") != identity):
            raise LLMError("补采集合执行配置与冻结输入不一致")
        visiting.add(collection_id)
        for child in plan.get("resolution_collection_ids", []):
            visit(child)
        visiting.remove(collection_id)
        plans[collection_id] = plan

    for collection_id in ids:
        visit(collection_id)
    original = {unit["unit_id"]: unit for batch in root_plan["units"] for unit in batch}
    replacements = {unit["unit_id"]: unit for plan in plans.values()
                    for batch in plan["units"] for unit in batch}
    available = {**original, **replacements}
    edges: dict[str, set[str]] = {}
    for plan in plans.values():
        repair = plan["input_data"].get("extra", {}).get("repair")
        try:
            repair = json.loads(repair) if isinstance(repair, str) else repair
        except (TypeError, ValueError) as exc:
            raise LLMError("补采集合缺少有效来源请求") from exc
        if not isinstance(repair, list):
            raise LLMError("补采集合缺少来源请求")
        targets = [unit for batch in plan["units"] for unit in batch]
        for request in repair:
            if not isinstance(request, dict):
                raise LLMError("补采来源请求格式不合法")
            for old in request.get("source_unit_ids") or []:
                if old not in available:
                    continue
                edges.setdefault(old, set()).update(unit["unit_id"] for unit in targets
                    if _page_identity(unit) == _page_identity(available[old]))
    unresolved = []
    for batch in batches:
        requests = batch.result_json.get("unresolved", [])
        if not isinstance(requests, list):
            raise LLMError("共享采集未解决请求格式不合法")
        for request in requests:
            source_ids = request.get("source_unit_ids") if isinstance(request, dict) else None
            if not isinstance(source_ids, list) or not source_ids or any(key not in original for key in source_ids):
                raise LLMError("共享采集未解决请求缺少有效原页")
            unresolved.extend(source_ids)
    for old, targets in mapping.items():
        if (old not in original or not isinstance(targets, list) or not targets
                or any(target not in replacements or _page_identity(original[old]) !=
                       _page_identity(replacements[target]) for target in targets)):
            raise LLMError("补采替代单元与原页归属不一致")
        reachable, pending = set(), list(edges.get(old, []))
        while pending:
            target = pending.pop()
            if target not in reachable:
                reachable.add(target)
                pending.extend(edges.get(target, set()) - reachable)
        if not set(targets).issubset(reachable):
            raise LLMError("补采替代单元缺少原请求关联")
    if any(old not in mapping for old in unresolved):
        raise LLMError("共享采集仍有未解决请求，不能标记就绪")
    return list(ids), deepcopy(mapping)
