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
from app.db.models import Document, DocumentPage, DocNode, MaterialTag
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


def _tender_mismatch_warning(project_id: str, document_id: int, extracted) -> str | None:  # noqa: ANN001
    """投标文件与项目招标文件的项目关键词重合率过低 → 错配告警（纯确定性）。

    2026-10-27 GYUU 事故实证：二维码项目招标文件 + 多媒体投标文件混在同项目，
    评标细则与投标内容文不对题却静默出分。取各自首页 3 页文本做 2-gram Jaccard，
    低于阈值 → 警告（仅提示不阻断）。
    """
    try:
        with session_scope() as s:
            tender_id = s.execute(
                select(Document.id).where(
                    Document.project_id == project_id,
                    Document.kind.in_(("tender", "addendum")),
                    Document.id != document_id,
                ).order_by(Document.id).limit(1)
            ).scalar_one_or_none()
            if tender_id is None:
                return None
            tender_pages = s.execute(
                select(DocumentPage.text).where(DocumentPage.document_id == tender_id)
                .order_by(DocumentPage.page_no).limit(3)
            ).scalars().all()
        bid_head = "\n".join(
            (p.text or "") for p in list(extracted.pages)[:3]
        )
        tender_head = "\n".join(t or "" for t in tender_pages)
        if not bid_head.strip() or not tender_head.strip():
            return None

        def _grams(t: str) -> set[str]:
            t = re.sub(r"\s+", "", t or "")[:4000]
            return {t[i: i + 2] for i in range(len(t) - 1)}

        a, b = _grams(tender_head), _grams(bid_head)
        inter = len(a & b)
        union = max(1, len(a | b))
        overlap = inter / union
        if overlap < 0.10:
            return (
                f"【规则包错配告警】投标文件与本项目招标文件的文本重合率仅 {overlap:.0%}"
                "（低于 10%）——两者可能不属于同一项目，请核对招标文件与投标文件"
                "的项目名称后再评分"
            )
    except Exception:  # noqa: BLE001 —— 护栏失败不阻断解析
        log.debug("错配检查失败", exc_info=True)
    return None


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
        project_id = doc.project_id
        doc_kind = doc.kind

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
        # 重解析：先清旧数据（幂等）。material_tags 同步清——页号会错位、
        # 旧标签悬挂是隐性事故（标签锚点是 (document_id, page_no)）
        s.execute(delete(DocNode).where(DocNode.document_id == document_id))
        s.execute(delete(DocumentPage).where(DocumentPage.document_id == document_id))
        s.execute(delete(MaterialTag).where(MaterialTag.document_id == document_id))

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
            doc.author = extracted.author  # 串通检测信号源（无元数据则 None）
            # 规则包错配护栏（2026-10-27）：投标文件与项目招标文件的项目关键词
            # 重合率过低 → 落警告（GYUU 类事故：二维码招标+多媒体投标静默出分）
            warn = _tender_mismatch_warning(project_id, document_id, extracted)
            if warn:
                warnings = list(warnings or []) + [warn]
            doc.warnings_json = warnings

    ctx.advance(message="完成")
    result = {
        "pages": len(extracted.pages),
        "nodes": len(nodes),
        "degraded": extracted.degraded,
    }
    # 招标文件解析成功 → 自动匹配并更新规则（护栏见 auto_merge_service）
    from app.services.auto_merge_service import maybe_schedule_auto_match

    result.update(maybe_schedule_auto_match(project_id, document_id))
    # 投标文件解析成功 → 自动分类打标（四类材料索引；tender 侧暂不分类）
    if doc_kind == "bid" and result.get("rejected") is None:
        from app.material_index.service import schedule_classify

        classify_job = schedule_classify(document_id, project_id=project_id)
        if classify_job:
            result["classify_job_id"] = classify_job
    return result


# ---------------------------------------------------------------------------
# 查询（API 层调用）
# ---------------------------------------------------------------------------
def rescue_material_pages(document_id: int, max_pages: int | None = None) -> dict:
    """对投标文档做定向 OCR 救援：只 OCR 证据段内"有材料标题但文本极少 + 大图"的页。

    幂等（已 OCR 页 source != "text" 跳过）、限量（`BID_EVAL_MATERIAL_OCR_MAX_PAGES`）。
    写入页文本后**重算全文 char_start/char_end**（保持跨页偏移一致）并追加文档警告。
    不调 LLM；不删除任何原文。
    """
    from app.evidence.case_evidence import (
        PageRec,
        classify_materials,
        link_case_blocks,
        locate_section,
        material_ocr_targets,
        page_image_ratios,
        parse_case_table,
    )
    from app.parsing.pdf_pymupdf import ocr_pages_text

    limit = max_pages if max_pages is not None else config.material_ocr_max_pages()
    with session_scope() as s:
        doc = s.get(Document, document_id)
        if doc is None or doc.kind != "bid":
            return {"document_id": document_id, "targets": 0, "rescued": 0, "pages": []}
        pdf_path = doc.stored_path
        recs = [
            PageRec(int(p.page_no), p.text or "", p.source or "text")
            for p in s.execute(
                select(DocumentPage)
                .where(DocumentPage.document_id == document_id)
                .order_by(DocumentPage.page_no)
            ).scalars()
        ]
    if not pdf_path or not Path(pdf_path).exists():
        return {"document_id": document_id, "targets": 0, "rescued": 0, "pages": [],
                "error": "pdf missing"}
    ratios = page_image_ratios(pdf_path)
    sec = locate_section(recs)
    # 先做块级分类，只救援"材料不可读"的案例页（每案 ≤2 页），避免整段扫描页
    # 全送 OCR 打爆远程服务（2026-10-21 doc24 实证）。
    blocks: list = []
    if sec.found:
        table_rows = parse_case_table(
            pdf_path, sec.start, sec.end, config.case_table_scan_pages()
        )
        if table_rows:
            blocks = link_case_blocks(table_rows, recs, sec)
            classify_materials(blocks, recs, ratios)
    targets = material_ocr_targets(recs, ratios, sec, blocks=blocks or None)[: max(0, limit)]
    if not targets:
        return {"document_id": document_id, "targets": 0, "rescued": 0, "pages": []}
    try:
        ocr_text = ocr_pages_text(pdf_path, targets, min_image_ratio=0.10)
    except Exception as exc:  # noqa: BLE001 —— 救援失败不阻断入/评分
        log.warning("材料页 OCR 救援失败 doc=%s：%s", document_id, exc)
        return {"document_id": document_id, "targets": len(targets), "rescued": 0,
                "pages": [], "error": str(exc)}
    rescued: list[int] = []
    with session_scope() as s:
        rows = {
            int(p.page_no): p
            for p in s.execute(
                select(DocumentPage).where(DocumentPage.document_id == document_id)
            ).scalars()
        }
        for pno, text in ocr_text.items():
            row = rows.get(int(pno))
            if row is None or not text.strip():
                continue
            old_lines = [ln for ln in (row.text or "").split("\n") if ln.strip()]
            new_lines = [ln for ln in text.split("\n") if ln.strip()]
            merged = old_lines + [ln for ln in new_lines if ln not in old_lines]
            row.text = "\n".join(merged)
            row.source = "ocr"
            rescued.append(int(pno))
        if rescued:
            # 页文本已变 → 标签失效（因果链：text_sha 校验只是兜底）
            s.execute(
                delete(MaterialTag).where(
                    MaterialTag.document_id == document_id,
                    MaterialTag.page_no.in_(rescued),
                )
            )
            # 重算全文偏移（页文本变长，后续页 char_start 必须同步平移）
            offset = 0
            for p in s.execute(
                select(DocumentPage)
                .where(DocumentPage.document_id == document_id)
                .order_by(DocumentPage.page_no)
            ).scalars():
                p.char_start = offset
                p.char_end = offset + len(p.text or "")
                offset = p.char_end
            doc = s.get(Document, document_id)
            if doc is not None:
                warns = list(doc.warnings_json or [])
                warns.append(
                    "材料页 OCR 救援：第 " + "、".join(str(x) for x in rescued) + " 页"
                )
                doc.warnings_json = warns
    return {"document_id": document_id, "targets": len(targets),
            "rescued": len(rescued), "pages": rescued}


# 人员级材料章节标题（社保证明/经验证明/劳动合同…）——标题页本身或紧随的空白
# 扫描页常承载嵌入图（2026-10-23 实证：龙即 p247"社保证明"48 字 + 1.4MB 参保证
# 明扫描图、致成 p169 标题+参保证明同页、先数 p186 空白页即参保证明），文本层
# 永远看不到内容 → c59/c60 的 social_security/experience_proof 全 False → 0 分。
_PERSON_MAT_HEADER = re.compile(
    r"社保证明|社保代缴|社会保险参保证明|参保人员|缴纳证明"
    r"|工作经验证明|年以上工作经验证明|经验证明|劳动合同|职称"
)


def rescue_person_material_pages(document_id: int, max_pages: int = 60) -> dict:
    """人员级评分项的材料页定向 OCR 救援。

    目标页：①含人员材料章节标题且文本 <250 字的页（标题+嵌入扫描图同页）；
    ②标题页之后文本 <100 字的连续空白扫描页（内容在后一页的形态，最多 6 页）。
    幂等（已 OCR 页跳过）、限量。写入后重算 char_start/char_end。
    """
    from app.evidence.case_evidence import page_image_ratios
    from app.parsing.pdf_pymupdf import ocr_pages_text

    with session_scope() as s:
        doc = s.get(Document, document_id)
        if doc is None or doc.kind != "bid":
            return {"document_id": document_id, "targets": 0, "rescued": 0, "pages": []}
        pdf_path = doc.stored_path
        rows = [
            (int(p.page_no), p.text or "", p.source or "text")
            for p in s.execute(
                select(DocumentPage)
                .where(DocumentPage.document_id == document_id)
                .order_by(DocumentPage.page_no)
            ).scalars()
        ]
    if not pdf_path or not Path(pdf_path).exists():
        return {"document_id": document_id, "targets": 0, "rescued": 0, "pages": [],
                "error": "pdf missing"}
    ratios = page_image_ratios(pdf_path)
    targets: list[int] = []
    by_no = {no: (t, src) for no, t, src in rows}
    ordered_nos = [no for no, _t, _s in rows]
    for i, no in enumerate(ordered_nos):
        t, src = by_no[no]
        if src != "text":
            continue  # 已 OCR/救援过
        if not _PERSON_MAT_HEADER.search(t or ""):
            continue
        if len((t or "").strip()) >= 250:
            continue
        if ratios.get(no, 0.0) >= 0.03 or len((t or "").strip()) < 120:
            targets.append(no)
        # 标题页后的连续空白扫描页（内容在下一页的形态）
        for j in range(i + 1, min(i + 7, len(ordered_nos))):
            nxt = ordered_nos[j]
            nt, nsrc = by_no[nxt]
            if nsrc != "text" or len((nt or "").strip()) >= 100:
                break
            if ratios.get(nxt, 0.0) >= 0.03 or len((nt or "").strip()) < 60:
                targets.append(nxt)
    targets = sorted(set(targets))[: max(0, max_pages)]
    if not targets:
        return {"document_id": document_id, "targets": 0, "rescued": 0, "pages": []}
    try:
        ocr_text = ocr_pages_text(pdf_path, targets, min_image_ratio=0.03)
    except Exception as exc:  # noqa: BLE001 —— 救援失败不阻断评分
        log.warning("人员材料页 OCR 救援失败 doc=%s：%s", document_id, exc)
        return {"document_id": document_id, "targets": len(targets), "rescued": 0,
                "pages": [], "error": str(exc)}
    rescued: list[int] = []
    with session_scope() as s:
        page_rows = {
            int(p.page_no): p
            for p in s.execute(
                select(DocumentPage).where(DocumentPage.document_id == document_id)
            ).scalars()
        }
        for pno, text in ocr_text.items():
            row = page_rows.get(int(pno))
            if row is None or not text.strip():
                continue
            old_lines = [ln for ln in (row.text or "").split("\n") if ln.strip()]
            new_lines = [ln for ln in text.split("\n") if ln.strip()]
            merged = old_lines + [ln for ln in new_lines if ln not in old_lines]
            row.text = "\n".join(merged)
            row.source = "ocr"
            rescued.append(int(pno))
        if rescued:
            # 页文本已变 → 标签失效（同 rescue_material_pages 口径）
            s.execute(
                delete(MaterialTag).where(
                    MaterialTag.document_id == document_id,
                    MaterialTag.page_no.in_(rescued),
                )
            )
            offset = 0
            for p in s.execute(
                select(DocumentPage)
                .where(DocumentPage.document_id == document_id)
                .order_by(DocumentPage.page_no)
            ).scalars():
                p.char_start = offset
                p.char_end = offset + len(p.text or "")
                offset = p.char_end
            doc = s.get(Document, document_id)
            if doc is not None:
                warns = list(doc.warnings_json or [])
                warns.append(
                    "人员材料页 OCR 救援：第 " + "、".join(str(x) for x in rescued) + " 页"
                )
                doc.warnings_json = warns
    return {"document_id": document_id, "targets": len(targets),
            "rescued": len(rescued), "pages": rescued}


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
