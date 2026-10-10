"""提取器路由：按文件类型分派。"""
from __future__ import annotations

from pathlib import Path

from app import config
from app.parsing.base import DocumentRejectedError, ExtractedDocument


def extract_document(path: str) -> ExtractedDocument:
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        from app.parsing.pdf_pdfplumber import extract_pdf

        return extract_pdf(path)
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
