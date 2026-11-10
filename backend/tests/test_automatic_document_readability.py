"""Exercise mixed text/scans against actual locally generated PDF files."""
from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

import pymupdf
import pytest
from PIL import Image, ImageDraw

from app.llm.base import LLMError
from app.services.automatic_document_service import annotate_page_readability


def _scan_png() -> bytes:
    image = Image.new("RGB", (500, 700), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((15, 15, 485, 685), outline="black", width=3)
    draw.text((50, 70), "SCANNED CERTIFICATE / CONTRACT", fill="black")
    for y in range(150, 600, 35):
        draw.line((50, y, 450, y), fill="black", width=2)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _text(page) -> None:
    remaining = page.insert_textbox(pymupdf.Rect(15, 15, 385, 180), "Many extracted text words. " * 35, fontsize=8)
    assert remaining >= 0


def _record(page_no, **kwargs):
    return {"page_no": page_no, "text": "Many extracted text words. " * 70,
            "source": "text", "has_text_layer": True, "known_blank": False,
            "unreadable": False, "requires_vision": False, **kwargs}


def test_dense_text_with_large_scan_is_unreadable_while_logo_blank_and_ocr_are_preserved(tmp_path):
    path = tmp_path / "mixed-scans.pdf"
    image = _scan_png()
    with pymupdf.open() as document:
        page = document.new_page(width=400, height=600)
        _text(page)
        page.insert_image(pymupdf.Rect(20, 200, 380, 570), stream=image, keep_proportion=False)
        page = document.new_page(width=400, height=600)
        _text(page)
        page.insert_image(pymupdf.Rect(350, 550, 380, 580), stream=image, keep_proportion=False)
        document.new_page(width=400, height=600)
        page = document.new_page(width=400, height=600)
        _text(page)
        page.insert_image(pymupdf.Rect(20, 200, 380, 570), stream=image, keep_proportion=False)
        document.save(path)
    with pymupdf.open(path) as document:
        assert len(document[0].get_text().strip()) > 500
    pages = [_record(1), _record(2), _record(3, text="", source="skipped", known_blank=True),
             _record(4, source="ocr_img")]
    parsed_warnings = ["保留已有解析警告"]
    checked = annotate_page_readability(pages, str(path), parsed_warnings)
    assert checked[0]["unreadable"] and checked[0]["requires_vision"]
    assert checked[0]["embedded_image_ratio"] > 0.5
    assert checked[1]["unreadable"] is False
    assert checked[2]["known_blank"] is True and checked[2]["unreadable"] is False
    assert checked[3]["unreadable"] is False
    assert pages[0]["unreadable"] is False
    assert parsed_warnings == ["保留已有解析警告"]


def test_rotated_and_clipped_pdf_images_use_actual_visible_area(tmp_path):
    path = tmp_path / "rotated-cropped.pdf"
    image = _scan_png()
    with pymupdf.open() as document:
        page = document.new_page(width=400, height=600)
        _text(page)
        page.insert_image(pymupdf.Rect(20, 200, 380, 570), stream=image, keep_proportion=False)
        page.set_rotation(90)
        page = document.new_page(width=400, height=600)
        _text(page)
        page.insert_image(pymupdf.Rect(-300, -300, 10, 10), stream=image, keep_proportion=False)
        page = document.new_page(width=400, height=600)
        _text(page)
        page.insert_image(pymupdf.Rect(20, 200, 220, 380), stream=image, keep_proportion=False)
        document.save(path)
    checked = annotate_page_readability([_record(1), _record(2), _record(3)], str(path))
    assert checked[0]["requires_vision"] is True
    assert checked[1]["embedded_image_ratio"] < 0.01
    assert checked[1]["requires_vision"] is False
    assert checked[2]["embedded_image_ratio"] == 0.15
    assert checked[2]["requires_vision"] is True


def test_pdf_image_probe_failure_never_defaults_to_readable(tmp_path, monkeypatch):
    path = tmp_path / "probe-failure.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=400, height=600)
        _text(page)
        page.insert_image(pymupdf.Rect(20, 200, 380, 570), stream=_scan_png(), keep_proportion=False)
        document.save(path)

    def broken_probe(self, *args, **kwargs):
        raise RuntimeError("image parser broken")

    monkeypatch.setattr(pymupdf.Page, "get_image_info", broken_probe)
    # The public path normally isolates native calls in spawn workers. Inject
    # the same probe locally only to make the native parser fault deterministic.
    from app.parsing import pdf_runtime

    monkeypatch.setattr(pdf_runtime, "run_pdf_task", lambda operation, *args: pdf_runtime._probe_pdf(*args))
    pages = [_record(1)]
    with pytest.raises(LLMError, match="第 1 页原文件图像结构核验失败"):
        annotate_page_readability(pages, str(path))
    assert pages[0]["requires_vision"] is False


def test_docx_images_require_reliable_original_page_mapping(tmp_path):
    path = tmp_path / "embedded.docx"
    with ZipFile(path, "w") as package:
        package.writestr("word/document.xml", "<document/>")
        package.writestr("word/media/image1.png", _scan_png())
    with pytest.raises(LLMError, match="无法将图片可靠映射到原始页码"):
        annotate_page_readability([_record(1)], str(path))
