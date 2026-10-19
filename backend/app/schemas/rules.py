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
    origin: str = "document"
    template_code: str | None = None
    template_hash: str | None = None
    library_version: str | None = None
    override_json: dict | None = None
    signoff_json: dict | None = None
    p_source: str | None = None
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
    origin: str = "document"
    template_id: str | None = None
    template_hash: str | None = None
    library_version: str | None = None
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
    mode: str = "replace"  # replace|merge（S1：merge = 产出临时编译包供逐条覆盖）
    merge_target_id: int | None = None  # merge 模式指向的模板包


class InstantiateBody(BaseModel):
    template_id: str = Field(min_length=1)


class LawImportBody(BaseModel):
    """法律规则包制品 JSON 原样投递（顶层 = {package, criteria}，其余键忽略）。"""

    model_config = ConfigDict(extra="ignore")

    package: dict = Field(default_factory=dict)
    criteria: list[dict] = Field(default_factory=list)


class LawPackSummaryOut(BaseModel):
    """仓库内固化的法规规则包（rules/library/law_packs/）目录项。"""

    id: str
    name: str
    version: str
    rule_count: int
    scope: str | None = None
    total_max: float | None = None


class LawPackImportBody(BaseModel):
    """按 pack_id 导入仓库内固化的法规规则包（无需前端持有 87 条 JSON）。"""

    pack_id: str = Field(default="general-law", min_length=1)


class DroppedNodeOut(BaseModel):
    code: str
    node_id: str


class LawImportOut(BaseModel):
    package_id: int
    version: int
    created: bool
    imported: int
    skipped_codes: list[str]
    dropped_nodes: list[DroppedNodeOut]
    validations: list[ValidationOut]


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
    # 引文改正：模型抽短/抽错时人工修正原文依据；改动会同步到证据引用并重新核验
    source_quote: str | None = None
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
    # 扩展参数（表结构的既定扩展位）：如不对称扣分的
    # {"deduct_rate_down": "0.4", "deduct_rate_up": "0.8"}。
    params_json: dict[str, Any] | None = None
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


# ---------------------------------------------------------------------------
# 通用评分规则库（S0）
# ---------------------------------------------------------------------------
class TemplateLevelOut(BaseModel):
    label: str
    min: Decimal | None
    max: Decimal | None
    criteria: str


class TemplateCheckOut(BaseModel):
    check_type: str
    field: str | None = None
    params: dict
    on_fail: str


class TemplateFormulaOut(BaseModel):
    formula_type: str
    benchmark_method: str
    k: Decimal
    upper_bound: Decimal | None = None
    lower_bound: Decimal | None = None
    rounding: int = 2
    trim_ratio: Decimal | None = None
    deduct_rate: Decimal | None = None


class TemplateCriterionOut(BaseModel):
    template_code: str
    category: str
    name: str
    score_type: str
    is_veto: bool = False
    veto_consequence: str | None = None
    max_score: Decimal | None = None
    description: str | None = None
    essential: bool = False
    hint: str | None = None
    levels: list[TemplateLevelOut] = []
    checks: list[TemplateCheckOut] = []
    formula: TemplateFormulaOut | None = None


class TemplateSummaryOut(BaseModel):
    template_id: str
    name: str
    method: str
    library_version: str
    criteria_count: int
    categories_summary: dict[str, int]


class TemplateDetailOut(TemplateSummaryOut):
    applicable: dict
    notes: str | None = None
    criteria: list[TemplateCriterionOut]


class SignoffBody(BaseModel):
    reason: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# 逐条覆盖（S1）：匹配提案
# ---------------------------------------------------------------------------
class MatchPairOut(BaseModel):
    pair_id: str
    match_method: str  # exact|deterministic|llm
    similarity: float
    confidence: float | None = None
    rationale: str | None = None
    template: dict
    document: dict


class MatchProposalOut(BaseModel):
    merge_package_id: int
    target_package_id: int
    pairs: list[MatchPairOut]
    unmatched_template: list[dict]
    unmatched_document: list[dict]


class MatchDecision(BaseModel):
    action: str  # override|drop_template|append_document|reject
    pair_id: str | None = None
    template_criterion_id: int | None = None
    document_criterion_id: int | None = None
    reason: str = Field(min_length=1)


class ResolveProposalBody(BaseModel):
    decisions: list[MatchDecision] = Field(min_length=1)


class CoverageOut(BaseModel):
    total: int
    document_backed: int
    template_fallback_signed: int
    template_fallback_pending: int
    retired: int
    manual: int
    law: int = 0
    by_category: dict[str, dict]
    essential_uncovered: list[str]
    price_covered: bool
    veto_covered_ratio: float
