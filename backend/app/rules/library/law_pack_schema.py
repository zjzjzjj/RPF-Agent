"""法规通用规则包（S-Law 固化版）契约：pack.json / rules/*.json 的强校验模型。

借鉴 coll_det 围串标规则包框架（一条规则一个文件 + 包元信息 + 法条登记），
把法规库制品固化为仓库内一等公民规则包。extra=forbid：包文件不得携带
契约之外的字段——制品升级带来新字段时，build 脚本的自检会当场失败，
逼着契约先行升级，而不是静默吞掉新字段。

字段与导入服务（services/law_import_service.py::_map_criterion）的消费字段
一一对应；loader 返回原始 dict（非 model_dump），保证与制品逐字段一致。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["qualification", "compliance", "price", "technical", "commercial"]
ScoreType = Literal["veto", "formula", "count", "match", "subjective"]
VetoConsequence = Literal["disqualify", "may_request", "discretionary"]
# 比 library/schema.py 的 CheckType 多一个 collusion_check：法规包 REJ 组的
# 串通核查项由 engine/collusion.py 预计算信号供给（engine/objective.run_check 已支持）。
CheckType = Literal[
    "document_present", "contains_phrase", "value_in_range", "date_valid",
    "math_relation", "cross_document_match", "count_at_least", "human_only",
    "collusion_check",
]
FormulaType = Literal["lowest_ratio", "benchmark_ratio", "deviation_deduct", "above_deduct"]
BenchmarkMethod = Literal["lowest", "average", "average_trimmed", "second_lowest", "average_below"]


class LawPackLevel(BaseModel):
    """主观档位（当前 87 条全为空，保留契约以防制品升级）。"""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=64)
    min: Decimal | None = None
    max: Decimal | None = None
    criteria: str = Field(min_length=1)
    quote: str | None = None


class LawPackRule(BaseModel):
    """单条法规规则（rules/<CODE>.json 的内容 = 制品 criterion 对象）。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64)
    category: Category
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    max_score: Decimal | None = None
    score_type: ScoreType
    is_veto: bool = False
    veto_consequence: VetoConsequence | None = None
    levels: list[LawPackLevel] = Field(default_factory=list)
    check_type: CheckType | None = None
    params: dict = Field(default_factory=dict)
    formula_type: FormulaType | None = None
    benchmark_method: BenchmarkMethod | None = None
    k: Decimal | None = None
    upper_bound: Decimal | None = None
    rounding: int | None = None
    count_per_item: Decimal | None = None
    quote: str | None = None        # 法条/招标文件原文（逐字）
    page_hint: int | None = None
    confidence: float | None = None
    source_node_id: str | None = None
    source_quote: str | None = None
    weight: Decimal | None = None   # 权重不在契约语义内，保留字段仅为对齐制品


class LawPackGeneratedFrom(BaseModel):
    """包的来源制品指纹（幂等与溯源的依据，不含时间戳）。"""

    model_config = ConfigDict(extra="forbid")

    artifact: str = Field(min_length=1)
    artifact_sha256_16: str = Field(min_length=16, max_length=16)
    law_nodes: int = Field(ge=0)
    non_law_criteria: int = Field(ge=0)


class LawPackMeta(BaseModel):
    """pack.json：包元信息。id/version/extends/scope 沿用 coll_det 语义。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=32)
    extends: str | None = None
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    scope: Literal["law", "bid-rigging", "industry"] = "law"
    source: Literal["LAW"] = "LAW"
    total_max: int | None = None
    status: Literal["draft", "confirmed"] = "draft"
    enabled: bool = True
    rule_count: int = Field(ge=1)
    external_refs: list[str] = Field(default_factory=list)
    generated_from: LawPackGeneratedFrom
