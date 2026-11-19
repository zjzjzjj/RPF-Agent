"""提取器路由：按文件类型与配置分派（PDF 主引擎可切换，见 decisions/0001）。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from app import config
from app.parsing.base import DocumentRejectedError, ExtractedDocument

log = logging.getLogger("bid_eval.parsing.router")

Progress = Callable[[int, int], None]


def _extract_pdf(path: str, on_progress: Progress | None = None) -> ExtractedDocument:
    """按 BID_EVAL_PDF_ENGINE 分派（默认 pymupdf）；主引擎缺失时回退 pdfplumber。"""
    if config.pdf_engine() == "pymupdf":
        try:
            from app.parsing.pdf_pymupdf import extract_pdf

            return extract_pdf(path, on_progress=on_progress)
        except ImportError as exc:  # 依赖缺失：回退而非崩溃
            log.warning("pymupdf 不可用（%s），回退 pdfplumber", exc)
    from app.parsing.pdf_pdfplumber import extract_pdf

    return extract_pdf(path, on_progress=on_progress)


def extract_document(path: str, on_progress: Progress | None = None) -> ExtractedDocument:
    """按扩展名分派；``on_progress(page_no, page_total)`` 逐页回调（刷新任务心跳）。"""
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path, on_progress=on_progress)
    if suffix == ".docx":
        from app.parsing.docx_extractor import extract_docx

        return extract_docx(path, on_progress=on_progress)
    if suffix == ".xlsx":
        from app.parsing.xlsx_text import extract_xlsx

        return extract_xlsx(path, on_progress=on_progress)
    if suffix == ".xls":
        from app.parsing.xls_text import extract_xls

        return extract_xls(path, on_progress=on_progress)
    if suffix == ".doc":
        raise DocumentRejectedError(
            config.REJECT_NOT_SUPPORTED, "旧版 .doc 暂不支持，请另存为 .docx 或 PDF 后上传"
        )
    raise DocumentRejectedError(
        config.REJECT_NOT_SUPPORTED, f"暂不支持的文件类型：{suffix or '（无扩展名）'}"
    )
