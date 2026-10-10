"""汇总与排名：纯 Python，零 LLM。

借鉴 RPF 的教训：**排序前必须按精度取整**——不取整时浮点噪声会在第 15 位小数
上决定名次。并列处理 4 级规则依次适用，每对并列都生成中文说明（可写进报告）。

排序键（依次适用，直到分出先后）：
    1. 总得分高者在前（先按规则包精度取整）
    2. 投标报价低者在前
    3. 技术得分高者在前
    4. 商务得分高者在前
    5. 投标人名称排序
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.engine.models import RankRow, TIE_BREAK_RULES


def round_score(value: Decimal, precision: int = 2) -> Decimal:
    return Decimal(value).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)


def _sort_key(row: RankRow) -> tuple:
    return (
        -row.total,                                       # 1. 总分高者在前
        row.bid_price if row.bid_price is not None else Decimal("999999999999"),  # 2. 价低者在前
        -row.tech_total,                                  # 3. 技术分高者在前
        -row.commercial_total,                            # 4. 商务分高者在前
        row.name.strip(),                                 # 5. 名称排序
    )


def rank_rows(rows: list[RankRow], precision: int = 2) -> list[RankRow]:
    """取整 → 排序 → 逐对解释并列依据。建议否决者同样参与排序（veto_flag 单独标色），
    因为人工需要看到"如果否决不成立他会排第几"来做交叉验证。"""
    for row in rows:
        row.total = round_score(row.total, precision)
        row.tech_total = round_score(row.tech_total, precision)
        row.commercial_total = round_score(row.commercial_total, precision)

    ordered = sorted(rows, key=_sort_key)
    for i, row in enumerate(ordered, start=1):
        row.rank = i

    # 逐对解释：相邻两行若总分相同，指出是哪条规则分开了他们
    for above, below in zip(ordered, ordered[1:]):
        if above.total != below.total:
            continue
        level = _first_separator(above, below)
        if level is not None:
            below.tie_break_level = level
            below.tie_explanations.append(
                f"与「{above.name}」总分并列（{round_score(above.total)} 分），"
                f"按规则{level}「{TIE_BREAK_RULES[level]}」排后"
            )
    return ordered


def _first_separator(above: RankRow, below: RankRow) -> int | None:
    pa = above.bid_price if above.bid_price is not None else Decimal("999999999999")
    pb = below.bid_price if below.bid_price is not None else Decimal("999999999999")
    if pa != pb:
        return 1
    if above.tech_total != below.tech_total:
        return 2
    if above.commercial_total != below.commercial_total:
        return 3
    if above.name.strip() != below.name.strip():
        return 4
    return None
