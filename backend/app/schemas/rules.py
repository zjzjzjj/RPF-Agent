"""规则契约。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LevelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    level_label: str
    score_min: Decimal
    score_max: Decimal
    description: str
    source_quote: str | None


class CriterionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    package_id: int
    code: str
    category: str
    name: str
    description: str | None
    max_score: Decimal | None
    weight: Decimal | None
    score_type: str
    is_veto: bool
    veto_consequence: str | None
    required_evidence: str | None
    source_node_id: str | None
    source_page: int | None
    source_quote: str | None
    status: str
    edit_json: dict | None
    confidence: float | None
    sort_order: int


class CriterionDetailOut(CriterionOut):
    levels: list[LevelOut] = []
    checks: list[dict] = []


class PackageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: str
    version: int
    status: str
    content_hash: str | None
    parent_version_id: int | None
    source_document_id: int | None
    model: str | None
    compiled_by: str | None
    confirmed_by: str | None
    confirmed_at: datetime | None
    frozen_by: str | None
    frozen_at: datetime | None


class PackageDetailOut(PackageOut):
    criteria: list[CriterionDetailOut] = []
    validations: list[dict] = []


class ValidationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    severity: str
    subject_type: str | None
    subject_id: int | None
    message: str
    hint: str | None


class CompileRequest(BaseModel):
    document_id: int
    provider: str | None = None
    model: str | None = None  # 前端可选模型（模型目录内的 id）


class CriterionPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    max_score: Decimal | None = None
    weight: Decimal | None = None
    category: str | None = None
    score_type: str | None = None
    is_veto: bool | None = None
    veto_consequence: str | None = None
    required_evidence: str | None = None
    reason: str = Field(min_length=1)


class LevelsBody(BaseModel):
    levels: list[dict[str, Any]]
    reason: str = Field(min_length=1)


class ReasonBody(BaseModel):
    reason: str = Field(min_length=1)


class PriceFormulaPatch(BaseModel):
    """价格公式的人工补全/修正（缺参时 confirm/freeze 会被校验阻断，这里是唯一出口）。"""

    formula_type: str | None = None
    benchmark_method: str | None = None
    trim_ratio: Decimal | None = None
    k: Decimal | None = None
    deduct_rate: Decimal | None = None
    upper_bound: Decimal | None = None
    lower_bound: Decimal | None = None
    rounding: int | None = None
    exclude_invalid: bool | None = None
    tax_basis: str | None = None
    reason: str = Field(min_length=1)


class NewCriterionBody(BaseModel):
    category: str
    name: str
    score_type: str
    max_score: Decimal | None = None
    weight: Decimal | None = None
    description: str | None = None
    is_veto: bool = False
    veto_consequence: str | None = None
    source_quote: str | None = None
    source_node_id: str | None = None
    reason: str = Field(min_length=1)


class DiffOut(BaseModel):
    added: list[dict]
    removed: list[dict]
    changed: list[dict]
