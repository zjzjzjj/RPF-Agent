"""Verify that a text extraction did not silently omit embedded scan images.

This inspects original document structure only. It does not declare image facts
readable, run OCR, or infer that absent extracted text means absent material.
"""
from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

from app.llm.base import LLMError


MATERIAL_IMAGE_MIN_RATIO = 0.15
_OCR_SOURCES = {"ocr", "ocr_img", "ocr_page", "ocr_mixed"}


def annotate_page_readability(
    pages: list[dict], stored_path: str, warnings: list | None = None,
) -> list[dict]:
    """Return copied pages with unread mixed text/image pages explicitly flagged.

    ``pages`` must contain every page of this one original document. Existing
    unreadability and known-blank flags, and the caller's parse warnings, are
    retained. A probe failure raises instead of treating the page as readable.
    DOCX image/page mapping is unsupported and is rejected explicitly.
    """
    from app.jobs.queue import JobCancelled

    copied = [dict(page) for page in pages]
    path = Path(stored_path)
    if path.suffix.lower() == ".txt":
        # Internal plain-text records cannot contain omitted bitmap material.
        return copied
    if path.suffix.lower() == ".docx":
        _check_docx_images(path)
        return copied
    if path.suffix.lower() != ".pdf":
        raise LLMError(f"当前不支持 {path.suffix or '未知格式'} 原文件的图像可读性核验")

    try:
        from app.parsing.pdf_runtime import run_pdf_task

        checked = run_pdf_task("probe", str(path))
        page_numbers = [int(page["page_no"]) for page in copied]
        if (len(page_numbers) != len(set(page_numbers))
                or set(page_numbers) != set(range(1, int(checked["page_count"]) + 1))):
            raise LLMError("原 PDF 页数与送审页面不完整对应，不能判定材料缺失")
        by_number = {int(p["page_no"]): p for p in checked["pages"]}
        for item in copied:
            geometry = by_number[int(item["page_no"])]
            largest_ratio = geometry["embedded_image_ratio"]
            item["embedded_image_ratio"] = largest_ratio
            item["page_count"] = int(checked["page_count"])
            item["rotation"] = geometry["rotation"]
            source = str(item.get("source") or "text").lower()
            already_ocr = (source in _OCR_SOURCES or source.startswith("ocr_")
                           or bool(item.get("ocr_completed") or item.get("supplemented")))
            if (largest_ratio + 1e-9 >= MATERIAL_IMAGE_MIN_RATIO and source == "text"
                    and not already_ocr and not item.get("known_blank")):
                item["unreadable"] = True
                item["requires_vision"] = True
                item["readability_reason"] = "原页存在尚未完成 OCR 或视觉核验的大幅嵌入图像"
        return copied
    except (LLMError, JobCancelled):
        raise
    except Exception as exc:
        raise LLMError(f"原 PDF 图像可读性核验失败，不能视为已完整读取：{exc}") from exc


def _check_docx_images(path: Path) -> None:
    try:
        with ZipFile(path) as package:
            names = package.namelist()
            if "word/document.xml" not in names:
                raise ValueError("缺少 Word 文档正文")
            if any(name.startswith("word/media/") and not name.endswith("/") for name in names):
                raise LLMError("DOCX 包含嵌入图像，当前无法将图片可靠映射到原始页码；"
                               "不支持将其近似当作已读取，请使用保留原始分页的 PDF 后新建运行")
    except LLMError:
        raise
    except Exception as exc:
        raise LLMError(f"原 DOCX 图像结构核验失败，不能视为已完整读取：{exc}") from exc
