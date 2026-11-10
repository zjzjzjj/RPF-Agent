"""Original-page isolation, atomic artifacts and complete OCR checkpoints."""
from __future__ import annotations

import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor

import pymupdf
import pytest
from PIL import Image

from app.errors import AppError


def _png(color="white"):
    output = io.BytesIO()
    Image.new("RGB", (500, 400), color).save(output, format="PNG")
    return output.getvalue()


def _pdf(tmp_path, *, images=False, rotation=0):
    path = tmp_path / "original.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=500, height=700)
        page.insert_text((20, 20), "Original bidder page")
        if images:
            page.insert_image(pymupdf.Rect(10, 40, 490, 320), stream=_png("white"), keep_proportion=False)
            page.insert_image(pymupdf.Rect(10, 350, 490, 650), stream=_png("yellow"), keep_proportion=False)
        page.set_rotation(rotation)
        document.save(path)
    return path


def test_render_coalesces_concurrent_requests_and_rebuilds_corrupt_png(tmp_path, monkeypatch):
    from app.evidence import renderer
    from app.parsing import pdf_runtime

    path = _pdf(tmp_path)
    monkeypatch.setenv("BID_EVAL_RENDER_DIR", str(tmp_path / "renders"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    calls = []
    original = pdf_runtime.run_pdf_task

    def spy(operation, *args, **kwargs):
        calls.append(operation)
        return original(operation, *args, **kwargs)

    monkeypatch.setattr(pdf_runtime, "run_pdf_task", spy)
    with ThreadPoolExecutor(max_workers=7) as pool:
        paths = list(pool.map(lambda _: renderer.render_page_png(digest, str(path), 1, scale=1), range(7)))
    assert len(set(paths)) == 1
    assert calls == ["render"]
    with Image.open(paths[0]) as image:
        assert image.size == (500, 700)
    paths[0].write_bytes(b"half-written-image")
    rebuilt = renderer.render_page_png(digest, str(path), 1, scale=1)
    with Image.open(rebuilt) as image:
        image.verify()
    assert calls == ["render", "render"]
    assert not list(rebuilt.parent.glob("*.tmp"))
    rebuilt.with_suffix(".json").write_text("[]", encoding="utf-8")
    renderer.render_page_png(digest, str(path), 1, scale=1)
    assert calls == ["render", "render", "render"]


def test_fallback_preserves_rotation_and_frozen_geometry(tmp_path, monkeypatch):
    import pypdfium2
    from app.evidence.renderer import render_page_png
    from app.parsing import pdf_runtime

    path = _pdf(tmp_path, rotation=90)

    def unavailable(*args, **kwargs):
        raise RuntimeError("PDFium cannot read this original")

    monkeypatch.setattr(pypdfium2, "PdfDocument", unavailable)
    result = pdf_runtime._render_pdf(str(path), 1, 1)
    assert result["engine"] == "pymupdf" and result["rotation"] == 90
    assert (result["width"], result["height"]) == (700, 500)
    monkeypatch.setenv("BID_EVAL_RENDER_DIR", str(tmp_path / "renders"))
    monkeypatch.setattr(pdf_runtime, "run_pdf_task", lambda operation, *args: result.copy())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(AppError, match="冻结解析记录不一致"):
        render_page_png(digest, str(path), 1, 1, expected_page_count=2)
    output = render_page_png(digest, str(path), 1, 1, expected_page_count=1,
                             expected_width=700, expected_height=500, expected_rotation=90)
    with Image.open(output) as image:
        assert image.size == (700, 500)
    with pytest.raises(AppError, match="冻结解析尺寸无效"):
        render_page_png(digest, str(path), 1, 1, expected_width=float("nan"))


def test_automatic_ocr_reads_both_images_once_and_reuses_complete_cache(tmp_path, monkeypatch):
    from app.parsing import ocr, paddle_client, pdf_pymupdf

    path = _pdf(tmp_path, images=True)
    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(ocr, "ocr_backend_available", lambda: (True, "remote_paddle"))
    calls = []

    def recognize(png):
        calls.append(hashlib.sha256(png).hexdigest())
        return "Complete original image " + calls[-1]

    monkeypatch.setattr(paddle_client, "remote_ocr_page", recognize)
    first = pdf_pymupdf.ocr_pages_text(str(path), [1])
    assert len(calls) == 2 and len(set(calls)) == 2
    assert first[1].count("Complete original image") == 2
    assert pdf_pymupdf.ocr_pages_text(str(path), [1]) == first
    assert len(calls) == 2
    cached = list((tmp_path / "data" / "ocr_cache").glob("*/result.json"))
    assert len(cached) == 2
    assert all(json.loads(p.read_text())["complete"] for p in cached)


def test_failed_second_image_does_not_publish_partial_page_and_first_image_survives(tmp_path, monkeypatch):
    from app.parsing import ocr, paddle_client, pdf_pymupdf, pdf_runtime

    path = _pdf(tmp_path, images=True)
    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(ocr, "ocr_backend_available", lambda: (True, "remote_paddle"))
    calls = []
    second_should_fail = True

    def recognize(png):
        digest = hashlib.sha256(png).hexdigest()
        calls.append(digest)
        if len(set(calls)) == 2 and second_should_fail:
            raise RuntimeError("segment still truncated")
        return "Complete source " + digest

    original = pdf_runtime.run_pdf_task

    def without_local_engine(operation, *args, **kwargs):
        if operation == "rapid_text":
            raise RuntimeError("local OCR unavailable")
        return original(operation, *args, **kwargs)

    monkeypatch.setattr(pdf_runtime, "run_pdf_task", without_local_engine)
    monkeypatch.setattr(paddle_client, "remote_ocr_page", recognize)
    with pytest.raises(RuntimeError, match="local OCR unavailable"):
        pdf_pymupdf.ocr_pages_text(str(path), [1])
    second_should_fail = False
    result = pdf_pymupdf.ocr_pages_text(str(path), [1])
    assert result[1].count("Complete source") == 2
    assert len(calls) == 3
    assert calls[0] != calls[1] == calls[2]


def test_truncated_segment_is_never_accepted_by_strict_client(monkeypatch):
    from app.parsing import paddle_client

    responses = iter([
        {"choices": [{"finish_reason": "length", "message": {"content": "partial whole"}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": "complete first"}}]},
        {"choices": [{"finish_reason": "length", "message": {"content": "partial second"}}]},
    ])
    monkeypatch.setattr(paddle_client, "_post", lambda *args: next(responses))
    with pytest.raises(RuntimeError, match="分段输出仍.*截断"):
        paddle_client.remote_ocr_page(_png())


def test_ocr_cache_changes_with_model_budget_and_prompt(tmp_path, monkeypatch):
    from app.parsing.pdf_pymupdf import _ocr_cache_path

    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path))
    before = _ocr_cache_path(_png(), "remote_paddle", "rescue-complete")
    monkeypatch.setenv("BID_EVAL_PADDLE_OCR_MODEL", "different-ocr-model")
    after_model = _ocr_cache_path(_png(), "remote_paddle", "rescue-complete")
    assert after_model != before


def test_corrupt_ocr_text_is_recovered_instead_of_reused(tmp_path, monkeypatch):
    from app.parsing import paddle_client, pdf_pymupdf

    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path))
    calls = []

    def recognize(png):
        calls.append(png)
        return "Verified source image content"

    monkeypatch.setattr(paddle_client, "remote_ocr_page", recognize)
    source = _png()
    original = pdf_pymupdf._rescue_ocr_text(source, "remote_paddle")
    cache = pdf_pymupdf._ocr_cache_path(source, "remote_paddle", "rescue-complete")
    damaged = json.loads(cache.read_text(encoding="utf-8"))
    damaged["text"] = "Wrong cached bidder facts"
    cache.write_text(json.dumps(damaged), encoding="utf-8")
    assert pdf_pymupdf._rescue_ocr_text(source, "remote_paddle") == original
    assert len(calls) == 2


def test_corrupt_ocr_line_cache_is_recovered(tmp_path, monkeypatch):
    from app.parsing import pdf_pymupdf

    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path))
    calls = []

    def recognize(png, state):
        calls.append(png)
        return "Source facts from original page"

    monkeypatch.setattr(pdf_pymupdf, "_remote_text", recognize)
    source = _png()
    original = pdf_pymupdf._complete_ocr_lines(source, (0, 0, 500, 400), "remote_paddle", {}, "test")
    cache = pdf_pymupdf._ocr_cache_path(source, "remote_paddle", "test")
    damaged = json.loads(cache.read_text(encoding="utf-8"))
    damaged["rows"][0][0] = "Wrong cached bidder facts"
    cache.write_text(json.dumps(damaged), encoding="utf-8")
    assert pdf_pymupdf._complete_ocr_lines(source, (0, 0, 500, 400), "remote_paddle", {}, "test") == original
    assert len(calls) == 2


def test_encrypted_pdf_retains_explicit_rejection(tmp_path):
    from app import config
    from app.parsing.base import DocumentRejectedError
    from app.parsing.pdf_pymupdf import extract_pdf

    path = tmp_path / "encrypted.pdf"
    with pymupdf.open() as document:
        document.new_page()
        document.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                      owner_pw="owner", user_pw="user")
    with pytest.raises(DocumentRejectedError) as rejected:
        extract_pdf(str(path))
    assert rejected.value.code == config.REJECT_ENCRYPTED
