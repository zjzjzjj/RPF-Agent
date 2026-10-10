"""投标人 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.deps import get_actor
from app.errors import NotFound
from app.schemas.document import DocumentOut, DocumentUploadOut
from app.services import audit_service, bidder_service, ingest_service

router = APIRouter(tags=["bidders"])


class BidderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: str
    name: str
    unified_code: str | None
    bid_price: Decimal | None
    bid_price_upper: str | None
    price_breakdown_json: dict | None
    price_status: str
    price_evidence_id: int | None
    status: str


class BidderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    unified_code: str | None = None


class ConfirmBody(BaseModel):
    reason: str = Field(min_length=1)


class SetPriceBody(BaseModel):
    price_yuan: Decimal = Field(gt=0)
    reason: str = Field(min_length=1)


@router.post("/projects/{project_id}/bidders", response_model=BidderOut)
def create_bidder(project_id: str, body: BidderCreate, actor: str = Depends(get_actor)) -> BidderOut:
    bidder = bidder_service.create_bidder(project_id, body.name, body.unified_code)
    audit_service.log(project_id, actor, "bidder.create", "bidder", str(bidder.id), {"name": body.name})
    return BidderOut.model_validate(bidder)


@router.get("/projects/{project_id}/bidders", response_model=list[BidderOut])
def list_bidders(project_id: str) -> list[BidderOut]:
    return [BidderOut.model_validate(b) for b in bidder_service.list_bidders(project_id)]


@router.get("/bidders/{bidder_id}", response_model=BidderOut)
def get_bidder(bidder_id: int) -> BidderOut:
    return BidderOut.model_validate(bidder_service.get_bidder(bidder_id))


@router.post("/bidders/{bidder_id}/documents", response_model=DocumentUploadOut)
def upload_bid_document(
    bidder_id: int,
    file: UploadFile = File(...),
    role: str = Form("main"),
    actor: str = Depends(get_actor),
) -> DocumentUploadOut:
    bidder = bidder_service.get_bidder(bidder_id)
    content = file.file.read()
    doc_id, job_id = ingest_service.save_upload(
        project_id=bidder.project_id,
        filename=file.filename or "upload",
        content=content,
        kind="bid",
        bidder_id=bidder_id,
    )
    from app.db.session import session_scope
    from app.db.models import BidderDocument

    with session_scope() as s:
        s.add(BidderDocument(bidder_id=bidder_id, document_id=doc_id, role=role))
    audit_service.log(bidder.project_id, actor, "bidder.upload_doc", "bidder", str(bidder_id),
                      {"document_id": doc_id, "role": role})
    return DocumentUploadOut(document_id=doc_id, parse_status="pending", job_id=job_id)


@router.get("/bidders/{bidder_id}/documents", response_model=list[DocumentOut])
def list_bidder_documents(bidder_id: int) -> list[DocumentOut]:
    return [DocumentOut.model_validate(d) for d in bidder_service.bidder_documents(bidder_id)]


@router.get("/bidders/{bidder_id}/price")
def get_price(bidder_id: int) -> dict:
    bidder = bidder_service.get_bidder(bidder_id)
    if bidder.price_status == "extracted" or bidder.bid_price is None:
        bidder = bidder_service.extract_price(bidder_id)
    return {
        "bidder_id": bidder.id,
        "bid_price": str(bidder.bid_price) if bidder.bid_price is not None else None,
        "bid_price_upper": bidder.bid_price_upper,
        "price_status": bidder.price_status,
        "breakdown": bidder.price_breakdown_json,
        "evidence_id": bidder.price_evidence_id,
    }


@router.post("/bidders/{bidder_id}/price/confirm", response_model=BidderOut)
def confirm_price(bidder_id: int, body: ConfirmBody, actor: str = Depends(get_actor)) -> BidderOut:
    bidder = bidder_service.confirm_price(bidder_id, actor, body.reason)
    audit_service.log(bidder.project_id, actor, "bidder.price.confirm", "bidder", str(bidder_id),
                      {"price": str(bidder.bid_price)})
    return BidderOut.model_validate(bidder)


@router.post("/bidders/{bidder_id}/price/set", response_model=BidderOut)
def set_price_manual(bidder_id: int, body: SetPriceBody, actor: str = Depends(get_actor)) -> BidderOut:
    """人工录入/纠正报价（抽取失败或抽错时的兜底，直接进入已确认态并留痕）。"""
    bidder = bidder_service.set_price_manual(bidder_id, body.price_yuan, actor, body.reason)
    audit_service.log(bidder.project_id, actor, "bidder.price.manual", "bidder", str(bidder_id),
                      {"price": str(bidder.bid_price)})
    return BidderOut.model_validate(bidder)


@router.get("/bidders/{bidder_id}/qualification-preview")
def qualification_preview(bidder_id: int) -> dict:
    """对当前规则包（冻结或草稿）做一次只读的资格/符合性预判。"""
    from app.engine.facts import extract_bid_facts
    from app.services import rule_service, scoring_service

    bidder = bidder_service.get_bidder(bidder_id)
    pkg = rule_service.current_package(bidder.project_id)
    if pkg is None:
        raise NotFound("项目还没有规则包")
    _snapshot, criteria = scoring_service._snapshot_criteria(pkg.id)
    text, _titles, _docs = bidder_service.bid_text(bidder_id)
    facts = extract_bid_facts(text)
    results = []
    for c in criteria:
        if c["score_type"] != "veto":
            continue
        r = scoring_service._eval_veto(c, facts)  # type: ignore[arg-type]
        results.append(
            {"code": c["code"], "name": c["name"], "verdict": r["verdict"], "message": r["message"]}
        )
    return {"bidder_id": bidder_id, "package_version": pkg.version, "items": results}
