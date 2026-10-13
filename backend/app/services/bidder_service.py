"""投标人服务：建档、报价抽取与人工确认（价格分输入的强制卡点）。"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from app.db.models import Bidder, BidderDocument, Document, ReviewRecord
from app.db.session import session_scope
from app.engine.facts import extract_bid_facts
from app.errors import NotFound, ValidationFailed


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


def bid_text(bidder_id: int) -> tuple[str, list[str], list[int]]:
    """该投标人全部投标文件的拼接文本 + 文档标题列表 + 文档 id 列表。"""
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
    return "\n".join(parts), titles, doc_ids


# ---------------------------------------------------------------------------
# 报价：抽取 → 人工确认（卡点 ③）
# ---------------------------------------------------------------------------
def extract_price(bidder_id: int) -> Bidder:
    """用确定性抽取器读报价（总价/大写/分项合计），落库 + 建证据引用。"""
    bidder = get_bidder(bidder_id)
    text, _titles, doc_ids = bid_text(bidder_id)
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
