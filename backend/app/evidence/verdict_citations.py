"""veto/match 裁决逐字引文落库（2026-11-05 由 scoring_service 迁入，供 deep 与
qualification-light 两条执行路径共用）。

口径镜像 _persist_subjective 证据段：quote <8 字符跳过、每格最多 5 条、
create_citation 立即核验（verified 仍只由 verifier.py 写）、evidence_status
三态回写。与主观分路径的差异：重跑**先删后建**（限本 role + created_by='llm'，
保留人工补录与其它角色引文），引文与裁决结论同步替换；人工已处置
（human_confirmed/overridden）的行不动。纯机器判定（run_check）不产引文，
evidence_items 为空 → 只把 evidence_status 归 none。

兼容扩展（2026-11-16，资格轻量三态化）：
- evidence 条目可自带 ``document_id``（轻量快审引文可落在附件页），缺省回退
  调用方给的 ``main_doc_id``；
- ``stats`` 可传 None（轻量路径不维护采集统计）。
"""
from __future__ import annotations

from sqlalchemy import delete

from app.db.models import EvidenceCitation, ScoreRecord
from app.db.session import session_scope


def persist_verdict_citations(
    score_row,  # ScoreRecord（expunged 行：带已加载属性的游离实例）
    result: dict,
    *,
    main_doc_id: int | None,
    role: str,
    stats: dict | None = None,
) -> None:
    if score_row.review_status in ("human_confirmed", "human_overridden"):
        return
    items = result.get("evidence_items") or []
    from app.evidence.verifier import create_citation

    with session_scope() as s:
        s.execute(
            delete(EvidenceCitation).where(
                EvidenceCitation.score_record_id == score_row.id,
                EvidenceCitation.citation_role == role,
                EvidenceCitation.created_by == "llm",
            )
        )
    evidence_status = "none"
    verified_any = False
    found_any = False
    for ev in items[:5]:
        quote = str(ev.get("quote") or "")
        if len(quote.strip()) < 8:
            continue
        cit_id = create_citation(
            ev.get("document_id") or main_doc_id,
            quote,
            page_hint=ev.get("page_hint") if ev.get("page_hint") is not None else ev.get("page_no"),
            role=role,
            score_record_id=score_row.id,
            created_by="llm",
        )
        if stats is not None:
            stats["citations_total"] += 1
        with session_scope() as s:
            cit = s.get(EvidenceCitation, cit_id)
            if cit.verified:
                verified_any = True
            if cit.verify_method is not None:
                found_any = True
    if verified_any:
        evidence_status = "verified"
    elif found_any:
        evidence_status = "unverified"
    if stats is not None:
        stats["citations_verified"] += int(verified_any)
    with session_scope() as s:
        row = s.get(ScoreRecord, score_row.id)
        row.evidence_status = evidence_status
        s.add(row)


# 向后兼容别名：scoring_service 既有导入与测试继续用旧名
_persist_verdict_citations = persist_verdict_citations
