"""PyMuPDF 主解析引擎测试：双引擎对比 + 前导垃圾容忍 + OCR 兜底。"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4

REPO = Path(__file__).resolve().parents[2]
SAMPLES = REPO / "data" / "samples"


def test_double_engine_consistency(samples_dir: Path) -> None:
    """PyMuPDF 与 pdfplumber（回退引擎）对样例 PDF 的解析必须语义一致。"""
    from app.parsing import pdf_pdfplumber, pdf_pymupdf

    pdfs = sorted(SAMPLES.glob("**/*.pdf"))
    assert pdfs, "样例 PDF 缺失"
    for pdf_path in pdfs:
        a = pdf_pymupdf.extract_pdf(str(pdf_path))
        b = pdf_pdfplumber.extract_pdf(str(pdf_path))
        assert len(a.pages) == len(b.pages), pdf_path.name
        chars_a = sum(len(p.text) for p in a.pages)
        chars_b = sum(len(p.text) for p in b.pages)
        assert abs(chars_a - chars_b) / max(1, chars_b) < 0.02, (
            f"{pdf_path.name} 双引擎字符差过大：{chars_a} vs {chars_b}"
        )


def test_leading_garbage_tolerated(samples_dir: Path, tmp_path: Path) -> None:
    """前导垃圾字节的 PDF（政府平台签章件常见）不再被 NOT_A_PDF 误拒。"""
    pdf_path = next((samples_dir / "tender").glob("*招标文件.pdf"))
    garbage = b"\x00\x01\xfe\xff SIGNATURE-BLOCK \r\n" + b"\x00" * 64
    polluted = tmp_path / "polluted.pdf"
    polluted.write_bytes(garbage + pdf_path.read_bytes())

    from app.parsing.pdf_pymupdf import extract_pdf

    doc = extract_pdf(str(polluted))
    assert doc.pages and sum(len(p.text) for p in doc.pages) > 0


def test_non_pdf_still_rejected(tmp_path: Path) -> None:
    """纯文本文件（无 %PDF）仍然被拒绝——魔数放宽不等于不设防。"""
    from app.parsing.pdf_pymupdf import extract_pdf
    from app.parsing.base import DocumentRejectedError

    txt = tmp_path / "not_a_pdf.pdf"
    txt.write_text("这只是个文本文件", encoding="utf-8")
    with pytest.raises(DocumentRejectedError) as ei:
        extract_pdf(str(txt))
    assert ei.value.code == "NOT_A_PDF"


def test_ocr_fallback_on_image_only_pdf(samples_dir: Path, tmp_path: Path) -> None:
    """纯图片 PDF（扫描件形态）→ OCR 兜底出文字，engine=pymupdf+ocr。"""
    pytest.importorskip("rapidocr_onnxruntime")

    import io

    import pypdfium2 as pdfium
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    from app.parsing.pdf_pymupdf import extract_pdf

    tender = next((samples_dir / "tender").glob("*招标文件.pdf"))
    img = pdfium.PdfDocument(str(tender))[0].render(scale=2.0).to_pil()
    fake = tmp_path / "scan_like.pdf"
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    cv = canvas.Canvas(str(fake), pagesize=A4)
    cv.drawImage(ImageReader(buf), 0, 0, width=A4[0], height=A4[1])
    cv.showPage()
    cv.save()

    doc = extract_pdf(str(fake))
    assert doc.engine == "pymupdf+ocr"
    assert doc.degraded is True
    assert len(doc.pages[0].text) >= 30, "OCR 应识别出可观的文字量"
    assert doc.pages[0].source == "ocr"
    assert doc.pages[0].has_text_layer is False


def test_blank_pages_skipped(tmp_path: Path) -> None:
    """真空白页（无文字无图）→ source=skipped，不触发 OCR、不算退化。"""
    from reportlab.pdfgen import canvas

    from app.parsing.pdf_pymupdf import extract_pdf

    fake = tmp_path / "blank.pdf"
    cv = canvas.Canvas(str(fake), pagesize=A4)
    cv.showPage()  # 空页
    cv.showPage()
    cv.save()

    doc = extract_pdf(str(fake))
    assert len(doc.pages) == 2
    assert all(p.source == "skipped" for p in doc.pages)
    assert doc.degraded is False
