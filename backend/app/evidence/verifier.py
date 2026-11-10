"""证据核验：verified 字段的**唯一写入点**。

规则（与方案一致）：
- verified=True 当且仅当 L1 精确命中（归一化后逐字）且引文 ≥ MIN_QUOTE_CHARS；
- L2/L3 命中 → verified=False + verify_method + 候选，交人工；
- 引文根本不存在（引用造假）→ verified=False 且无候选 → 进复核队列；
- 页码以核验结果为准（模型给的页码只是提示）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from app.db.models import Document, DocumentPage, EvidenceCitation
from app.db.session import session_scope
from app.evidence.locator import DocumentSource, locate

log = logging.getLogger("bid_eval.evidence")


def verify_automatic_citation(citation: EvidenceCitation, page_text: str) -> bool:
    """核验自动补读后的逐页引文，供同一事务内保存证据和完成标记。

    page_text 必须来自该页原文或本轮对原页的 OCR，不能使用模型改写的文本。
    与定位器共用归一化及最短长度，不将视觉观察伪装成逐字引文。
    """
    from app import config
    from app.parsing.normalize import normalize_no_ws

    quote = normalize_no_ws(citation.quote or "")
    text = normalize_no_ws(page_text or "")
    citation.verified = len(quote) >= config.MIN_QUOTE_CHARS and quote in text
    citation.verify_method = "exact" if citation.verified else None
    citation.verify_score = 1.0 if citation.verified else None
    return citation.verified


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


# 文档 ID 仅在当前数据库内唯一；重解析和原文件替换也必须使缓存失效。
# source 与 memo 共用版本键，保留热点文档会话但不复用旧页文本或旧坐标。
@dataclass
class _SourceEntry:
    source: DocumentSource
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0
    cached: bool = True


_SOURCES: OrderedDict[tuple, _SourceEntry] = OrderedDict()
_SOURCES_LOCK = threading.Lock()
_SOURCES_LIMIT = 8
_MEMO: OrderedDict[tuple, dict] = OrderedDict()
_MEMO_LIMIT = 4096


def _document_version(session, document_id: int) -> tuple:
    document = session.get(Document, document_id)
    if document is None:
        raise ValueError(f"文档不存在：{document_id}")
    path = Path(document.stored_path or "")
    try:
        stat = path.stat()
        file_version = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
    except OSError:
        file_version = None
    pages = session.execute(select(
        DocumentPage.page_no, DocumentPage.text, DocumentPage.width, DocumentPage.height,
        DocumentPage.source, DocumentPage.has_text_layer,
    ).where(DocumentPage.document_id == document_id).order_by(DocumentPage.page_no)).all()
    pages_hash = hashlib.sha256(json.dumps(
        [list(row) for row in pages], ensure_ascii=False, default=str,
    ).encode("utf-8")).hexdigest()
    # Retain the Engine object instead of id(engine), which Python may reuse
    # after a database reset while old cache entries are still alive.
    return (session.get_bind(), document_id, str(path.resolve()), document.sha256, file_version, pages_hash)


def _retire_source(entry: _SourceEntry) -> None:
    entry.cached = False
    if not entry.users:
        entry.source.__exit__()


@contextmanager
def _get_source(document_id: int, version: tuple):
    with _SOURCES_LOCK:
        entry = _SOURCES.get(version)
        if entry is None:
            entry = _SourceEntry(DocumentSource(document_id))
            # Retire prior revisions of this document immediately. Active
            # readers keep their handle until leaving their source context.
            for old in [key for key in _SOURCES if key[:2] == version[:2]]:
                _retire_source(_SOURCES.pop(old))
            for old in [key for key in _MEMO if key[0][:2] == version[:2] and key[0] != version]:
                _MEMO.pop(old)
            _SOURCES[version] = entry
            while len(_SOURCES) > _SOURCES_LIMIT:
                _old, retired = _SOURCES.popitem(last=False)
                _retire_source(retired)
        _SOURCES.move_to_end(version)
        entry.users += 1
    try:
        # pdfplumber and DocumentSource's lazy page caches are shared only
        # between serial readers of this source; independent files can proceed.
        with entry.lock:
            yield entry.source
    finally:
        with _SOURCES_LOCK:
            entry.users -= 1
            if not entry.cached and not entry.users:
                entry.source.__exit__()


def verify_citation(citation_id: int) -> EvidenceCitation | None:
    """核验一条证据引用并落库（本函数是 verified 的唯一写入点）。"""
    with session_scope() as s:
        cit = s.get(EvidenceCitation, citation_id)
        if cit is None:
            return None
        document_id = cit.document_id
        quote = cit.quote
        hint = coerce_page_hint(cit.page_no)  # 存量行可能存了字符串形态
        try:
            version = _document_version(s, document_id) if document_id is not None else None
        except Exception:  # noqa: BLE001 —— 保持核验失败降级为未核验
            log.exception("证据文档版本读取异常 citation=%s", citation_id)
            version = None

    verified = False
    method = None
    page_no = hint
    bbox = None
    page_size = None
    candidates: list[dict] = []

    if document_id is not None and version is not None:
        try:
            memo_key = (version, quote or "", hint)
            with _SOURCES_LOCK:
                cached = deepcopy(_MEMO.get(memo_key))
                if cached is not None:
                    _MEMO.move_to_end(memo_key)
            if cached is not None:
                verified = cached["verified"]
                method = cached["method"]
                page_no = cached["page_no"]
                bbox = cached["bbox"]
                page_size = cached["page_size"]
                candidates = cached["candidates"]
            else:
                with _get_source(document_id, version) as source:
                    result = locate(quote, source, hint_page=hint)
                verified = result.verified
                method = result.method
                page_no = result.page_no if result.found else hint
                bbox = result.bbox
                page_size = result.page_size
                candidates = result.candidates
                with _SOURCES_LOCK:
                    _MEMO[memo_key] = deepcopy({
                        "verified": verified, "method": method, "page_no": page_no,
                        "bbox": bbox, "page_size": page_size, "candidates": candidates,
                    })
                    _MEMO.move_to_end(memo_key)
                    while len(_MEMO) > _MEMO_LIMIT:
                        _MEMO.popitem(last=False)
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
