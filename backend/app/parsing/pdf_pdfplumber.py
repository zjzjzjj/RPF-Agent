"""PDF 解析主路径：pdfplumber（MIT 许可）。

不依赖 PyMuPDF（AGPL）——闭源交付不被传染。可选加速路径见 pdf_pymupdf.py，
默认关闭（docs/decisions/0001-why-not-pymupdf.md）。

产出：
- 每页文本（逐行，附行 bbox）、页宽高、是否有文字层；
- 表格 bbox（供条款树排除编号扫描）；
- 全文坐标（char_start/char_end）由 chapter_tree 统一分配。
"""
from __future__ import annotations

import logging
from pathlib import Path

from app import config
from app.parsing.base import DocumentRejectedError, ExtractedDocument, ExtractedPage

log = logging.getLogger("bid_eval.parsing.pdf")

# pdfplumber 导入较慢，延迟到实际使用
_TABLE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "join_tolerance": 3,
}


def probe_has_text_layer(path: str) -> bool:
    """抽样前 N 页判断是否有文字层。"""
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        probe = pdf.pages[: config.TEXT_PROBE_PAGES]
        if not probe:
            return False
        total = 0
        for page in probe:
            total += len((page.extract_text() or "").strip())
        return (total / len(probe)) >= config.MIN_CHARS_PER_PAGE


def _check_file(path: str) -> None:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        raise DocumentRejectedError(config.REJECT_EMPTY_FILE, "文件为空或不存在")
    if p.stat().st_size > config.max_upload_bytes():
        raise DocumentRejectedError(config.REJECT_TOO_LARGE, "文件超过大小上限")
    with open(path, "rb") as fh:
        head = fh.read(5)
    if not head.startswith(b"%PDF"):
        raise DocumentRejectedError(config.REJECT_NOT_A_PDF, "文件不是 PDF 格式")


def extract_pdf(path: str) -> ExtractedDocument:
    """解析 PDF：页文本 + 行 bbox + 表格 bbox。"""
    import pdfplumber

    _check_file(path)
    warnings: list[str] = []
    degraded = False
    pages: list[ExtractedPage] = []
    table_boxes: dict[int, list[tuple[float, float, float, float]]] = {}

    try:
        pdf = pdfplumber.open(path)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "password" in msg or "encrypt" in msg:
            raise DocumentRejectedError(config.REJECT_ENCRYPTED, "PDF 已加密，无法解析") from exc
        raise DocumentRejectedError(config.REJECT_EXTRACTION_FAILED, f"PDF 打开失败：{exc}") from exc

    with pdf:
        if not pdf.pages:
            raise DocumentRejectedError(config.REJECT_EMPTY_FILE, "PDF 没有任何页面")

        # 文字层判定（抽样）
        probe = pdf.pages[: config.TEXT_PROBE_PAGES]
        probe_chars = sum(len((pg.extract_text() or "").strip()) for pg in probe)
        has_layer = (probe_chars / max(1, len(probe))) >= config.MIN_CHARS_PER_PAGE
        if not has_layer:
            raise DocumentRejectedError(
                config.REJECT_NO_TEXT_LAYER,
                "PDF 无文字层（疑似扫描件）。OCR 通道尚未启用，请提供数字版文件。",
            )

        for i, page in enumerate(pdf.pages, start=1):
            try:
                text_lines = page.extract_text_lines() or []
            except Exception as exc:  # noqa: BLE001 —— 单页失败不让整本文档失败
                warnings.append(f"第 {i} 页文本提取失败：{exc}")
                text_lines = []
                degraded = True
            texts = [ln.get("text", "") for ln in text_lines]
            boxes = [
                (float(ln["x0"]), float(ln["top"]), float(ln["x1"]), float(ln["bottom"]))
                for ln in text_lines
            ]
            pages.append(
                ExtractedPage(
                    page_no=i,
                    text="\n".join(texts),
                    width=float(page.width),
                    height=float(page.height),
                    has_text_layer=True,
                    line_boxes=boxes,
                )
            )
            # 表格 bbox（供条款树排除编号扫描；提取失败只警告）
            try:
                for table in page.find_tables(_TABLE_SETTINGS):
                    x0, top, x1, bottom = table.bbox
                    table_boxes.setdefault(i, []).append(
                        (float(x0), float(top), float(x1), float(bottom))
                    )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"第 {i} 页表格探测失败：{exc}")
                degraded = True

    return ExtractedDocument(
        pages=pages, engine="pdfplumber", warnings=warnings, table_boxes=table_boxes, degraded=degraded
    )
