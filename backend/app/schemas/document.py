"""文档契约。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DocumentKind = Literal["tender", "bid", "addendum", "attachment"]
DataClass = Literal["synthetic", "redacted_sample", "confidential"]


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: str
    bidder_id: int | None
    kind: str
    original_filename: str
    sha256: str
    page_count: int | None
    parse_status: str
    parse_engine: str | None
    warnings_json: list | None
    data_class: str
    created_at: datetime


class DocumentUploadOut(BaseModel):
    document_id: int
    parse_status: str
    job_id: int


class PageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    page_no: int
    text: str
    width: float
    height: float
    page_approximate: bool
    has_text_layer: bool


class NodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    node_id: str
    parent_node_id: str | None
    kind: str
    number: str | None
    label: str | None
    title: str | None
    page: int
    char_start: int
    char_end: int
    is_coarse: bool
    depth: int
