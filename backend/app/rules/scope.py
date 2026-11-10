"""Out-of-scope clause policy: bid-rigging / collusion is not evaluated here.

Bid-rigging analysis is owned by a separate dedicated agent.  Tender documents
frequently contain their own collusion clauses (资格/符合性否决条款); this
system keeps them in the frozen rule package as reference data but must not
create evaluation cells for them.  They are reported as a blank "out of scope"
section instead of being scored or vetoed.

The classifier is deterministic, keyword-based and fully auditable.  It only
matches explicit bid-rigging language; ordinary conflict-of-interest or
qualification clauses are not affected.
"""
from __future__ import annotations

import re

OUT_OF_SCOPE_REASON = "围串标核查由独立智能体负责，本系统不评审（留空）"

_COLLUSION_TERMS = (
    "串通投标",
    "恶意串通",
    "视为串通",
    "串通行为",
    "串标",
    "围标",
    "陪标",
)

_MECHANISM_PATTERNS = (
    # Same bidder-controlled author/environment across different bidders.
    re.compile(r"不同投标人.*(?:同一(?:单位|个人|IP|账户|账号|计算机)|"
               r"网卡\s*MAC|MAC\s*地址|CPU\s*序列号|硬盘序列号)"),
    re.compile(r"(?:投标文件|电子投标文件).*(?:由同一(?:单位|个人)编制|异常一致|高度一致|"
               r"呈规律性差异)"),
    # Same deposit account is a classic bid-rigging signal.
    re.compile(r"(?:投标保证金|保证金).*同一(?:账户|账号)"),
    # Same legal representative / controlling or management relationship
    # between different bidders is a bid-rigging clause (c15 class).  Keep the
    # match narrow: ordinary conflict-of-interest clauses are not affected.
    re.compile(r"单位负责人.{0,12}同一人"),
    re.compile(r"(?:控股|管理)关系.{0,30}(?:不同单位|同时投标|同一标段)"),
)


def _criterion_text(criterion: dict) -> str:
    return "\n".join(
        str(criterion.get(key) or "")
        for key in ("name", "description", "source_quote", "required_evidence")
    )


def collusion_reason(criterion: dict) -> str | None:
    """Return the auditable reason when a clause belongs to bid-rigging scope."""
    text = _criterion_text(criterion)
    if not text.strip():
        return None
    if any(term in text for term in _COLLUSION_TERMS):
        return OUT_OF_SCOPE_REASON
    if any(pattern.search(text) for pattern in _MECHANISM_PATTERNS):
        return OUT_OF_SCOPE_REASON
    return None


def split_out_of_scope(criteria: list[dict]) -> tuple[list[dict], list[dict]]:
    """Partition frozen criteria into evaluated clauses and blank out-of-scope ones."""
    kept: list[dict] = []
    excluded: list[dict] = []
    for criterion in criteria:
        reason = collusion_reason(criterion)
        if reason is None:
            kept.append(criterion)
            continue
        excluded.append({
            "id": criterion.get("id") or criterion.get("criterion_id"),
            "code": criterion.get("code"),
            "name": criterion.get("name"),
            "score_type": criterion.get("score_type"),
            "source_page": criterion.get("source_page"),
            "reason": reason,
        })
    return kept, excluded
