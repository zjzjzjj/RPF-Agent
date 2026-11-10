# -*- coding: utf-8 -*-
"""材料分类编排：规则层（+LLM 兜底）→ material_tags 落库 → 索引查询。

- classify_document：全量/增量分类（text_sha 跳过未变页），同事务重建。
- classify_material job：parse_document 后自动排队（仅 kind=bid）。
- ensure_index：评分前同步补规则层（毫秒级，无 LLM），盖住 rescue 改文本后的失效页。
- index_for_document / index_for_bidder：API 查询组装。
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import delete, select

from app import config
from app.db.models import DocNode, Document, DocumentPage, MaterialTag
from app.db.session import session_scope
from app.jobs.handlers import register
from app.jobs.queue import JobContext, submit
from app.material_index import rules
from app.material_index.catalog import MATERIAL_CATEGORIES

log = logging.getLogger("bid_eval.material_index")

# 版本戳：规则层词表/判定口径变更时递增，落 classifier_version 供审计
RULES_VER = 1
LLM_VER = 0  # PR2 起启用 LLM 兜底时置 1


def classifier_version() -> str:
    return f"r{RULES_VER}-l{LLM_VER}"


def text_sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 版面段检测（纯 PyMuPDF，不落 OCR）
# ---------------------------------------------------------------------------
def detect_layout_segments(stored_path: str, page_nos: set[int] | None = None) -> list[rules.LayoutSeg]:
    """每页表格/图片 bbox → LayoutSeg。失败静默返回空（分类不因此中断）。

    图片阈值比解析期放宽（面积≥10%）：材料扫描图常不到 35% 也要打标。
    """
    out: list[rules.LayoutSeg] = []
    try:
        import fitz  # PyMuPDF
    except Exception:  # noqa: BLE001
        return out
    try:
        doc = fitz.open(stored_path)
    except Exception:  # noqa: BLE001
        return out
    try:
        for pno in range(1, doc.page_count + 1):
            if page_nos is not None and pno not in page_nos:
                continue
            page = doc.load_page(pno - 1)
            try:
                rect = page.rect
                page_area = max(1.0, float(rect.width) * float(rect.height))
            except Exception:  # noqa: BLE001
                page_area = 1.0
            idx = 0
            from app.parsing.pdf_pymupdf import _page_tables

            for bbox in _page_tables(page):
                out.append(rules.LayoutSeg(pno, "table", idx, bbox, None))
                idx += 1
            try:
                infos = page.get_image_info(xrefs=True)
            except Exception:  # noqa: BLE001
                infos = []
            for info in infos or []:
                bbox = info.get("bbox")
                if not bbox:
                    continue
                x0, y0, x1, y1 = (float(v) for v in bbox[:4])
                ratio = (max(0.0, x1 - x0) * max(0.0, y1 - y0)) / page_area
                if ratio < 0.10:
                    continue
                out.append(rules.LayoutSeg(pno, "image", idx, (x0, y0, x1, y1), round(ratio, 4)))
                idx += 1
    except Exception:  # noqa: BLE001 —— 版面检测失败不影响页级分类
        log.warning("版面段检测失败：%s", stored_path, exc_info=True)
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass
    return out


# ---------------------------------------------------------------------------
# 节点标题链（页 → 该页所在章节标题，深→浅）
# ---------------------------------------------------------------------------
def _node_title_chains(document_id: int, pages: list[rules.PageInput]) -> dict[int, list[str]]:
    with session_scope() as s:
        rows = s.execute(
            select(DocNode.node_id, DocNode.parent_node_id, DocNode.title,
                   DocNode.page, DocNode.char_start, DocNode.char_end, DocNode.depth)
            .where(DocNode.document_id == document_id)
        ).all()
    if not rows:
        return {}
    by_id = {r.node_id: r for r in rows}
    out: dict[int, list[str]] = {}
    for p in pages:
        # 口径：起点页 ≤ 本页的最深节点 + 其祖先链（材料页常无节点，回退页内标题行）
        started = [r for r in rows if r.page <= p.page_no]
        if not started:
            continue
        deepest = max(started, key=lambda r: (r.page, r.depth))
        chain: list[str] = []
        cur = deepest
        seen: set[str] = set()
        while cur is not None and cur.node_id not in seen:
            seen.add(cur.node_id)
            if cur.title:
                chain.append(cur.title)
            parent_id = cur.parent_node_id
            cur = by_id.get(parent_id) if parent_id else None
        if chain:
            out[p.page_no] = chain
    return out


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------
def _persist(document_id: int, rows: list[rules.TagRow], pages: dict[int, str],
             keep: set[int] | None = None) -> int:
    """重建/增量写入 material_tags。keep 非空时保留这些页的现有行（增量）。"""
    sha_by_page = {no: text_sha(t) for no, t in pages.items()}
    ver = classifier_version()
    with session_scope() as s:
        if keep:
            s.execute(
                delete(MaterialTag).where(
                    MaterialTag.document_id == document_id,
                    MaterialTag.page_no.notin_(list(keep)),
                )
            )
        else:
            s.execute(delete(MaterialTag).where(MaterialTag.document_id == document_id))
        for r in rows:
            s.add(
                MaterialTag(
                    document_id=document_id,
                    page_no=r.page_no,
                    seg_kind=r.seg_kind,
                    seg_index=r.seg_index,
                    bbox_json=r.bbox_json,
                    category=r.category,
                    tags_json=r.tags or None,
                    method=r.method,
                    confidence=r.confidence,
                    basis=(r.basis or "")[:512] or None,
                    text_sha=sha_by_page.get(r.page_no),
                    classifier_version=ver,
                )
            )
        s.flush()
    return len(rows)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def classify_document(document_id: int, mode: str = "full", use_llm: bool = False) -> dict:
    """分类一份文档。mode=incremental 时跳过 text_sha 未变的页。"""
    with session_scope() as s:
        doc = s.get(Document, document_id)
        if doc is None:
            raise ValueError(f"文档不存在：{document_id}")
        stored_path = doc.stored_path
        pages = s.execute(
            select(DocumentPage.page_no, DocumentPage.text, DocumentPage.source)
            .where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_no)
        ).all()
        old_rows = s.execute(
            select(MaterialTag.page_no, MaterialTag.text_sha)
            .where(MaterialTag.document_id == document_id, MaterialTag.seg_kind == "page")
        ).all()
    if not pages:
        return {"pages": 0, "rule": 0, "llm": 0, "other": 0, "segments": 0}

    old_sha = dict(old_rows)
    page_inputs = [rules.PageInput(int(no), text or "", src or "text") for no, text, src in pages]
    text_by_page = {p.page_no: p.text for p in page_inputs}

    if mode == "incremental" and old_sha:
        unchanged = {
            no for no, t in text_by_page.items()
            if old_sha.get(no) == text_sha(t)
        }
    else:
        unchanged = set()

    todo = [p for p in page_inputs if p.page_no not in unchanged]
    node_titles = _node_title_chains(document_id, todo) if todo else {}
    segs = detect_layout_segments(stored_path, {p.page_no for p in todo}) if todo else []

    rows = rules.classify_pages(todo, node_titles, segs)

    llm_count = 0
    if use_llm and config.material_index_enabled():
        from app.material_index import llm_fallback  # 延迟导入（PR2）

        unknown = [r for r in rows if rules.needs_llm(r)]
        if unknown:
            rows = llm_fallback.fill_unknown(document_id, rows, text_by_page)
            llm_count = sum(1 for r in rows if r.method == "llm")

    # 增量：保留未变页的行，只重建新判页
    keep = unchanged if mode == "incremental" and unchanged else None
    _persist(document_id, rows, text_by_page, keep=keep)

    other = sum(1 for r in rows if r.seg_kind == "page" and r.category == "other")
    return {
        "pages": len(page_inputs),
        "rule": sum(1 for r in rows if r.seg_kind == "page" and r.method == "rule"),
        "llm": llm_count,
        "other": other,
        "segments": sum(1 for r in rows if r.seg_kind != "page"),
        "classifier_version": classifier_version(),
    }


def ensure_index(document_id: int) -> int:
    """评分前同步补规则层：只重判缺行/文本已变的页（无 LLM）。返回补判页数。"""
    if not config.material_index_enabled():
        return 0
    with session_scope() as s:
        pages = s.execute(
            select(DocumentPage.page_no, DocumentPage.text, DocumentPage.source)
            .where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_no)
        ).all()
        old = dict(
            s.execute(
                select(MaterialTag.page_no, MaterialTag.text_sha)
                .where(MaterialTag.document_id == document_id, MaterialTag.seg_kind == "page")
            ).all()
        )
    if not pages:
        return 0
    text_by_page = {int(no): (t or "") for no, t, _src in pages}
    stale = [no for no, t in text_by_page.items() if old.get(no) != text_sha(t)]
    if not stale:
        return 0
    page_inputs = [
        rules.PageInput(int(no), t, src or "text")
        for no, t, src in pages
        if int(no) in set(stale)
    ]
    node_titles = _node_title_chains(document_id, page_inputs)
    rows = rules.classify_pages(page_inputs, node_titles, [])
    # 只重建 stale 页（其余页行不动）
    keep = set(text_by_page) - set(stale)
    _persist(document_id, rows, text_by_page, keep=keep or None)
    return len(stale)


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------
@register("classify_material")
def classify_material_job(ctx: JobContext, payload: dict) -> dict:
    document_id = int(payload["document_id"])
    mode = str(payload.get("mode") or "full")
    use_llm = bool(payload.get("use_llm", True))
    ctx.start(total=3, message="读取页面")
    ctx.advance(message="规则层分类")
    result = classify_document(document_id, mode=mode, use_llm=use_llm)
    ctx.advance(message="完成")
    result["document_id"] = document_id
    return result


def schedule_classify(document_id: int, project_id: str | None = None, mode: str = "full") -> int:
    if not config.material_index_enabled():
        return 0
    return submit(
        "classify_material",
        {"document_id": document_id, "mode": mode, "use_llm": True},
        project_id=project_id,
    )


# ---------------------------------------------------------------------------
# 查询组装（API）
# ---------------------------------------------------------------------------
@dataclass
class MaterialIndex:
    document_id: int
    pages_total: int
    pages_classified: int
    stale_pages: list[int]
    by_category: dict[str, list[str]]  # category → ["p3", "p120-125", ...]
    segments: list[dict]
    classifier_version: str | None


def _ranges(pages: list[int]) -> list[str]:
    """[3,4,5,10] → ["p3-5", "p10"]（连续区间合并）。"""
    out: list[str] = []
    if not pages:
        return out
    ps = sorted(set(pages))
    start = prev = ps[0]
    for n in ps[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(f"p{start}" if start == prev else f"p{start}-{prev}")
        start = prev = n
    out.append(f"p{start}" if start == prev else f"p{start}-{prev}")
    return out


def index_for_document(document_id: int) -> MaterialIndex:
    with session_scope() as s:
        doc = s.get(Document, document_id)
        if doc is None:
            raise ValueError(f"文档不存在：{document_id}")
        pages = s.execute(
            select(DocumentPage.page_no, DocumentPage.text)
            .where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_no)
        ).all()
        tags = s.execute(
            select(MaterialTag)
            .where(MaterialTag.document_id == document_id)
            .order_by(MaterialTag.page_no, MaterialTag.seg_kind, MaterialTag.seg_index)
        ).scalars().all()
        for t in tags:
            s.expunge(t)

    text_by_page = {int(no): (t or "") for no, t in pages}
    old_sha = {int(t.page_no): t.text_sha for t in tags if t.seg_kind == "page"}
    stale = [no for no, t in text_by_page.items() if old_sha.get(no) != text_sha(t)]

    by_cat: dict[str, list[int]] = {c: [] for c in MATERIAL_CATEGORIES}
    segments: list[dict] = []
    seen_pages: set[int] = set()
    for t in tags:
        if t.seg_kind == "page":
            if t.category in by_cat:
                by_cat[t.category].append(int(t.page_no))
            seen_pages.add(int(t.page_no))
        else:
            segments.append(
                {
                    "page_no": int(t.page_no),
                    "seg_kind": t.seg_kind,
                    "seg_index": int(t.seg_index),
                    "category": t.category,
                    "tags": t.tags_json or [],
                    "method": t.method,
                    "confidence": t.confidence,
                    "basis": t.basis,
                    "bbox": t.bbox_json,
                }
            )
    versions = {t.classifier_version for t in tags if t.classifier_version}
    return MaterialIndex(
        document_id=document_id,
        pages_total=len(text_by_page),
        pages_classified=len(seen_pages),
        stale_pages=sorted(stale),
        by_category={c: _ranges(ps) for c, ps in by_cat.items()},
        segments=segments,
        classifier_version=sorted(versions)[-1] if versions else None,
    )


def index_for_bidder(bidder_id: int) -> list[MaterialIndex]:
    # 口径同 bid_text：以 documents.bidder_id 直连为准（BidderDocument 只承载 role 标注）
    with session_scope() as s:
        doc_ids = [
            int(r) for r in s.execute(
                select(Document.id).where(Document.bidder_id == bidder_id).order_by(Document.id)
            ).scalars().all()
        ]
    return [index_for_document(did) for did in doc_ids]
