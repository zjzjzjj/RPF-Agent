"""证据核验：verified 字段的**唯一写入点**。

规则（与方案一致）：
- verified=True 当且仅当 L1 精确命中（归一化后逐字）且引文 ≥ MIN_QUOTE_CHARS；
- L2/L3 命中 → verified=False + verify_method + 候选，交人工；
- 引文根本不存在（引用造假）→ verified=False 且无候选 → 进复核队列；
- 页码以核验结果为准（模型给的页码只是提示）。
"""
from __future__ import annotations

import logging

from app.db.models import EvidenceCitation
from app.db.session import session_scope
from app.evidence.locator import DocumentSource, locate

log = logging.getLogger("bid_eval.evidence")


def coerce_page_hint(value) -> int | None:  # noqa: ANN001
    """页码提示归一为 int。

    主观分证据的 page_hint 是 LLM 原始输出（"97"/"第97页"/"P3" 都出现过），不走
    compile 契约的 _coerce_page_hint；字符串落库后会在 locate() 里与 int 比较
    TypeError（2026-10-14 真实评分运行实证）。入口统一归一，取首个数字。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    import re as _re

    m = _re.search(r"\d+", str(value))
    return int(m.group()) if m else None


def verify_citation(citation_id: int) -> EvidenceCitation | None:
    """核验一条证据引用并落库（本函数是 verified 的唯一写入点）。"""
    with session_scope() as s:
        cit = s.get(EvidenceCitation, citation_id)
        if cit is None:
            return None
        document_id = cit.document_id
        quote = cit.quote
        hint = coerce_page_hint(cit.page_no)  # 存量行可能存了字符串形态

    verified = False
    method = None
    page_no = hint
    bbox = None
    page_size = None
    candidates: list[dict] = []

    if document_id is not None:
        try:
            with DocumentSource(document_id) as src:
                result = locate(quote, src, hint_page=hint)
            verified = result.verified
            method = result.method
            page_no = result.page_no if result.found else hint
            bbox = result.bbox
            page_size = result.page_size
            candidates = result.candidates
        except Exception:  # noqa: BLE001 —— 核验失败不阻塞评分，降级为未核验
            log.exception("证据核验异常 citation=%s", citation_id)

    with session_scope() as s:
        cit = s.get(EvidenceCitation, citation_id)
        if cit is None:
            return None
        cit.verified = verified
        cit.verify_method = method
        if page_no is not None:
            cit.page_no = page_no
        cit.bbox_json = bbox
        cit.page_size_json = page_size
        cit.candidates_json = candidates or None
        s.flush()
        s.expunge(cit)
        return cit


def create_citation(
    document_id: int | None,
    quote: str,
    *,
    page_hint: int | None = None,
    role: str = "score",
    criterion_id: int | None = None,
    score_record_id: int | None = None,
    created_by: str = "llm",
    verify: bool = True,
) -> int:
    """创建证据引用（可选立即核验）。返回 citation id。"""
    page_hint = coerce_page_hint(page_hint)
    with session_scope() as s:
        cit = EvidenceCitation(
            document_id=document_id,
            quote=quote,
            page_no=page_hint,
            citation_role=role,
            criterion_id=criterion_id,
            score_record_id=score_record_id,
            created_by=created_by,
        )
        s.add(cit)
        s.flush()
        cit_id = cit.id
    if verify:
        verify_citation(cit_id)
    return cit_id
