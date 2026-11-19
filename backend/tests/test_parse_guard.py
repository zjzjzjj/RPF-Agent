"""解析期大小护栏与逐页进度回调（2026-11-18 run129 实证修复）。

背景：项目包 zip 上限已放宽到 8GB，但解析层仍按 HTTP 上传限额 200MB 拒绝
单份 160–426MB 的主标书 → 每家只剩报价表，技术分全 0。这里锁住三点：
1. 解析上限与上传限额解耦（默认 4GB）；
2. 超限仍明确拒绝 TOO_LARGE；
3. 逐页回调页号/总页数，供任务心跳续期。
"""
from __future__ import annotations

import pytest

from app import config
from app.parsing.base import DocumentRejectedError
from app.parsing.router import extract_document


def _make_pdf(path, pages: int = 2):
    import pymupdf

    doc = pymupdf.open()
    try:
        for i in range(pages):
            page = doc.new_page()
            page.insert_text((72, 72), f"第 {i + 1} 页 投标文件正文内容")
        doc.save(str(path))
    finally:
        doc.close()
    return path


def test_parse_limit_decoupled_from_upload_limit(monkeypatch, tmp_path):
    """上传限额很小也不影响解析：解析上限独立（默认 4GB）。"""
    monkeypatch.setenv("BID_EVAL_MAX_UPLOAD_MB", "1")
    monkeypatch.delenv("BID_EVAL_MAX_PARSE_FILE_MB", raising=False)
    assert config.max_parse_file_bytes() == 4096 * 1024 * 1024

    pdf = _make_pdf(tmp_path / "bid.pdf", pages=2)
    out = extract_document(str(pdf))
    assert len(out.pages) == 2


def test_parse_limit_env_override_rejects_oversized(monkeypatch, tmp_path):
    """BID_EVAL_MAX_PARSE_FILE_MB 可收紧（运维/测试用），超限明确 TOO_LARGE。"""
    monkeypatch.setenv("BID_EVAL_MAX_PARSE_FILE_MB", "0")
    pdf = _make_pdf(tmp_path / "small.pdf", pages=1)
    with pytest.raises(DocumentRejectedError) as exc:
        extract_document(str(pdf))
    assert exc.value.code == config.REJECT_TOO_LARGE


def test_page_progress_callback_reports_each_page(monkeypatch, tmp_path):
    """逐页回调 (page_no, page_total)：大标书靠它刷新任务心跳。

    OCR 页会在规划阶段和 OCR 完成时各回调一次（节流由 JobContext.beat 负责），
    这里只要求每页至少出现一次且页号/总页数正确。
    """
    monkeypatch.setenv("BID_EVAL_PDF_ENGINE", "pymupdf")
    pdf = _make_pdf(tmp_path / "three.pdf", pages=3)
    seen: list[tuple[int, int]] = []
    out = extract_document(str(pdf), on_progress=lambda no, total: seen.append((no, total)))
    assert len(out.pages) == 3
    assert {no for no, _ in seen} == {1, 2, 3}
    assert all(total == 3 for _, total in seen)
    assert seen[0] == (1, 3)
    assert seen[-1] == (3, 3)


def test_scoring_supplement_skipped_when_ocr_disabled(monkeypatch):
    """BID_EVAL_SCORING_OCR=0：评分期补检整段跳过（扫描件只保留标签）。"""
    from app.services import automatic_scoring_service as A

    monkeypatch.setenv("BID_EVAL_SCORING_OCR", "0")
    pages = [{"document_id": 1, "page_no": 1, "text": "", "known_blank": False,
              "unreadable": True, "requires_vision": True}]
    assert A._supplement_pages({}, pages, visual=False) is pages
    assert A._supplement_pages({}, pages, visual=True) is pages


def test_xls_routes_to_xlrd_extractor(tmp_path):
    """.xls 不再 NOT_SUPPORTED：路由到 xlrd，损坏文件按 EXTRACTION_FAILED 拒绝。"""
    path = tmp_path / "供应商信息收集模板.xls"
    path.write_bytes(b"this is not a real BIFF workbook")
    with pytest.raises(DocumentRejectedError) as exc:
        extract_document(str(path))
    assert exc.value.code == "EXTRACTION_FAILED"


def test_ocr_priority_prefers_material_pages():
    """材料页优先：命中合同/发票/证书等关键词的扫描页排前面。"""
    from app.parsing.pdf_pymupdf import _ocr_priority

    items = [
        {"page_no": 2, "texts": ["7.1.2 QUERY SCREENSHOT"]},
        {"page_no": 5, "texts": ["合同 复印件"]},
        {"page_no": 9, "texts": []},
    ]
    assert [it["page_no"] for it in sorted(items, key=_ocr_priority)] == [5, 2, 9]


def test_ocr_budget_prefers_material_pages(monkeypatch, tmp_path):
    """OCR 预算被截断时，证明材料页先占预算，普通扫描页让位。

    用伪造 layout（页面文本含中文关键词）而非合成 PDF——PyMuPDF 默认字体
    写不进中文字形，get_text 读回来是 "···"，测不出材料页判定。
    """
    import pymupdf

    from app.parsing import paddle_client
    from app.parsing import pdf_pymupdf as P

    monkeypatch.setenv("BID_EVAL_OCR_ENGINE", "remote_paddle")
    monkeypatch.setenv("BID_EVAL_OCR_MAX_PAGES", "1")
    paddle_client.reset_probe_cache()
    monkeypatch.setattr(paddle_client, "probe", lambda **kw: True)

    page1 = {"page_no": 1, "width": 595.0, "height": 842.0, "texts": ["普通页"], "boxes": [],
             "warnings": [], "has_large_image": False, "has_images": True, "tables": []}
    page2 = {"page_no": 2, "width": 595.0, "height": 842.0, "texts": ["合同 扫描件"], "boxes": [],
             "warnings": [], "has_large_image": True, "has_images": True, "tables": []}
    monkeypatch.setattr(
        "app.parsing.pdf_runtime.run_pdf_task",
        lambda op, *a, **k: {"pages": [page1, page2], "page_count": 2},
    )
    monkeypatch.setattr(
        P, "_prepare_ocr_images",
        lambda path, i, ratio: {"kind": "page", "images": [{"png": b"\x89PNG-fake",
                                                           "rect": [0, 0, 595, 842]}]},
    )
    monkeypatch.setattr(
        P, "_ocr_render_lines",
        lambda png, page, engine, state: [("识别出的证明材料内容", (0.0, 0.0, 10.0, 10.0))],
    )

    pdf = tmp_path / "mixed.pdf"
    doc = pymupdf.open()
    try:
        doc.new_page().insert_text((72, 72), "plain")
        doc.save(str(pdf))
    finally:
        doc.close()

    out = P.extract_pdf(str(pdf))
    by_page = {p.page_no: p for p in out.pages}
    assert by_page[2].source in ("ocr", "ocr_img"), "命中材料的第 2 页应优先 OCR"
    assert "识别出的证明材料内容" in by_page[2].text
    assert any("第 1 页" in w and "超出 OCR 页数上限" in w for w in out.warnings), out.warnings


def test_ocr_pages_run_in_parallel(monkeypatch, tmp_path):
    """OCR 页并行：多页扫描件在多个线程里同时识别（不再逐页串行）。"""
    import threading
    import time

    from app.parsing import paddle_client

    monkeypatch.setenv("BID_EVAL_OCR_ENGINE", "remote_paddle")
    monkeypatch.setenv("BID_EVAL_OCR_PAGE_CONCURRENCY", "4")
    monkeypatch.setenv("BID_EVAL_PADDLE_OCR_CONCURRENCY", "4")
    monkeypatch.setenv("BID_EVAL_DATA_DIR", str(tmp_path / "data"))  # 隔离 OCR 缓存
    paddle_client.reset_probe_cache()
    monkeypatch.setattr(paddle_client, "probe", lambda **kw: True)
    threads: set[str] = set()
    lock = threading.Lock()

    def fake_ocr(png, prompt=None):
        with lock:
            threads.add(threading.current_thread().name)
        time.sleep(0.05)
        return "扫描识别内容"

    monkeypatch.setattr(paddle_client, "remote_ocr_page", fake_ocr)

    pdf = _make_pdf(tmp_path / "parallel.pdf", pages=4)
    extract_document(str(pdf))
    assert len(threads) > 1, f"OCR 仍在单线程串行执行：{threads}"
