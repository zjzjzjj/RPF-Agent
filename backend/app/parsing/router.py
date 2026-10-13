"""提取器路由：按文件类型与配置分派（PDF 主引擎可切换，见 decisions/0001）。"""
from __future__ import annotations

import logging
from pathlib import Path

from app import config
from app.parsing.base import DocumentRejectedError, ExtractedDocument

log = logging.getLogger("bid_eval.parsing.router")


def _extract_pdf(path: str) -> ExtractedDocument:
    """按 BID_EVAL_PDF_ENGINE 分派（默认 pymupdf）；主引擎缺失时回退 pdfplumber。"""
    if config.pdf_engine() == "pymupdf":
        try:
            from app.parsing.pdf_pymupdf import extract_pdf

            return extract_pdf(path)
        except ImportError as exc:  # 依赖缺失：回退而非崩溃
            log.warning("pymupdf 不可用（%s），回退 pdfplumber", exc)
    from app.parsing.pdf_pdfplumber import extract_pdf

    return extract_pdf(path)


def extract_document(path: str) -> ExtractedDocument:
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix == ".docx":
        from app.parsing.docx_extractor import extract_docx

        return extract_docx(path)
    if suffix == ".doc":
        raise DocumentRejectedError(
            config.REJECT_NOT_SUPPORTED, "旧版 .doc 暂不支持，请另存为 .docx 或 PDF 后上传"
        )
    raise DocumentRejectedError(
        config.REJECT_NOT_SUPPORTED, f"暂不支持的文件类型：{suffix or '（无扩展名）'}"
    )
