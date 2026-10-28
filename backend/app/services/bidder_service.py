"""投标人服务：建档、报价抽取与人工确认（价格分输入的强制卡点）。"""
from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, func, select

from app.db.models import Bidder, BidderDocument, Document, EvidenceCitation, ReviewRecord, ScoreRecord
from app.db.session import session_scope
from app.engine.facts import extract_bid_facts
from app.errors import Conflict, NotFound, ValidationFailed
from app.jobs.handlers import register
from app.jobs.queue import JobContext

log = logging.getLogger("bid_eval.bidder")


def create_bidder(project_id: str, name: str, unified_code: str | None = None) -> Bidder:
    with session_scope() as s:
        bidder = Bidder(project_id=project_id, name=name, unified_code=unified_code)
        s.add(bidder)
        s.flush()
        s.expunge(bidder)
        return bidder


def list_bidders(project_id: str) -> list[Bidder]:
    with session_scope() as s:
        rows = s.execute(
            select(Bidder).where(Bidder.project_id == project_id).order_by(Bidder.id)
        ).scalars().all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def get_bidder(bidder_id: int) -> Bidder:
    with session_scope() as s:
        bidder = s.get(Bidder, bidder_id)
        if bidder is None:
            raise NotFound("投标人不存在")
        s.expunge(bidder)
        return bidder


def bidder_documents(bidder_id: int) -> list[Document]:
    """该投标人的全部文件。

    以 documents.bidder_id 直连为准（上传即绑定）；BidderDocument 仅承载 role 标注，
    两条路径都要能查到，否则会出现"上传成功但评审看不到"的静默空洞。
    """
    with session_scope() as s:
        rows = s.execute(
            select(Document).where(Document.bidder_id == bidder_id).order_by(Document.id)
        ).scalars().all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def bid_text(
    bidder_id: int, on_doc: Callable[[Document], None] | None = None
) -> tuple[str, list[str], list[int]]:
    """该投标人全部投标文件的拼接文本 + 文档标题列表 + 文档 id 列表。

    on_doc 逐文档回调（后台任务的天然进度点：大标书拼接是耗时主项）。
    """
    from app.db.models import DocumentPage

    docs = bidder_documents(bidder_id)
    parts: list[str] = []
    titles: list[str] = []
    doc_ids: list[int] = []
    for doc in docs:
        if doc.parse_status not in ("parsed", "degraded"):
            continue
        doc_ids.append(doc.id)
        with session_scope() as s:
            pages = s.execute(
                select(DocumentPage.text)
                .where(DocumentPage.document_id == doc.id)
                .order_by(DocumentPage.page_no)
            ).scalars().all()
        parts.append("\n".join(pages))
        titles.append(doc.original_filename)
        if on_doc is not None:
            on_doc(doc)
    return "\n".join(parts), titles, doc_ids


# ---------------------------------------------------------------------------
# 报价：抽取 → 人工确认（卡点 ③）
# ---------------------------------------------------------------------------
def extract_price(bidder_id: int) -> Bidder:
    """用确定性抽取器读报价（总价/大写/分项合计），落库 + 建证据引用。

    legacy 同步路径：`GET /bidders/{id}/price` 走这里（多处测试与脚本依赖其副作用）。
    新前端已改用 `POST /bidders/{id}/price/extract`（后台 job，带进度与取消）。
    """
    bidder = get_bidder(bidder_id)
    text, _titles, doc_ids = bid_text(bidder_id)
    text = _price_domain_text(bidder_id, text, doc_ids)
    return _extract_price_impl(bidder, text, doc_ids)


def _price_domain_text(bidder_id: int, full_text: str, doc_ids: list[int]) -> str:
    """报价类材料页文本（material_index 的 price 类标签）——抽价硬过滤检索域。

    无有效价格类页（分类缺失/判丢/未启用）→ 回退全量文本（安全阀：
    分类故障不得打死报价抽取）。抽取只读返回域，引文同域可查。
    """
    from app import config as _config

    if not _config.material_index_enabled() or not doc_ids:
        return full_text
    try:
        from sqlalchemy import select as _select

        from app.db.models import DocumentPage as _DP
        from app.db.models import MaterialTag
        from app.material_index.service import text_sha as _tsha

        with session_scope() as s:
            price_pages = [
                int(p) for p in s.execute(
                    _select(MaterialTag.page_no).where(
                        MaterialTag.document_id.in_(doc_ids),
                        MaterialTag.seg_kind == "page",
                        MaterialTag.category == "price",
                    )
                ).scalars().all()
            ]
            if not price_pages:
                return full_text
            parts: list[str] = []
            for d_id in doc_ids:
                for pno, ptext, psha in s.execute(
                    _select(_DP.page_no, _DP.text, MaterialTag.text_sha)
                    .join(MaterialTag, (MaterialTag.document_id == _DP.document_id)
                          & (MaterialTag.page_no == _DP.page_no)
                          & (MaterialTag.seg_kind == "page"), isouter=True)
                    .where(_DP.document_id == d_id, _DP.page_no.in_(price_pages))
                    .order_by(_DP.page_no)
                ):
                    if psha and _tsha(ptext or "") != psha:
                        continue  # 标签已过期，宁缺勿错
                    parts.append(ptext or "")
            text = "\n".join(p for p in parts if p.strip())
        return text or full_text  # 空集安全阀
    except Exception:  # noqa: BLE001 —— 分类是增强，不阻塞抽价
        return full_text


def _extract_price_impl(bidder: Bidder, text: str, doc_ids: list[int]) -> Bidder:
    """抽取主体（不碰文本拼接，便于后台任务在拼接阶段上报进度）。"""
    bidder_id = bidder.id
    facts = extract_bid_facts(text)

    conflict = (
        facts.bid_price is not None
        and facts.bid_price_upper_value is not None
        and facts.bid_price != facts.bid_price_upper_value
    ) or (
        facts.bid_price is not None
        and facts.price_items_total is not None
        and facts.bid_price != facts.price_items_total
    )

    evidence_id = None
    if doc_ids and facts.bid_price is not None:
        from app.evidence.verifier import create_citation

        quote = _find_price_quote(text, facts.bid_price)
        if quote:
            evidence_id = create_citation(
                doc_ids[0], quote, role="price", created_by="engine"
            )

    with session_scope() as s:
        bidder = s.get(Bidder, bidder_id)
        bidder.bid_price = facts.bid_price
        bidder.bid_price_upper = facts.bid_price_upper
        bidder.price_breakdown_json = {
            "items_total": str(facts.price_items_total) if facts.price_items_total else None,
            "upper_value": str(facts.bid_price_upper_value) if facts.bid_price_upper_value else None,
            "conflict": conflict,
        }
        bidder.price_status = "conflict" if conflict else "extracted"
        bidder.price_evidence_id = evidence_id
        s.add(bidder)
        s.flush()
        s.expunge(bidder)
        return bidder


@register("extract_price")
def extract_price_job(ctx: JobContext, payload: dict) -> dict:
    """后台抽取报价：逐文档上报进度（大标书的拼接是耗时主项）。"""
    bidder_id = int(payload["bidder_id"])
    bidder = get_bidder(bidder_id)
    docs = [d for d in bidder_documents(bidder_id) if d.parse_status in ("parsed", "degraded")]
    ctx.start(total=max(1, len(docs) + 2), message="读取投标文件")

    def _on_doc(doc: Document) -> None:
        ctx.advance(message=f"读取 {doc.original_filename}")

    text, _titles, doc_ids = bid_text(bidder_id, on_doc=_on_doc)
    ctx.advance(message="识别投标报价")
    updated = _extract_price_impl(bidder, _price_domain_text(bidder_id, text, doc_ids), doc_ids)
    ctx.advance(message="建立证据引用")
    return {
        "bidder_id": bidder_id,
        "price_status": updated.price_status,
        "bid_price": str(updated.bid_price) if updated.bid_price is not None else None,
    }


def _find_price_quote(text: str, price: Decimal | None = None) -> str | None:
    """从文本中找到包含报价的整行（作为证据引文）。

    “投标总价”行优先；兜底找含报价数字本身（裸/千分位两种写法）的整行——
    开标一览表跨行断裂形态下，数据行（如 `2401143 2545211.58 郑峪嘉`）是唯一证据。
    """
    for line in text.split("\n"):
        if "投标总价" in line and any(ch.isdigit() for ch in line):
            return line.strip()
    for line in text.split("\n"):
        if "投标总价人民币" in line:
            return line.strip()
    if price is not None:
        raw = f"{price:f}".rstrip("0").rstrip(".")
        grouped = f"{price:,.2f}"
        for line in text.split("\n"):
            if raw in line or grouped in line:
                return line.strip()
    return None


def confirm_price(bidder_id: int, actor: str, reason: str) -> Bidder:
    """人工确认报价。冲突状态也允许确认（人工裁决本身就是确认），但理由必填。"""
    if not reason.strip():
        raise ValidationFailed("确认报价必须填写理由")
    with session_scope() as s:
        bidder = s.get(Bidder, bidder_id)
        if bidder is None:
            raise NotFound("投标人不存在")
        if bidder.bid_price is None:
            raise ValidationFailed("该投标人尚未抽取到报价，无法确认；请使用人工录入接口")
        old_status = bidder.price_status
        bidder.price_status = "confirmed"
        s.add(bidder)
        s.add(
            ReviewRecord(
                subject_type="price", subject_id=str(bidder_id), action="confirm",
                actor=actor, reason=reason,
                old_value_json={"price_status": old_status, "price": str(bidder.bid_price)},
                new_value_json={"price_status": "confirmed"},
            )
        )
        s.flush()
        s.expunge(bidder)
        return bidder


def set_price_manual(bidder_id: int, price_yuan: Decimal, actor: str, reason: str) -> Bidder:
    """人工录入/纠正报价（抽取失败或抽错时的兜底，直接进入已确认态并留痕）。"""
    if not reason.strip():
        raise ValidationFailed("人工录入报价必须填写理由（价格分的最敏感输入）")
    if price_yuan <= 0:
        raise ValidationFailed("报价必须为正数")
    with session_scope() as s:
        bidder = s.get(Bidder, bidder_id)
        if bidder is None:
            raise NotFound("投标人不存在")
        old = {"price": str(bidder.bid_price), "price_status": bidder.price_status}
        bidder.bid_price = price_yuan
        bidder.price_status = "confirmed"
        s.add(bidder)
        s.add(
            ReviewRecord(
                subject_type="price", subject_id=str(bidder_id), action="override",
                actor=actor, reason=reason,
                old_value_json=old,
                new_value_json={"price": str(price_yuan), "price_status": "confirmed"},
            )
        )
        s.flush()
        s.expunge(bidder)
        return bidder


def rename_bidder(bidder_id: int, name: str, actor: str, reason: str) -> Bidder:
    """人工纠正投标人名称（上传时用占位名，评审前按投标文件封面校准，全程留痕）。"""
    if not reason.strip():
        raise ValidationFailed("更名必须填写理由")
    name = name.strip()
    if not name or len(name) > 255:
        raise ValidationFailed("投标人名称不能为空且不超过 255 字符")
    with session_scope() as s:
        bidder = s.get(Bidder, bidder_id)
        if bidder is None:
            raise NotFound("投标人不存在")
        old = bidder.name
        if old == name:
            return bidder
        bidder.name = name
        s.add(bidder)
        s.add(
            ReviewRecord(
                subject_type="bidder", subject_id=str(bidder_id), action="rename",
                actor=actor, reason=reason,
                old_value_json={"name": old},
                new_value_json={"name": name},
            )
        )
        s.flush()
        s.expunge(bidder)
        return bidder


def delete_bidder(bidder_id: int, actor: str) -> dict:
    """删除投标人及其投标文件；不存在抛 NotFound。

    删除顺序（照 delete_project 的依赖序思路，SQLite RESTRICT 外键真实生效）：
      1 evidence_citations（document_id 指向本投标人文档；其余引用随 documents 级联）
      2 bidder_documents（role 标注表）
      3 documents（bidder_id RESTRICT；其 pages/nodes 级联）
      4 bidders 本体

    **已有评分记录则拒绝（409）**：单删一家会让评分矩阵/报告引用不存在的投标人，
    破坏历史口径一致性。整项目删除会把 evaluation_runs 一并清掉，故不受此限。
    review_records / audit_logs 历史保留（全局合规留痕）。物理文件在事务提交后删除，
    失败仅记日志。
    """
    files_to_remove: list[Path] = []
    counts: dict[str, int] = {}

    with session_scope() as s:
        bidder = s.get(Bidder, bidder_id)
        if bidder is None:
            raise NotFound("投标人不存在")
        project_id = bidder.project_id

        n_scored = s.execute(
            select(func.count()).select_from(ScoreRecord).where(ScoreRecord.bidder_id == bidder_id)
        ).scalar_one()
        if n_scored:
            raise Conflict(
                f"该投标人已有 {n_scored} 条评分记录，删除会使历史评分与报告口径不一致；"
                "请先删除相关评分运行，或删除整个项目"
            )

        doc_ids = [
            r[0]
            for r in s.execute(select(Document.id).where(Document.bidder_id == bidder_id)).all()
        ]
        files_to_remove = [
            Path(r[0])
            for r in s.execute(
                select(Document.stored_path).where(Document.bidder_id == bidder_id)
            ).all()
            if r[0]
        ]

        if doc_ids:
            counts["evidence_citations"] = int(
                s.execute(
                    delete(EvidenceCitation).where(EvidenceCitation.document_id.in_(doc_ids))
                ).rowcount
                or 0
            )
        counts["bidder_documents"] = int(
            s.execute(
                delete(BidderDocument).where(BidderDocument.bidder_id == bidder_id)
            ).rowcount
            or 0
        )
        counts["documents"] = int(
            s.execute(delete(Document).where(Document.bidder_id == bidder_id)).rowcount or 0
        )
        s.delete(bidder)
        counts["bidders"] = 1

    from app.services import audit_service

    audit_service.log(project_id, actor, "bidder.delete", "bidder", str(bidder_id), counts)

    for f in files_to_remove:
        try:
            Path(f).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("删除投标文件失败：%s（%s）", f, exc)

    return counts
