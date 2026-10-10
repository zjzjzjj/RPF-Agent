"""规则抽取契约：LLM 输出 → Pydantic 模型。

硬约束落在模型校验器里：
- 抽取值必须带逐字 quote（给不出原文 = null，不得编造）；
- 档位区间不得由模型发明（原文没给分值 → 编译器置 ambiguous）；
- 权重（weight）不在契约中——它永远不发给模型。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Category = Literal["qualification", "compliance", "price", "technical", "commercial"]
ScoreType = Literal["veto", "formula", "count", "match", "subjective"]
VetoConsequence = Literal["disqualify", "may_request", "discretionary"]


class ExtractedLevel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=64)
    min: Decimal | None = None
    max: Decimal | None = None
    criteria: str = Field(min_length=1)  # 档位判据原文
    quote: str | None = None


class ExtractedCriterion(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    code: str = Field(default="", max_length=64)  # 空则由编译器按序补
    category: Category
    name: str = Field(min_length=1, max_length=200)
    max_score: Decimal | None = None
    score_type: ScoreType
    is_veto: bool = False
    veto_consequence: VetoConsequence | None = None
    description: str | None = None
    levels: list[ExtractedLevel] = Field(default_factory=list)
    check_type: str | None = None
    params: dict = Field(default_factory=dict)
    formula_type: str | None = None
    benchmark_method: str | None = None
    k: Decimal | None = None
    upper_bound: Decimal | None = None
    rounding: int | None = None
    count_per_item: Decimal | None = None
    quote: str | None = None  # 招标文件原文（逐字）
    page_hint: int | None = None
    confidence: float | None = None

    @field_validator("page_hint", mode="before")
    @classmethod
    def _coerce_page_hint(cls, v: object) -> int | None:
        """模型常返回"第2页"/"2页"/"P3"等形态——提取数字即可，不必为此整包拒绝。"""
        import re as _re

        if v is None or v == "":
            return None
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v)
        if isinstance(v, str):
            m = _re.search(r"\d+", v)
            return int(m.group()) if m else None
        return None

    @field_validator("quote")
    @classmethod
    def _quote_not_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            return None
        return v


class RuleExtraction(BaseModel):
    """编译器一次调用的完整输出。"""

    criteria: list[ExtractedCriterion] = Field(default_factory=list)

    @field_validator("criteria")
    @classmethod
    def _non_empty_names(cls, v: list[ExtractedCriterion]) -> list[ExtractedCriterion]:
        return [c for c in v if c.name]
