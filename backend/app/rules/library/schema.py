"""通用评分规则库（S0）：模板 JSON 契约 + 强校验。

对齐既有 Criterion/CriterionLevel/CriterionCheck/PriceFormula 契约（rules/model.py、
engine 词汇表），使实例化后的草稿包能直接通过既有确定性校验器。
设计稿：docs/通用评分规则库设计方案.md §3.2。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.rules.validators import (
    _check_level_closure,
    _check_level_monotonic,
)

Category = Literal["qualification", "compliance", "price", "technical", "commercial"]
ScoreType = Literal["veto", "formula", "count", "match", "subjective"]
VetoConsequence = Literal["disqualify", "may_request", "discretionary"]
CheckType = Literal[
    "document_present", "contains_phrase", "value_in_range", "date_valid",
    "math_relation", "cross_document_match", "count_at_least", "human_only",
]
FormulaType = Literal["lowest_ratio", "benchmark_ratio", "deviation_deduct", "above_deduct"]
BenchmarkMethod = Literal["lowest", "average", "average_trimmed", "second_lowest", "average_below"]
OnFail = Literal["pass", "recommend_reject", "need_human"]

# template_code 的 T<分类号>.<序号> 与 category 的对应（设计稿 §3.2）
_CATEGORY_NUMBER = {
    "qualification": "1",
    "compliance": "2",
    "price": "3",
    "technical": "4",
    "commercial": "5",
}


class TemplateLevel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=64)
    min: Decimal | None = None
    max: Decimal | None = None
    criteria: str = Field(min_length=1)


class TemplateCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_type: CheckType
    field: str | None = None
    params: dict = Field(default_factory=dict)
    on_fail: OnFail = "need_human"


class TemplateFormula(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formula_type: FormulaType
    benchmark_method: BenchmarkMethod
    k: Decimal
    upper_bound: Decimal | None = None
    lower_bound: Decimal | None = None
    rounding: int = 2
    trim_ratio: Decimal | None = None
    deduct_rate: Decimal | None = None


class TemplateCriterion(BaseModel):
    """模板评分项。extra=forbid：模板不得携带模型不认识的字段（词汇表契约）。"""

    model_config = ConfigDict(extra="forbid")

    template_code: str = Field(max_length=64)
    category: Category
    name: str = Field(min_length=1, max_length=200)
    score_type: ScoreType
    is_veto: bool = False
    veto_consequence: VetoConsequence | None = None
    max_score: Decimal | None = None
    description: str | None = None
    levels: list[TemplateLevel] = Field(default_factory=list)
    checks: list[TemplateCheck] = Field(default_factory=list)
    formula: TemplateFormula | None = None
    weight: None = None  # 权重永不进模板（也永不进 prompt）
    essential: bool = False
    hint: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "TemplateCriterion":
        errors: list[str] = []

        # 否决项语义：is_veto ⇔ score_type=='veto'，且必须带后果、不得给分
        if self.is_veto != (self.score_type == "veto"):
            errors.append(f"{self.template_code} is_veto 与 score_type 不一致")
        if self.is_veto and not self.veto_consequence:
            errors.append(f"{self.template_code} 否决项必须声明 veto_consequence")
        if self.is_veto and self.max_score is not None:
            errors.append(f"{self.template_code} 否决项不得设置 max_score（VETO_SCORED）")
        if not self.is_veto and self.max_score is None:
            errors.append(f"{self.template_code} 非否决项必须设置 max_score")

        # 主观项：档位必填；其余类型不得带档位
        if self.score_type == "subjective":
            if not self.levels:
                errors.append(f"{self.template_code} 主观项必须提供档位 levels")
            if self.checks:
                errors.append(f"{self.template_code} 主观项不应携带客观检查 checks")
        else:
            if self.levels:
                errors.append(f"{self.template_code} 只有主观项允许档位 levels")

        # 价格项：category=price ⇔ formula 必填；非价格项不得带 formula
        if self.category == "price":
            if self.formula is None:
                errors.append(f"{self.template_code} 价格项必须提供 formula")
            if self.score_type != "formula":
                errors.append(f"{self.template_code} 价格项 score_type 必须是 formula")
        elif self.formula is not None:
            errors.append(f"{self.template_code} 非价格项不得携带 formula")

        # 客观检查原语只允许出现在 veto/count/match 上
        if self.checks and self.score_type not in ("veto", "count", "match"):
            errors.append(f"{self.template_code} 客观检查只允许 veto/count/match 携带")

        # 档位区间直接复用既有确定性校验器（保证实例化后不被卡死）
        if self.score_type == "subjective":
            snapshot = {
                "code": self.template_code,
                "name": self.name,
                "score_type": "subjective",
                "max_score": self.max_score,
                "levels": [
                    {"label": lv.label, "min": lv.min, "max": lv.max,
                     "criteria": lv.criteria}
                    for lv in self.levels
                ],
            }
            for finding in _check_level_closure(snapshot) + _check_level_monotonic(snapshot):
                errors.append(f"{self.template_code} {finding.code}: {finding.message}")

        if errors:
            raise ValueError("；".join(errors))
        return self


class RuleTemplateFile(BaseModel):
    """模板文件契约。template_id 必须等于文件名（loader 强校验）。"""

    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1)
    method: str = Field(default="comprehensive")
    applicable: dict = Field(default_factory=dict)
    library_version: str = Field(default="1.0.0")
    notes: str | None = None
    criteria: list[TemplateCriterion] = Field(min_length=1)

    @model_validator(mode="after")
    def _template_consistent(self) -> "RuleTemplateFile":
        errors: list[str] = []

        # template_code 唯一 + 分类号前缀正确（设计稿：编号稳定锚）
        seen: dict[str, str] = {}
        for c in self.criteria:
            if c.template_code in seen:
                errors.append(f"template_code 重复：{c.template_code}（{seen[c.template_code]} 与 {c.name}）")
            seen[c.template_code] = c.name
            parts = c.template_code.split(".")
            if len(parts) != 2 or not parts[0].startswith("T") or not parts[1].isdigit():
                errors.append(f"{c.template_code} 格式必须为 T<分类号>.<序号>")
                continue
            number = parts[0][1:]
            if number != _CATEGORY_NUMBER.get(c.category):
                errors.append(
                    f"{c.template_code} 分类号 {number} 与 category={c.category} 不符"
                    f"（应为 T{_CATEGORY_NUMBER.get(c.category)}）"
                )

        # 非否决项满分合计必须 = 100（保证实例化后 TOTAL_SCORE 通过）
        scored = [c for c in self.criteria if not c.is_veto]
        if scored:
            total = sum((c.max_score or Decimal(0)) for c in scored)
            if total != Decimal("100"):
                errors.append(f"非否决项满分合计 {total} ≠ 100（当前 {[str(c.max_score) for c in scored]}）")

        if errors:
            raise ValueError("；".join(errors))
        return self