"""文档与解析 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse

from app import config
from app.deps import get_actor
from app.errors import NotFound
from app.schemas.document import DocumentOut, DocumentUploadOut, NodeOut, PageOut
from app.services import audit_service, ingest_service

router = APIRouter(tags=["documents"])


@router.post("/projects/{project_id}/documents", response_model=DocumentUploadOut)
def upload_document(
    project_id: str,
    file: UploadFile = File(...),
    kind: str = Form("tender"),
    bidder_id: int | None = Form(None),
    data_class: str = Form("synthetic"),
    actor: str = Depends(get_actor),
) -> DocumentUploadOut:
    content = file.file.read()
    doc_id, job_id = ingest_service.save_upload(
        project_id=project_id,
        filename=file.filename or "upload",
        content=content,
        kind=kind,
        bidder_id=bidder_id,
        data_class=data_class,
    )
    audit_service.log(project_id, actor, "document.upload", "document", str(doc_id),
                      {"filename": file.filename, "kind": kind, "size": len(content)})
    return DocumentUploadOut(document_id=doc_id, parse_status="pending", job_id=job_id)


@router.get("/projects/{project_id}/documents", response_model=list[DocumentOut])
def list_documents(project_id: str) -> list[DocumentOut]:
    return [DocumentOut.model_validate(d) for d in ingest_service.list_documents(project_id)]


@router.get("/documents/{document_id}", response_model=DocumentOut)
def get_document(document_id: int) -> DocumentOut:
    doc = ingest_service.get_document(document_id)
    if doc is None:
        raise NotFound("文档不存在")
    return DocumentOut.model_validate(doc)


@router.get("/documents/{document_id}/pages", response_model=list[PageOut])
def get_pages(document_id: int, from_page: int = 1, to_page: int | None = None) -> list[PageOut]:
    pages = ingest_service.get_pages(document_id, from_page, to_page)
    return [PageOut.model_validate(p) for p in pages]


@router.get("/documents/{document_id}/pages/{page_no}/image")
def get_page_image(document_id: int, page_no: int, scale: float | None = None) -> FileResponse:
    doc = ingest_service.get_document(document_id)
    if doc is None:
        raise NotFound("文档不存在")
    from app.evidence.renderer import render_page_png

    path = render_page_png(doc.sha256, doc.stored_path, page_no, scale)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@router.get("/documents/{document_id}/nodes", response_model=list[NodeOut])
def get_nodes(document_id: int, parent_id: str | None = None) -> list[NodeOut]:
    nodes = ingest_service.get_nodes(document_id, parent_id)
    return [NodeOut.model_validate(n) for n in nodes]


@router.post("/documents/{document_id}/reparse", response_model=DocumentUploadOut)
def reparse_document(document_id: int, actor: str = Depends(get_actor)) -> DocumentUploadOut:
    doc = ingest_service.get_document(document_id)
    if doc is None:
        raise NotFound("文档不存在")
    from app.jobs.queue import submit

    job_id = submit("parse_document", {"document_id": document_id}, project_id=doc.project_id)
    audit_service.log(doc.project_id, actor, "document.reparse", "document", str(document_id), {})
    return DocumentUploadOut(document_id=document_id, parse_status=doc.parse_status, job_id=job_id)
