"""入库服务：上传落盘 → 排队解析 → 页与条款树入库。

拒绝码（NOT_A_PDF/NO_TEXT_LAYER/…）是**可预期的正常结果**：文档置 failed 并记录
拒绝码，任务本身成功结束（UI 显示拒绝原因），而不是让整个任务报系统错误。
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from sqlalchemy import delete, select

from app import config
from app.db.models import Document, DocumentPage, DocNode
from app.db.session import session_scope
from app.jobs.handlers import register
from app.jobs.queue import JobContext
from app.parsing.base import DocumentRejectedError
from app.parsing.chapter_tree import build_chapter_tree
from app.parsing.router import extract_document

log = logging.getLogger("bid_eval.ingest")

_SAFE_NAME = re.compile(r"[^\w.一-鿿-]+")


def sanitize_filename(name: str) -> str:
    base = Path(name).name
    return _SAFE_NAME.sub("_", base)[:200] or "upload"


def save_upload(
    project_id: str,
    filename: str,
    content: bytes,
    kind: str = "tender",
    bidder_id: int | None = None,
    data_class: str = "synthetic",
) -> tuple[int, int]:
    """落盘 + 建 Document 行 + 排队解析。返回 (document_id, job_id)。"""
    safe = sanitize_filename(filename)
    target_dir = config.upload_dir() / project_id
    target_dir.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(content).hexdigest()

    with session_scope() as s:
        if bidder_id is not None:
            from app.db.models import Bidder

            bidder = s.get(Bidder, bidder_id)
            if bidder is None:
                from app.errors import NotFound

                raise NotFound("投标人不存在")
            # 跨项目挂接会让投标文本混入别的项目评分——必须在此拦下
            if bidder.project_id != project_id:
                from app.errors import ValidationFailed

                raise ValidationFailed(
                    f"投标人 {bidder_id} 不属于项目 {project_id}（跨项目挂接被拒绝）"
                )
        doc = Document(
            project_id=project_id,
            bidder_id=bidder_id,
            kind=kind,
            original_filename=filename,
            stored_path="",  # 落盘后回填
            sha256=sha,
            parse_status="pending",
            data_class=data_class,
        )
        s.add(doc)
        s.flush()
        doc_id = doc.id
        stored = target_dir / f"doc{doc_id}_{safe}"
        stored.write_bytes(content)
        doc.stored_path = str(stored)

    from app.jobs.queue import submit

    job_id = submit("parse_document", {"document_id": doc_id}, project_id=project_id)
    return doc_id, job_id


@register("parse_document")
def parse_document(ctx: JobContext, payload: dict) -> dict:
    document_id = int(payload["document_id"])
    ctx.start(total=3, message="读取文件")

    with session_scope() as s:
        doc = s.get(Document, document_id)
        if doc is None:
            raise ValueError(f"文档不存在：{document_id}")
        path = doc.stored_path
        prefix = f"{doc.kind}{doc.id}"

    try:
        extracted = extract_document(path)
    except DocumentRejectedError as rej:
        with session_scope() as s:
            doc = s.get(Document, document_id)
            if doc is not None:
                doc.parse_status = "failed"
                doc.warnings_json = [{"code": rej.code, "message": rej.message}]
        ctx.advance(message=f"已拒绝：{rej.message}")
        return {"rejected": rej.code, "message": rej.message}

    ctx.advance(message="构建条款树")
    nodes, tree_warnings, page_ranges = build_chapter_tree(prefix, extracted.pages, extracted.table_boxes)
    warnings = [{"code": "WARN", "message": w} for w in (extracted.warnings + tree_warnings)]

    ctx.advance(message="写入数据库")
    with session_scope() as s:
        # 重解析：先清旧数据（幂等）
        s.execute(delete(DocNode).where(DocNode.document_id == document_id))
        s.execute(delete(DocumentPage).where(DocumentPage.document_id == document_id))

        for page in extracted.pages:
            c_start, c_end = page_ranges.get(page.page_no, (0, 0))
            s.add(
                DocumentPage(
                    document_id=document_id,
                    page_no=page.page_no,
                    text=page.text,
                    char_start=c_start,
                    char_end=c_end,
                    width=page.width,
                    height=page.height,
                    source=page.source,
                    page_approximate=page.page_approximate,
                    has_text_layer=page.has_text_layer,
                )
            )
        for n in nodes:
            s.add(
                DocNode(
                    document_id=document_id,
                    node_id=n.node_id,
                    parent_node_id=n.parent_node_id,
                    kind=n.kind,
                    number=n.number,
                    label=n.label,
                    title=n.title,
                    page=n.page,
                    char_start=n.char_start,
                    char_end=n.char_end,
                    text=n.text,
                    is_toc=n.is_toc,
                    is_coarse=n.is_coarse,
                    depth=n.depth,
                )
            )
        doc = s.get(Document, document_id)
        if doc is not None:
            doc.page_count = len(extracted.pages)
            doc.parse_engine = extracted.engine
            doc.parse_status = "degraded" if extracted.degraded else "parsed"
            doc.warnings_json = warnings

    ctx.advance(message="完成")
    return {
        "pages": len(extracted.pages),
        "nodes": len(nodes),
        "degraded": extracted.degraded,
    }


# ---------------------------------------------------------------------------
# 查询（API 层调用）
# ---------------------------------------------------------------------------
def list_documents(project_id: str) -> list[Document]:
    with session_scope() as s:
        rows = (
            s.execute(
                select(Document)
                .where(Document.project_id == project_id)
                .order_by(Document.id)
            )
            .scalars()
            .all()
        )
        for r in rows:
            s.expunge(r)
        return list(rows)


def get_document(document_id: int) -> Document | None:
    with session_scope() as s:
        doc = s.get(Document, document_id)
        if doc is not None:
            s.expunge(doc)
        return doc


def get_pages(document_id: int, from_page: int = 1, to_page: int | None = None) -> list[DocumentPage]:
    with session_scope() as s:
        q = (
            select(DocumentPage)
            .where(DocumentPage.document_id == document_id, DocumentPage.page_no >= from_page)
            .order_by(DocumentPage.page_no)
        )
        if to_page is not None:
            q = q.where(DocumentPage.page_no <= to_page)
        rows = s.execute(q).scalars().all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def get_nodes(document_id: int, parent_node_id: str | None = None) -> list[DocNode]:
    """条款树懒加载：给定父节点返回其直接子节点；不给父则返回根。"""
    with session_scope() as s:
        q = select(DocNode).where(DocNode.document_id == document_id)
        if parent_node_id is None:
            q = q.where(DocNode.kind == "document")
        else:
            q = q.where(DocNode.parent_node_id == parent_node_id)
        rows = s.execute(q.order_by(DocNode.id)).scalars().all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def get_node_by_id(document_id: int, node_id: str) -> DocNode | None:
    with session_scope() as s:
        row = s.execute(
            select(DocNode).where(DocNode.document_id == document_id, DocNode.node_id == node_id)
        ).scalar_one_or_none()
        if row is not None:
            s.expunge(row)
        return row


def node_exists(project_id: str, node_id: str) -> bool:
    """引用可达性校验：node_id 是否存在于本项目任一文档。"""
    with session_scope() as s:
        row = s.execute(
            select(DocNode.id)
            .join(Document, Document.id == DocNode.document_id)
            .where(Document.project_id == project_id, DocNode.node_id == node_id)
            .limit(1)
        ).first()
        return row is not None
