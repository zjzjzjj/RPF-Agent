"""Textless image material needs vision; transport failures remain failures."""
from __future__ import annotations

import io
import threading
from contextvars import ContextVar

import pytest
from PIL import Image

from app.llm import request_control
from app.parsing import ocr, pdf_pymupdf, pdf_runtime
from app.errors import AppError, DataClassDenied, LLMBudgetExceeded
from app.jobs.queue import JobCancelled
from app.parsing.pdf_runtime import OCRNoTextError, OCRProcessingError


@pytest.fixture
def targeted_ocr(monkeypatch):
    monkeypatch.setattr(pdf_pymupdf, "_check_file", lambda path: None)
    monkeypatch.setattr(ocr, "ocr_backend_available", lambda: (True, "rapidocr"))

    def prepare(path, page_no, ratio):
        return {"kind": "img", "images": [
            {"png": str(page_no).encode(), "rect": (10, 20, 30, 40)}]}

    monkeypatch.setattr(pdf_pymupdf, "_prepare_ocr_images", prepare)


def test_textless_photo_remains_typed_with_file_page_image_context(targeted_ocr, monkeypatch):
    def no_text(*args):
        raise OCRNoTextError(engine_attempts=[{"engine": "rapidocr", "outcome": "no_text"}])

    monkeypatch.setattr(pdf_pymupdf, "_rescue_ocr_text", no_text)
    with pytest.raises(OCRNoTextError, match="原始.pdf 第 418 页第 1/1 张图像 ocr_image") as caught:
        pdf_pymupdf.ocr_pages_text("/original/原始.pdf", [418])
    assert caught.value.ocr_context["page_no"] == 418
    assert caught.value.ocr_context["image_index"] == 1
    assert len(caught.value.ocr_context["image_sha256"]) == 64
    assert caught.value.engine_attempts == [{"engine": "rapidocr", "outcome": "no_text"}]


def test_textless_callback_retains_successful_images_and_continues_other_pages(targeted_ocr, monkeypatch):
    def prepare(path, page_no, ratio):
        values = [b"certificate", b"photo"] if page_no == 418 else [b"contract"]
        return {"kind": "img", "images": [{"png": v, "rect": (0, 0, 1, 1)} for v in values]}

    def recognize(png, engine):
        if png == b"photo":
            raise OCRNoTextError()
        return png.decode()

    monkeypatch.setattr(pdf_pymupdf, "_prepare_ocr_images", prepare)
    monkeypatch.setattr(pdf_pymupdf, "_rescue_ocr_text", recognize)
    issues = []
    result = pdf_pymupdf.ocr_pages_text("original.pdf", [418, 419], on_textless=issues.append)
    assert result == {418: "certificate", 419: "contract"}
    assert len(issues) == 1
    assert issues[0]["page_no"] == 418 and issues[0]["image_index"] == 2
    assert issues[0]["image_count"] == 2 and issues[0]["state"] == "requires_vision"


@pytest.mark.parametrize("message", ["OCR timeout", "OCR bad JSON", "OCR segment truncated"])
def test_technical_failure_never_becomes_textless_or_partial_completion(targeted_ocr, monkeypatch, message):
    def fail(*args):
        raise RuntimeError(message)

    monkeypatch.setattr(pdf_pymupdf, "_rescue_ocr_text", fail)
    issues = []
    with pytest.raises(RuntimeError, match=message) as caught:
        pdf_pymupdf.ocr_pages_text("original.pdf", [418], on_textless=issues.append)
    assert not isinstance(caught.value, OCRNoTextError)
    assert caught.value.ocr_context["page_no"] == 418 and issues == []


def test_ocr_preparation_failure_has_page_and_action_context(targeted_ocr, monkeypatch):
    def fail(*args):
        raise RuntimeError("native raster failed")

    monkeypatch.setattr(pdf_pymupdf, "_prepare_ocr_images", fail)
    with pytest.raises(RuntimeError, match="original.pdf 第 418 页 prepare_ocr_images"):
        pdf_pymupdf.ocr_pages_text("original.pdf", [418])


def test_two_page_workers_preserve_context_and_merge_callbacks_in_input_order(targeted_ocr, monkeypatch):
    from app.services.review_activity import report, reporting

    identity = ContextVar("test_targeted_ocr_identity", default=None)
    first_started, second_started = threading.Event(), threading.Event()
    active = 0
    peak = 0
    lock = threading.Lock()
    seen_contexts, callback_threads, issues, activities, events = [], [], [], [], []
    coordinator = threading.get_ident()

    def recognize(png, engine):
        nonlocal active, peak
        page_no = int(png)
        with lock:
            active += 1
            peak = max(peak, active)
            seen_contexts.append(identity.get())
        try:
            if page_no == 3:
                first_started.set()
                assert second_started.wait(3), "independent second page must run concurrently"
            if page_no == 1:
                assert first_started.wait(3)
                second_started.set()
            report("local OCR page " + str(page_no))
            request_control.emit_pdf_task_metrics("rapid_text", {"ocr_local_ms": 10})
            raise OCRNoTextError()
        finally:
            with lock:
                active -= 1

    def callback(issue):
        issues.append(issue)
        callback_threads.append(threading.get_ident())

    monkeypatch.setattr(pdf_pymupdf, "_rescue_ocr_text", recognize)
    token = identity.set("frozen run identity")
    try:
        with reporting(activities.append), request_control.request_scope(
                metadata={"run_id": 85}, on_event=events.append):
            result = pdf_pymupdf.ocr_pages_text("original.pdf", [3, 1, 2, 3], on_textless=callback)
    finally:
        identity.reset(token)
    assert result == {} and [issue["page_no"] for issue in issues] == [3, 1, 2]
    assert callback_threads == [coordinator] * 3
    assert peak == 2 and seen_contexts == ["frozen run identity"] * 3
    assert len(activities) == 6 and len(events) == 3
    assert all(event["run_id"] == 85 for event in events)


def test_local_empty_result_survives_worker_error_transport_as_typed(monkeypatch):
    monkeypatch.setattr(ocr, "get_ocr", lambda: object())
    monkeypatch.setattr(ocr, "recognize", lambda array: [])
    output = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(output, format="PNG")

    class ImmediatePool:
        def submit(self, function, *args):
            value = function(*args)
            return type("ImmediateFuture", (), {"result": lambda self, timeout: value})()

    monkeypatch.setattr(pdf_runtime, "_pool", ImmediatePool())
    with pytest.raises(OCRNoTextError):
        pdf_runtime.run_pdf_task("rapid_text", output.getvalue())


def test_fallback_diagnostics_retain_remote_fault_before_local_empty(monkeypatch, tmp_path):
    from app.parsing import paddle_client

    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(pdf_runtime, "atomic_json", lambda *a: pytest.fail("no text is never a complete cache"))

    def remote_fail(*args):
        raise RuntimeError("remote OCR timeout")

    def local_empty(*args):
        raise OCRNoTextError()

    monkeypatch.setattr(paddle_client, "remote_ocr_page", remote_fail)
    monkeypatch.setattr(pdf_runtime, "run_pdf_task", local_empty)
    with pytest.raises(OCRNoTextError) as caught:
        pdf_pymupdf._rescue_ocr_text(b"original photograph", "remote_paddle")
    attempts = caught.value.engine_attempts
    assert [(a["engine"], a["outcome"]) for a in attempts] == [
        ("remote_paddle", "failed"), ("rapidocr", "no_text")]
    assert "remote OCR timeout" in attempts[0]["error"]
    assert not list(tmp_path.rglob("result.json"))


def test_exhausted_technical_engines_keep_typed_error_and_image_context(targeted_ocr, monkeypatch, tmp_path):
    from app.parsing import paddle_client

    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ocr, "ocr_backend_available", lambda: (True, "remote_paddle"))
    calls = []

    def remote_fail(*args):
        calls.append("remote_paddle")
        raise RuntimeError("remote OCR timeout")

    def local_fail(*args):
        calls.append("rapidocr")
        raise RuntimeError("local OCR engine crashed")

    monkeypatch.setattr(paddle_client, "remote_ocr_page", remote_fail)
    monkeypatch.setattr(pdf_runtime, "run_pdf_task", local_fail)
    issues = []
    with pytest.raises(OCRProcessingError, match="原始.pdf 第 418 页第 1/1 张图像") as caught:
        pdf_pymupdf.ocr_pages_text("/original/原始.pdf", [418], on_textless=issues.append)
    error = caught.value
    assert error.ocr_attempts_exhausted is True
    assert not isinstance(error, OCRNoTextError) and issues == []
    assert error.ocr_context["file_name"] == "原始.pdf"
    assert error.ocr_context["page_no"] == 418 and error.ocr_context["image_index"] == 1
    assert len(error.ocr_context["image_sha256"]) == 64
    assert calls == ["remote_paddle", "rapidocr"]
    assert [(a["engine"], a["outcome"]) for a in error.engine_attempts] == [
        ("remote_paddle", "failed"), ("rapidocr", "failed")]
    assert "remote OCR timeout" in error.engine_attempts[0]["error"]
    assert "local OCR engine crashed" in error.engine_attempts[1]["error"]
    assert not list(tmp_path.rglob("result.json"))


@pytest.mark.parametrize("error_type", [AppError, LLMBudgetExceeded, DataClassDenied, JobCancelled])
def test_control_errors_survive_network_and_ocr_layers_without_fallback(
        targeted_ocr, monkeypatch, tmp_path, error_type):
    from app.parsing import paddle_client

    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ocr, "ocr_backend_available", lambda: (True, "remote_paddle"))
    error = error_type("stop this execution")
    calls = []

    def remote_exit(*args):
        calls.append("remote_paddle")
        raise error

    monkeypatch.setattr(paddle_client, "_post", remote_exit)
    monkeypatch.setattr(pdf_runtime, "run_pdf_task", lambda *args: pytest.fail("control exit cannot use fallback"))
    with pytest.raises(error_type) as caught:
        pdf_pymupdf.ocr_pages_text("original.pdf", [418], on_textless=lambda _: pytest.fail("not textless"))
    assert caught.value is error
    assert calls == ["remote_paddle"]
    assert not list(tmp_path.rglob("result.json"))


@pytest.mark.parametrize("engine", ["remote_paddle", "rapidocr"])
def test_existing_complete_cache_and_positional_call_contract_remain_compatible(
        targeted_ocr, monkeypatch, tmp_path, engine):
    import hashlib
    from app.parsing import paddle_client

    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ocr, "ocr_backend_available", lambda: (True, engine))
    image = b"418"
    text = "already verified complete certificate"
    path = pdf_pymupdf._ocr_cache_path(image, engine, "rescue-complete")
    pdf_runtime.atomic_json(path, {"version": 3, "complete": True, "engine": engine,
        "image_sha256": hashlib.sha256(image).hexdigest(), "text": text,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest()})
    before = path.read_bytes()
    monkeypatch.setattr(paddle_client, "remote_ocr_page", lambda *args: pytest.fail("remote cache must be reused"))
    monkeypatch.setattr(pdf_runtime, "run_pdf_task", lambda *args: pytest.fail("local cache must be reused"))

    assert pdf_pymupdf._rescue_ocr_text(image, engine) == text
    assert pdf_pymupdf.ocr_pages_text("original.pdf", [418], 0.1) == {418: text}
    assert path.read_bytes() == before


def test_successful_local_fallback_is_reused_before_repeating_failed_remote(monkeypatch, tmp_path):
    from app.parsing import paddle_client

    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path))
    calls = []

    def remote(*args):
        calls.append("remote")
        raise RuntimeError("remote could not finish this image")

    def local(*args):
        calls.append("local")
        return "Complete certificate from the original image"

    monkeypatch.setattr(paddle_client, "remote_ocr_page", remote)
    monkeypatch.setattr(pdf_runtime, "run_pdf_task", local)
    first = pdf_pymupdf._rescue_ocr_text(b"immutable certificate image", "remote_paddle")
    assert calls == ["remote", "local"]
    assert pdf_pymupdf._rescue_ocr_text(b"immutable certificate image", "remote_paddle") == first
    assert calls == ["remote", "local"]


@pytest.mark.parametrize("damage", ["text", "complete", "image_sha256", "version"])
def test_invalid_fallback_cache_does_not_bypass_real_ocr(monkeypatch, tmp_path, damage):
    import hashlib
    import json
    from app.parsing import paddle_client

    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path))
    image, text = b"immutable certificate", "Old complete certificate"
    path = pdf_pymupdf._ocr_cache_path(image, "rapidocr", "rescue-complete")
    record = {"version": 3, "complete": True, "engine": "rapidocr", "text": text,
              "image_sha256": hashlib.sha256(image).hexdigest(),
              "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
    record[damage] = False if damage == "complete" else "corrupt"
    pdf_runtime.atomic_json(path, record)
    calls = []
    monkeypatch.setattr(paddle_client, "remote_ocr_page", lambda *args: calls.append("remote") or "New complete certificate")
    assert pdf_pymupdf._rescue_ocr_text(image, "remote_paddle") == "New complete certificate"
    assert calls == ["remote"]
