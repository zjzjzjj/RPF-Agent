"""重复汇总项（duplicate summary）判定——单一来源。

形态：LLM 编译时把细则组行抽成一条 **match 带分值** 的父项（如 c38
「投标人资质实力 满分4」），而骨架缺口补齐又按子行生成计分子项（c61/c62
各 2 分）。分数层已有运行时兜底（automatic_scoring_service._is_duplicate_summary
打 duplicate_summary 不给分），但 score-summary 列表/报告分项表仍会把父项
完整列出——用户口径「项目中不要出现重复计分项」要求展示层也隐藏。

判定（结构式，不依赖本次运行是否打上 fallback，V1/V2 执行器均有效）：
  1) score_type == "match" 且 max_score 非空（match 带分值，本身违约形态）；
  2) source_node_id 非空；
  3) 存在 >=2 条同包、非自身、同 source_node_id 的计分兄弟
     （formula/count/subjective，max_score 非空），且文本与父项重叠；
  4) Σ兄弟.max_score <= 父.max_score。

不误杀：正常 match（max_score=None）条件1失败；非 match 计分项不进谓词；
孤例 match 带分值（无 >=2 计分兄弟）条件3失败保留。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from app.parsing.normalize import normalize_no_ws

# 与 automatic_scoring_service._is_duplicate_summary 同口径
SCORING_TYPES = ("formula", "count", "subjective")
OVERLAP_PREFIX = 40


def _field(item: Any, key: str) -> Any:
    """Criterion ORM / SimpleNamespace / dict 统一取字段。"""
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _text(item: Any) -> str:
    """父/子文本：description 优先，缺 description 用 name（展示层兜底，
    运行时旧实现仅用 description——此处更宽只影响展示隐藏，不影响给分）。"""
    raw = _field(item, "description") or _field(item, "name") or ""
    return normalize_no_ws(str(raw))


def text_overlaps(parent_text: str, child_text: str) -> bool:
    if not parent_text or not child_text:
        return False
    return (
        child_text[:OVERLAP_PREFIX] in parent_text
        or parent_text[:OVERLAP_PREFIX] in child_text
    )


def covered_children(crit: Any, siblings: Iterable[Any]) -> list[Any]:
    """满足条件 3 的计分兄弟（不含自身）。"""
    if _field(crit, "score_type") != "match" or _field(crit, "max_score") is None:
        return []
    node = _field(crit, "source_node_id")
    if not node:
        return []
    parent_text = _text(crit)
    if not parent_text:
        return []
    crit_id = _field(crit, "id")
    hits: list[Any] = []
    for sibling in siblings:
        if sibling is crit:
            continue
        sid = _field(sibling, "id")
        if crit_id is not None and sid is not None and sid == crit_id:
            continue
        if _field(sibling, "source_node_id") != node:
            continue
        if _field(sibling, "score_type") not in SCORING_TYPES:
            continue
        if _field(sibling, "max_score") is None:
            continue
        if text_overlaps(parent_text, _text(sibling)):
            hits.append(sibling)
    return hits


def is_duplicate_summary(crit: Any, siblings: Iterable[Any]) -> bool:
    """c38 类「父级汇总 match」应从展示列表隐藏当且仅当四条件全满足。"""
    if _field(crit, "score_type") != "match" or _field(crit, "max_score") is None:
        return False
    children = covered_children(crit, siblings)
    if len(children) < 2:
        return False
    total = sum((_dec(_field(c, "max_score")) for c in children), Decimal(0))
    return total <= _dec(_field(crit, "max_score"))


def duplicate_summary_ids(criteria: Iterable[Any]) -> set[int]:
    """整包扫描：应被展示层隐藏的 criterion id 集合。"""
    items = list(criteria)
    return {int(_field(c, "id")) for c in items if is_duplicate_summary(c, items)}
