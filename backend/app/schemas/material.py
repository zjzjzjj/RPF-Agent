"""材料分类索引契约。"""
from __future__ import annotations

from pydantic import BaseModel


class MaterialSegmentOut(BaseModel):
    page_no: int
    seg_kind: str  # page|table|image|text
    seg_index: int
    category: str
    tags: list[str] = []
    method: str
    confidence: float
    basis: str | None = None
    bbox: dict | None = None


class MaterialIndexOut(BaseModel):
    document_id: int
    pages_total: int
    pages_classified: int
    stale_pages: list[int] = []
    by_category: dict[str, list[str]] = {}
    segments: list[MaterialSegmentOut] = []
    classifier_version: str | None = None


class MaterialClassifyOut(BaseModel):
    document_id: int
    job_id: int
