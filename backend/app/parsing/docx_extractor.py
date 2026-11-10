"""DOCX 解析：python-docx，段落 + 表格按文档顺序提取。

docx 没有页的概念，采用**虚拟分页**（按累计字符数在段落边界切页），
page_approximate=True 会如实落库——证据引用到"第 N 页（约）"时人工可知这是近似。
docx 无坐标，bbox 一律为空（页级引用）。
"""
from __future__ import annotations

from pathlib import Path

from app import config
from app.parsing.base import DocumentRejectedError, ExtractedDocument, ExtractedPage


def _check_file(path: str) -> None:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        raise DocumentRejectedError(config.REJECT_EMPTY_FILE, "文件为空或不存在")
    if p.stat().st_size > config.max_upload_bytes():
        raise DocumentRejectedError(config.REJECT_TOO_LARGE, "文件超过大小上限")
    with open(path, "rb") as fh:
        if fh.read(2) != b"PK":
            raise DocumentRejectedError(config.REJECT_NOT_A_PDF, "文件不是 docx 格式（zip 容器）")


def _iter_block_lines(doc) -> list[str]:  # noqa: ANN001
    """按文档顺序产出段落文本与表格行（表格行以 " | " 连接单元格）。"""
    from docx.document import Document as _Doc
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    lines: list[str] = []
    body = doc.element.body
    for child in body.iterchildren():
        if child.tag.endswith("}p"):
            para = Paragraph(child, doc)
            text = para.text.strip()
            if text:
                lines.append(text)
        elif child.tag.endswith("}tbl"):
            table = Table(child, doc)
            for row in table.rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                # 去掉相邻重复（合并单元格会重复展开）
                deduped: list[str] = []
                for c in cells:
                    if not deduped or deduped[-1] != c:
                        deduped.append(c)
                line = " | ".join(deduped).strip(" |")
                if line:
                    lines.append(line)
    return lines


def extract_docx(path: str) -> ExtractedDocument:
    _check_file(path)
    try:
        from docx import Document

        doc = Document(path)
    except Exception as exc:  # noqa: BLE001
        raise DocumentRejectedError(config.REJECT_EXTRACTION_FAILED, f"docx 打开失败：{exc}") from exc

    lines = _iter_block_lines(doc)
    if not lines:
        raise DocumentRejectedError(config.REJECT_NO_TEXT_LAYER, "docx 中没有任何文本内容")

    # 虚拟分页：按字符数在段落边界切页
    pages: list[ExtractedPage] = []
    buf: list[str] = []
    buf_chars = 0
    page_no = 1

    def _flush() -> None:
        nonlocal buf, buf_chars, page_no
        if not buf:
            return
        pages.append(
            ExtractedPage(
                page_no=page_no,
                text="\n".join(buf),
                width=595.0,  # A4 points，占位（无真实版面）
                height=842.0,
                has_text_layer=True,
                page_approximate=True,
            )
        )
        page_no += 1
        buf = []
        buf_chars = 0

    for line in lines:
        if buf_chars >= config.DOCX_APPROX_CHARS_PER_PAGE:
            _flush()
        buf.append(line)
        buf_chars += len(line) + 1
    _flush()

    warnings = ["docx 为虚拟分页（page_approximate=true），页码为近似值，证据无坐标。"]
    return ExtractedDocument(pages=pages, engine="python-docx", warnings=warnings)
