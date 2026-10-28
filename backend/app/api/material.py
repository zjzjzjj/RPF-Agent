"""材料分类索引 API（只读 + 手动重跑触发）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.deps import get_actor
from app.errors import NotFound
from app.material_index import service as material_index
from app.schemas.material import MaterialClassifyOut, MaterialIndexOut, MaterialSegmentOut
from app.services import audit_service, ingest_service

router = APIRouter(tags=["material-index"])


def _to_out(idx: material_index.MaterialIndex) -> MaterialIndexOut:
    return MaterialIndexOut(
        document_id=idx.document_id,
        pages_total=idx.pages_total,
        pages_classified=idx.pages_classified,
        stale_pages=idx.stale_pages,
        by_category=idx.by_category,
        segments=[MaterialSegmentOut(**seg) for seg in idx.segments],
        classifier_version=idx.classifier_version,
    )


@router.get("/documents/{document_id}/material-index", response_model=MaterialIndexOut)
def get_document_material_index(document_id: int) -> MaterialIndexOut:
    try:
        idx = material_index.index_for_document(document_id)
    except ValueError:
        raise NotFound("文档不存在") from None
    return _to_out(idx)


@router.get("/bidders/{bidder_id}/material-index", response_model=list[MaterialIndexOut])
def get_bidder_material_index(bidder_id: int) -> list[MaterialIndexOut]:
    try:
        indexes = material_index.index_for_bidder(bidder_id)
    except ValueError:
        raise NotFound("投标人不存在") from None
    return [_to_out(i) for i in indexes]


@router.post("/documents/{document_id}/classify", response_model=MaterialClassifyOut)
def classify_document(document_id: int, actor: str = Depends(get_actor)) -> MaterialClassifyOut:
    doc = ingest_service.get_document(document_id)
    if doc is None:
        raise NotFound("文档不存在")
    job_id = material_index.schedule_classify(document_id, project_id=doc.project_id, mode="full")
    audit_service.log(doc.project_id, actor, "document.classify", "document", str(document_id), {})
    return MaterialClassifyOut(document_id=document_id, job_id=job_id)
