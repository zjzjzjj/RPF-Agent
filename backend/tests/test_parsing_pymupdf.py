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


@pytest.fixture(autouse=True)
def _hermetic_ocr(tmp_path, monkeypatch):
    """OCR 全程离线：本地 rapidocr 引擎 + 临时数据目录（缓存不落仓库）。

    需要验证远程引擎路径的用例自行覆盖 BID_EVAL_OCR_ENGINE 并 monkeypatch 探针。
    """
    monkeypatch.setenv("BID_EVAL_OCR_ENGINE", "rapidocr")
    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path))
    from app.parsing import paddle_client

    paddle_client.reset_probe_cache()
    yield
    paddle_client.reset_probe_cache()


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
    # 2026-10-21：扫描件优先对**页内嵌入图原图** OCR（整页渲染只读得回文字层）
    assert doc.pages[0].source == "ocr_img"
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


# ---------------------------------------------------------------------------
# 2026-10-21 故障回归：远程 OCR 必须真正生效
# ---------------------------------------------------------------------------
def _scan_pdf(tmp_path: Path, samples_dir: Path, name: str, title: str | None = None) -> str:
    """构造"扫描件形态"PDF：整页大图（可选带一行文字层标题）。"""
    import io as _io

    import pypdfium2 as pdfium
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    tender = next((samples_dir / "tender").glob("*招标文件.pdf"))
    img = pdfium.PdfDocument(str(tender))[0].render(scale=2.0).to_pil()
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    out = tmp_path / name
    cv = canvas.Canvas(str(out), pagesize=A4)
    if title:
        cv.setFont("Helvetica", 9)
        cv.drawString(40, A4[1] - 30, title)
    cv.drawImage(ImageReader(buf), 0, 0, width=A4[0], height=A4[1])
    cv.showPage()
    cv.save()
    return str(out)


def test_remote_engine_available_without_rapidocr(
    samples_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    """① 回归闸门：本地 rapidocr 不可用时，远程引擎仍必须跑起来。

    旧实现 `ocr_available = get_ocr() is not None` 只看本地——镜像未装 rapidocr
    时 4 份标书整体零 OCR，扫描件（证书/查询截图/合同发票）全部丢失。
    """
    from app.parsing import ocr as ocr_mod
    from app.parsing import paddle_client
    from app.parsing.pdf_pymupdf import extract_pdf

    monkeypatch.setenv("BID_EVAL_OCR_ENGINE", "remote_paddle")
    monkeypatch.setattr(ocr_mod, "get_ocr", lambda: None)  # 本地引擎不可用
    paddle_client.reset_probe_cache()
    monkeypatch.setattr(paddle_client, "probe", lambda **kw: True)
    monkeypatch.setattr(
        paddle_client, "remote_ocr_page",
        lambda png, prompt=None: "国家市场监督管理总局 全国认证认可信息公共服务平台\n"
                                "证书编号：02623Q30431R5M-HZ\n杭州诚道科技股份有限公司",
    )

    doc = extract_pdf(_scan_pdf(tmp_path, samples_dir, "scan.pdf"))
    assert doc.engine == "pymupdf+ocr"
    assert doc.pages[0].source == "ocr_img"
    assert "认证认可信息公共服务平台" in doc.pages[0].text
    assert not any("OCR 组件未安装" in w for w in doc.warnings)
    assert any("OCR 引擎：remote_paddle" in w for w in doc.warnings), doc.warnings


def test_probe_down_and_no_rapidocr_degrades_quietly(
    samples_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    """② 远程探针不过 + 本地不可用 → 不抛异常、按缺字页处理、警告写明引擎。"""
    from app.parsing import ocr as ocr_mod
    from app.parsing import paddle_client
    from app.parsing.pdf_pymupdf import extract_pdf

    monkeypatch.setenv("BID_EVAL_OCR_ENGINE", "remote_paddle")
    monkeypatch.setattr(ocr_mod, "get_ocr", lambda: None)
    paddle_client.reset_probe_cache()
    monkeypatch.setattr(paddle_client, "probe", lambda **kw: False)

    doc = extract_pdf(_scan_pdf(tmp_path, samples_dir, "scan2.pdf", title="7.1.2 QUERY SCREENSHOT"))
    assert doc.pages[0].source == "text"
    assert any("OCR 不可用" in w for w in doc.warnings), doc.warnings


def test_embedded_image_preferred_and_text_layer_merged(
    samples_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    """③ 文字层标题 + 大图：OCR 送的是**嵌入图原图**，且标题行仍在文本里。"""
    from app.parsing import paddle_client
    from app.parsing.pdf_pymupdf import extract_pdf

    monkeypatch.setenv("BID_EVAL_OCR_ENGINE", "remote_paddle")
    paddle_client.reset_probe_cache()
    monkeypatch.setattr(paddle_client, "probe", lambda **kw: True)
    seen: list[bytes] = []

    def fake_ocr(png, prompt=None):
        seen.append(png)
        return "信息技术服务管理体系认证证书\n证书编号：01222IS0725R3L"

    monkeypatch.setattr(paddle_client, "remote_ocr_page", fake_ocr)

    doc = extract_pdf(_scan_pdf(tmp_path, samples_dir, "scan3.pdf", title="7.1.2 QUERY SCREENSHOT"))
    page = doc.pages[0]
    assert page.source == "ocr_img"
    assert seen, "必须对嵌入图原图调用 OCR"
    assert seen[0].startswith(b"\x89PNG"), "嵌入图须重编码为 PNG（JPEG/CMYK 流会被服务端拒绝）"
    assert "QUERY SCREENSHOT" in page.text, "文字层标题不能被 OCR 结果覆盖"
    assert "认证证书" in page.text


def test_ocr_line_boxes_align_with_text_lines(
    samples_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    """④ 行 bbox 必须与 text.split("\\n") 一一对应（旧实现返回整页单行 blob）。"""
    from app.parsing import paddle_client
    from app.parsing.pdf_pymupdf import extract_pdf

    monkeypatch.setenv("BID_EVAL_OCR_ENGINE", "remote_paddle")
    paddle_client.reset_probe_cache()
    monkeypatch.setattr(paddle_client, "probe", lambda **kw: True)
    monkeypatch.setattr(
        paddle_client, "remote_ocr_page",
        lambda png, prompt=None: "第一行\n第二行\n第三行",
    )

    doc = extract_pdf(_scan_pdf(tmp_path, samples_dir, "scan4.pdf"))
    page = doc.pages[0]
    assert len(page.text.split("\n")) == len(page.line_boxes)


def test_ocr_cache_key_varies_by_engine_variant_and_version(tmp_path: Path) -> None:
    """⑥ 缓存键必须区分引擎/路径变体/版本——否则改路径会复用旧（错误）结果。"""
    from app.parsing.pdf_pymupdf import _ocr_cache_path

    png = b"\x89PNG-fake"
    assert _ocr_cache_path(png, "remote_paddle", "img") != _ocr_cache_path(png, "remote_paddle", "page")
    assert _ocr_cache_path(png, "remote_paddle", "img") != _ocr_cache_path(png, "rapidocr", "img")
    assert _ocr_cache_path(png, "remote_paddle", "img") == _ocr_cache_path(png, "remote_paddle", "img")


def test_render_low_yield_marks_degraded_warning(
    samples_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    """⑦ 整页渲染只读回页码（真实故障形态）→ 必须留可见警告，不能静默。"""
    from app.parsing import paddle_client
    from app.parsing.pdf_pymupdf import extract_pdf

    monkeypatch.setenv("BID_EVAL_OCR_ENGINE", "remote_paddle")
    paddle_client.reset_probe_cache()
    monkeypatch.setattr(paddle_client, "probe", lambda **kw: True)
    # 只对整页渲染路径返回垃圾（嵌入图路径返回空 → 回落到渲染）
    monkeypatch.setattr(paddle_client, "remote_ocr_page", lambda png, prompt=None: "41")
    monkeypatch.setattr(
        "app.parsing.pdf_pymupdf._page_image_blocks", lambda doc, page: []
    )

    doc = extract_pdf(_scan_pdf(tmp_path, samples_dir, "scan5.pdf"))
    assert any("OCR 产出过少" in w for w in doc.warnings), doc.warnings
