"""投标人 API（只读展示 + 运维纠错兜底）。

2026-11-13 输入格式重构：投标人管理功能下线——投标人由项目包导入自动生成
（文件夹名 = 投标人名称），报价开标一览表自动抽取即生效。此文件只保留：
- 只读查询（结果页排名/报价展示依赖）；
- `POST /bidders/{id}/price/set` 人工纠错（无 UI，运维兜底，留痕）。

写入口（建档/上传/抽取/确认/改名/删除/资格预判）全部随导入链路收敛：
建档与上传走 bundle_import_service，抽取走 app.pricing.extractor。
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.deps import get_actor
from app.schemas.document import DocumentOut
from app.services import audit_service, bidder_service

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


class SetPriceBody(BaseModel):
    price_yuan: Decimal = Field(gt=0)
    reason: str = Field(min_length=1)


@router.get("/projects/{project_id}/bidders", response_model=list[BidderOut])
def list_bidders(project_id: str) -> list[BidderOut]:
    return [BidderOut.model_validate(b) for b in bidder_service.list_bidders(project_id)]


@router.get("/bidders/{bidder_id}", response_model=BidderOut)
def get_bidder(bidder_id: int) -> BidderOut:
    return BidderOut.model_validate(bidder_service.get_bidder(bidder_id))


@router.get("/bidders/{bidder_id}/documents", response_model=list[DocumentOut])
def list_bidder_documents(bidder_id: int) -> list[DocumentOut]:
    return [DocumentOut.model_validate(d) for d in bidder_service.bidder_documents(bidder_id)]


@router.get("/bidders/{bidder_id}/price")
def get_price(bidder_id: int) -> dict:
    """读取报价（纯读；抽取已在导入时完成，不再有同步补抽副作用）。"""
    bidder = bidder_service.get_bidder(bidder_id)
    return {
        "bidder_id": bidder.id,
        "bid_price": str(bidder.bid_price) if bidder.bid_price is not None else None,
        "bid_price_upper": bidder.bid_price_upper,
        "price_status": bidder.price_status,
        "breakdown": bidder.price_breakdown_json,
        "evidence_id": bidder.price_evidence_id,
    }


@router.post("/bidders/{bidder_id}/price/set", response_model=BidderOut)
def set_price_manual(bidder_id: int, body: SetPriceBody, actor: str = Depends(get_actor)) -> BidderOut:
    """人工录入/纠正报价（运维兜底，无 UI；直接进入已确认态并留痕）。"""
    bidder = bidder_service.set_price_manual(bidder_id, body.price_yuan, actor, body.reason)
    audit_service.log(bidder.project_id, actor, "bidder.price_set", "bidder", str(bidder_id),
                      {"price": str(body.price_yuan)})
    return BidderOut.model_validate(bidder)
