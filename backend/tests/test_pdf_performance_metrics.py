"""Compute timings exclude dispatch/slot waits and persist once per activity."""
from __future__ import annotations

import threading
import types
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.llm import request_control as control
from app.parsing import pdf_runtime
from app.services import review_collections


@pytest.fixture(autouse=True)
def reset_metrics():
    control.reset_request_metrics()
    yield
    control.reset_request_metrics()


class ImmediatePool:
    def submit(self, function, *args):
        value = function(*args)
        return types.SimpleNamespace(result=lambda timeout: value)


def test_local_ocr_task_times_worker_only_and_counts_nested_recognizer_once(monkeypatch):
    ticks = iter([100.0, 105.0, 105.25])
    monkeypatch.setattr(pdf_runtime, "_metric_clock", lambda: next(ticks))
    monkeypatch.setattr(pdf_runtime, "_pool", ImmediatePool())
    monkeypatch.setattr(pdf_runtime, "_rapid_lines", lambda data: [("本地原始识别文字", (0, 0, 1, 1))])
    events = []
    with control.request_scope(metadata={"run_id": 85, "batch_index": 4}, on_event=events.append):
        value = pdf_runtime.run_pdf_task("rapid_text", b"private-original-image")
    assert value == "本地原始识别文字"
    metrics = control.request_metrics()
    assert metrics["pdf_tasks"] == 1
    assert metrics["pdf_worker_ms"] == metrics["ocr_local_ms"] == metrics["ocr_ms"] == 250
    assert metrics["pdf_queue_wait_ms"] == 5000
    assert metrics["render_ms"] == metrics["ocr_network_ms"] == metrics["attempts"] == 0
    assert len(events) == 1 and events[0]["run_id"] == 85 and events[0]["batch_index"] == 4
    assert "private-original-image" not in str(events) and "本地原始识别文字" not in str(events)


@pytest.mark.parametrize("raster_ms", [0, 60])
def test_ocr_image_preparation_separates_extraction_and_raster_time(monkeypatch, raster_ms):
    ticks = iter([1.0, 1.1])
    monkeypatch.setattr(pdf_runtime, "_metric_clock", lambda: next(ticks))

    def prepare(*args):
        if raster_ms:
            pdf_runtime._worker_timings.get()["render_ms"] = raster_ms
        return {"kind": "page" if raster_ms else "img", "images": [b"original-image"]}

    monkeypatch.setattr(pdf_runtime, "_prepare_ocr_images", prepare)
    result = pdf_runtime._dispatch("ocr_images", (), {}, submitted_at=0.5)
    assert result["ok"] and result["value"]["images"] == [b"original-image"]
    assert result["timing"] == {"pdf_worker_ms": 100, "pdf_queue_wait_ms": 500,
        "pdf_prepare_ms": 100 - raster_ms, "render_ms": raster_ms, "ocr_local_ms": 0}


def test_full_page_ocr_raster_records_the_real_render_branch(monkeypatch):
    import pymupdf

    document = pymupdf.open()
    document.new_page(width=64, height=32)
    monkeypatch.setattr(pymupdf, "open", lambda path: document)
    ticks = iter([1.0, 1.02, 1.08, 1.1])
    monkeypatch.setattr(pdf_runtime, "_metric_clock", lambda: next(ticks))
    result = pdf_runtime._dispatch("ocr_images", ("original.pdf", 1, 0.1, 1),
        {"force_full_page": True})
    assert result["ok"] and result["value"]["kind"] == "page"
    assert result["value"]["images"][0]["png"].startswith(b"\x89PNG")
    assert result["timing"]["render_ms"] == 60
    assert result["timing"]["pdf_prepare_ms"] == 40
    assert result["timing"]["ocr_local_ms"] == 0


def test_failed_local_work_still_reports_actual_elapsed_without_material(monkeypatch):
    ticks = iter([1.0, 3.0, 3.125])
    monkeypatch.setattr(pdf_runtime, "_metric_clock", lambda: next(ticks))
    monkeypatch.setattr(pdf_runtime, "_pool", ImmediatePool())

    def fail(*args):
        raise RuntimeError("private-failed-document-text")

    monkeypatch.setattr(pdf_runtime, "_rapid_lines", fail)
    events = []
    with control.request_scope(on_event=events.append), pytest.raises(RuntimeError):
        pdf_runtime.run_pdf_task("rapid_lines", b"original")
    assert control.request_metrics()["ocr_local_ms"] == 125
    assert control.request_metrics()["pdf_failed"] == 1
    assert events[0]["error_type"] == "PDFTaskError"
    assert "private-failed-document-text" not in str(events)


def test_malformed_worker_diagnostics_cannot_break_completed_work():
    control.emit_pdf_task_metrics("rapid_text", {"pdf_worker_ms": float("nan"),
        "ocr_local_ms": "invalid", "render_ms": None, "pdf_prepare_ms": -1,
        "pdf_queue_wait_ms": float("inf")})
    control.emit_pdf_task_metrics("render", None)
    metrics = control.request_metrics()
    assert metrics["pdf_tasks"] == 2
    for name in ("pdf_worker_ms", "ocr_local_ms", "render_ms", "pdf_prepare_ms", "pdf_queue_wait_ms"):
        assert metrics[name] == 0


@contextmanager
def no_slot_wait():
    yield


def test_remote_ocr_uses_http_service_time_and_excludes_slot_wait(monkeypatch):
    ticks = iter([100.0, 105.0, 105.125])
    monkeypatch.setattr(control.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(control, "_slot", no_slot_wait)
    monkeypatch.setattr(control, "_reserve_budget", lambda: pytest.fail("OCR retains its independent budget"))
    observed = {}
    with control.request_scope(on_event=review_collections._metric_observer(observed, threading.Lock())):
        with control.http_attempt(provider="remote_paddle", model="offline", budget=False):
            pass
    metrics = control.request_metrics()
    assert metrics["ocr_ms"] == metrics["ocr_network_ms"] == metrics["service_ms"] == 125
    assert metrics["queue_ms"] == 5000 and metrics["ocr_local_ms"] == 0
    assert observed["ocr_ms"] == observed["ocr_network_ms"] == observed["request_ms"] == 125
    assert observed["queue_wait_ms"] == 5000


@pytest.fixture
def memory_run(monkeypatch):
    from app.db import session as db
    from app.db.base import Base
    from app.db.models import Bidder, Criterion, EvaluationRun, Project, RulePackage

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_session_factory", sessionmaker(bind=engine, expire_on_commit=False))
    Base.metadata.create_all(engine)
    with db.session_scope() as session:
        session.add(Project(id="pdf-metrics", name="PDF metrics"))
        session.flush()
        package = RulePackage(project_id="pdf-metrics", version=1, status="frozen")
        session.add(package)
        session.flush()
        bidder = Bidder(project_id="pdf-metrics", name="A")
        criterion = Criterion(package_id=package.id, code="Q1", name="Original evidence",
                              category="qualification", score_type="match")
        session.add_all([bidder, criterion])
        session.flush()
        run = EvaluationRun(project_id="pdf-metrics", package_id=package.id, package_version=1,
            status="running", config_json={"automation_version": 1, "executor_version": 2,
            "automation_manifest": {"criteria": [{"id": criterion.id}]}})
        session.add(run)
        session.flush()
        ids = run.id, bidder.id, criterion.id
    yield ids
    engine.dispose()


def test_run_and_batch_persist_compute_once_and_include_work_outside_batch(memory_run):
    from app.db.models import ReviewCollectionBatch, ReviewCollectionSet
    from app.db.session import session_scope
    from app.services.automation_service import execution_progress

    run_id, bidder_id, criterion_id = memory_run
    page = {"document_id": 101, "page_no": 1, "bidder_id": bidder_id, "text": "complete original page"}
    with review_collections.review_runtime(run_id, bidder_id, criterion_id, 1, {"executor_version": 2}):
        checkpoint = review_collections.CollectionCheckpoint({"name": "Evidence"}, [[page]],
            kind="v2-shared", data_class="synthetic")
    observer = review_collections.run_activity_observer(run_id, 1)
    events = []

    def perform():
        control.emit_pdf_task_metrics("rapid_text", {"pdf_worker_ms": 80,
            "ocr_local_ms": 80, "pdf_queue_wait_ms": 9000})
        control.emit_pdf_task_metrics("render", {"pdf_worker_ms": 40,
            "render_ms": 40, "pdf_queue_wait_ms": 50})
        progress = execution_progress(run_id)
        assert progress["metrics"]["ocr_ms"] == 80 and progress["metrics"]["render_ms"] == 40
        assert progress["last_progress_at"] is None
        return {"valid": True}

    with control.request_scope(on_event=observer):
        with control.request_scope(on_event=events.append):
            checkpoint.execute(0, perform, lambda raw: None)
            control.emit_pdf_task_metrics("render", {"pdf_worker_ms": 10, "render_ms": 10})
    progress = execution_progress(run_id)
    assert progress["metrics"]["ocr_ms"] == progress["metrics"]["ocr_local_ms"] == 80
    assert progress["metrics"]["render_ms"] == 50
    assert progress["metrics"]["pdf_worker_ms"] == 130
    assert progress["metrics"]["pdf_queue_wait_ms"] == 9050
    assert progress["metrics"]["http_attempts"] == 0
    with session_scope() as session:
        batch = session.execute(select(ReviewCollectionBatch)).scalar_one()
        activity = session.execute(select(ReviewCollectionSet).where(
            ReviewCollectionSet.group_key == "execution-metrics")).scalar_one()
        assert batch.metrics_json["ocr_ms"] == activity.metrics_json["ocr_ms"] == 80
        assert batch.metrics_json["render_ms"] == 40 and activity.metrics_json["render_ms"] == 50
    observer(events[0])
    assert execution_progress(run_id)["metrics"]["ocr_ms"] == 80
    assert control.request_metrics()["pdf_tasks"] == 3
