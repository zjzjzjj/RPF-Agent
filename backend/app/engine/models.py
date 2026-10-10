"""评分引擎共享模型：纯数据结构，无 DB 依赖（可独立单测）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

# ---------------------------------------------------------------------------
# 价格分
# ---------------------------------------------------------------------------
@dataclass
class PriceBid:
    bidder_id: int
    name: str
    price: Decimal
    excluded: bool = False  # 建议否决/否决 → 不进基准价（exclude_invalid=true 时）


@dataclass
class PriceResult:
    bidder_id: int
    name: str
    price: Decimal
    price_score: Decimal
    deviation_pct: Decimal
    excluded_from_benchmark: bool
    explanation: str


@dataclass
class PriceComputation:
    benchmark: Decimal | None
    formula_explanation: str
    results: list[PriceResult]


# ---------------------------------------------------------------------------
# 资格/符合性
# ---------------------------------------------------------------------------
CheckStatus = str  # pass|fail|unknown


@dataclass
class PrimitiveResult:
    status: CheckStatus
    message: str
    evidence_quote: str | None = None
    need_human: bool = False


@dataclass
class ObjectiveVerdict:
    """一个否决项的判定：绝不自动否决，最多"建议否决 + 依据"。"""

    criterion_code: str
    criterion_name: str
    status: str  # pass|recommend_reject|need_human|conflict_flagged
    message: str
    evidence_quote: str | None = None


# ---------------------------------------------------------------------------
# 排名
# ---------------------------------------------------------------------------
@dataclass
class RankRow:
    bidder_id: int
    name: str
    total: Decimal  # 已按精度取整
    bid_price: Decimal | None
    tech_total: Decimal  # 技术分合计
    commercial_total: Decimal  # 商务分合计
    veto_flag: bool = False  # 建议否决/已否决
    rank: int | None = None
    tie_break_level: int | None = None  # 该行靠哪条并列规则分开（1-4）
    tie_explanations: list[str] = field(default_factory=list)


TIE_BREAK_RULES: dict[int, str] = {
    1: "投标报价低者在前",
    2: "技术得分高者在前",
    3: "商务得分高者在前",
    4: "投标人名称字典序（Unicode 码位）",
}
