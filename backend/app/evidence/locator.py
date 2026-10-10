"""文档源与引文定位：引文 → (页码, bbox, 定位方式, 置信度)。

设计（对应方案"bbox 不入库全量 words"）：
- 页文本与页宽高从 document_pages 读（快）；
- 行级坐标**按需**从原 PDF 重新提取（打开一次、命中页才取），用完即关；
- docx 没有坐标，line_boxes 为 None → 证据是页级引用（bbox=None），如实呈现。

定位三级回退（LLM 引文后半段常被改写、前半段通常忠实）：
- L1 精确子串（归一化后）→ verified 的唯一条件（且引文 ≥ MIN_QUOTE_CHARS）
- L2 前缀回退：60/40/25/15 字符逐级试 → 候选交人工
- L3 按句读切段分别匹配 → 候选交人工
找不到 → 返回该文档中最相似的候选片段（含页号），不阻塞流程。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select

from app import config
from app.db.models import Document, DocumentPage
from app.db.session import session_scope
from app.parsing.normalize import normalize, normalize_no_ws_with_map, normalize_with_map


@dataclass
class PageInfo:
    page_no: int
    text: str
    width: float
    height: float
    line_boxes: list[tuple[float, float, float, float]] | None


class DocumentSource:
    """一个文档的读取会话（with 语法）。"""

    def __init__(self, document_id: int):
        self.document_id = document_id
        with session_scope() as s:
            doc = s.get(Document, document_id)
            if doc is None:
                raise ValueError(f"文档不存在：{document_id}")
            self.stored_path = doc.stored_path
            self.suffix = (doc.stored_path or "").rsplit(".", 1)[-1].lower()
        self._pages: dict[int, PageInfo] = {}
        self._count: int | None = None
        self._pdf = None

    # ------------------------------------------------------------------
    def __enter__(self) -> DocumentSource:
        return self

    def __exit__(self, *_exc) -> None:
        if self._pdf is not None:
            try:
                self._pdf.close()
            finally:
                self._pdf = None

    # ------------------------------------------------------------------
    def page_count(self) -> int:
        if self._count is None:
            with session_scope() as s:
                rows = s.execute(
                    select(DocumentPage.page_no).where(DocumentPage.document_id == self.document_id)
                ).scalars().all()
            self._count = max(rows) if rows else 0
        return self._count

    def page(self, page_no: int) -> PageInfo:
        if page_no in self._pages:
            return self._pages[page_no]
        with session_scope() as s:
            row = s.execute(
                select(DocumentPage).where(
                    DocumentPage.document_id == self.document_id, DocumentPage.page_no == page_no
                )
            ).scalar_one_or_none()
            if row is None:
                raise KeyError(f"第 {page_no} 页不存在")
            info = PageInfo(
                page_no=page_no,
                text=row.text or "",
                width=row.width or 0.0,
                height=row.height or 0.0,
                line_boxes=self._load_line_boxes(page_no) if self.suffix == "pdf" else None,
            )
        self._pages[page_no] = info
        return info

    # ------------------------------------------------------------------
    def _load_line_boxes(self, page_no: int) -> list[tuple[float, float, float, float]] | None:
        try:
            if self._pdf is None:
                import pdfplumber

                self._pdf = pdfplumber.open(self.stored_path)
            page = self._pdf.pages[page_no - 1]
            lines = page.extract_text_lines() or []
            return [
                (float(ln["x0"]), float(ln["top"]), float(ln["x1"]), float(ln["bottom"]))
                for ln in lines
            ]
        except Exception:  # noqa: BLE001 —— 坐标取不到就退回页级引用
            return None


# ---------------------------------------------------------------------------
# 定位结果与算法
# ---------------------------------------------------------------------------
@dataclass
class LocateResult:
    found: bool
    page_no: int | None = None
    bbox: list[list[float]] | None = None  # [[x0,y0,x1,y1], ...] top-left 原点
    page_size: list[float] | None = None  # [width, height]
    method: str | None = None  # exact|prefix_fallback|sentence_fallback
    verified: bool = False
    candidates: list[dict] = field(default_factory=list)  # [{page_no, quote}]


_SENTENCE_SPLIT = re.compile(r"[。；！？;!?]")


def locate(
    quote: str,
    src: DocumentSource,
    hint_page: int | None = None,
    min_len: int | None = None,
) -> LocateResult:
    """在文档中定位引文。hint_page 命中优先，其余页按序扫描。"""
    min_len = min_len or config.MIN_QUOTE_CHARS
    q_norm = normalize(quote)
    if len(q_norm) < min_len:
        return LocateResult(found=False, method=None)

    total = src.page_count()
    order = ([hint_page] if hint_page and 1 <= hint_page <= total else []) + [
        p for p in range(1, total + 1) if p != hint_page
    ]

    # L1：精确（归一化后逐字）命中
    for page_no in order:
        page = _safe_page(src, page_no)
        if page is None:
            continue
        p_norm, index_map = normalize_with_map(page.text)
        pos = p_norm.find(q_norm)
        if pos >= 0:
            return _hit(src, page, index_map, pos, len(q_norm), "exact", verified=True)

    # L1b：空白不敏感命中（中文文档的软换行不该影响逐字核对）
    q_nows, _ = normalize_no_ws_with_map(quote)
    if len(q_nows) >= min_len:
        for page_no in order:
            page = _safe_page(src, page_no)
            if page is None:
                continue
            p_nows, index_map_nows = normalize_no_ws_with_map(page.text)
            pos = p_nows.find(q_nows)
            if pos >= 0:
                return _hit(
                    src, page, index_map_nows, pos, len(q_nows), "ws_insensitive", verified=True
                )

    # L2：前缀回退
    for length in config.PREFIX_FALLBACK_LENGTHS:
        if length >= len(q_norm):
            continue
        prefix = q_norm[:length]
        for page_no in order:
            page = _safe_page(src, page_no)
            if page is None:
                continue
            p_norm, index_map = normalize_with_map(page.text)
            pos = p_norm.find(prefix)
            if pos >= 0:
                return _hit(src, page, index_map, pos, length, "prefix_fallback", verified=False)

    # L3：按句读切段分别匹配（取最长命中段）
    pieces = [p.strip() for p in _SENTENCE_SPLIT.split(q_norm) if len(p.strip()) >= min_len]
    for piece in sorted(pieces, key=len, reverse=True):
        for page_no in order:
            page = _safe_page(src, page_no)
            if page is None:
                continue
            p_norm, index_map = normalize_with_map(page.text)
            pos = p_norm.find(piece)
            if pos >= 0:
                return _hit(src, page, index_map, pos, len(piece), "sentence_fallback", verified=False)

    # 找不到：返回最相似的候选片段（不阻塞，交人工挑选）
    return LocateResult(found=False, method=None, candidates=_find_candidates(q_norm, src, order))


def _safe_page(src: DocumentSource, page_no: int):  # noqa: ANN201
    try:
        return src.page(page_no)
    except KeyError:
        return None


def _hit(
    src: DocumentSource,
    page,  # noqa: ANN001
    index_map: list[int],
    pos: int,
    length: int,
    method: str,
    verified: bool,
) -> LocateResult:
    start = index_map[pos]
    end = index_map[pos + length - 1] + 1
    boxes = _bbox_for_range(page, start, end)
    return LocateResult(
        found=True,
        page_no=page.page_no,
        bbox=boxes,
        page_size=[page.width, page.height] if page.width else None,
        method=method,
        verified=verified,
    )


def _bbox_for_range(page, start: int, end: int) -> list[list[float]] | None:  # noqa: ANN001
    """把页内字符区间映射为行 bbox 并集（top-left 原点）。"""
    if not page.line_boxes:
        return None
    boxes: list[list[float]] = []
    offset = 0
    for i, line in enumerate(page.text.split("\n")):
        l_start, l_end = offset, offset + len(line)
        if l_end > start and l_start < end and i < len(page.line_boxes):
            x0, y0, x1, y1 = page.line_boxes[i]
            boxes.append([float(x0), float(y0), float(x1), float(y1)])
        offset = l_end + 1
    return boxes or None


def _find_candidates(q_norm: str, src: DocumentSource, order: list[int], limit: int = 3) -> list[dict]:
    """用引文的首/中/尾片段找近似位置，作为人工挑选的候选。"""
    probes = []
    for frag in (q_norm[:20], q_norm[len(q_norm) // 2 : len(q_norm) // 2 + 20], q_norm[-20:]):
        if len(frag) >= 8 and frag not in probes:
            probes.append(frag)
    out: list[dict] = []
    for frag in probes:
        for page_no in order:
            page = _safe_page(src, page_no)
            if page is None:
                continue
            p_norm = normalize(page.text)
            pos = p_norm.find(frag)
            if pos >= 0:
                # 取命中位置所在行作为候选原文
                snippet = _line_at(page.text, p_norm, pos)
                if snippet:
                    out.append({"page_no": page.page_no, "quote": snippet[:200]})
                    break
        if len(out) >= limit:
            break
    return out


def _line_at(page_text: str, page_norm: str, pos: int) -> str | None:
    # 粗略定位：按行累计归一化长度
    acc = 0
    for line in page_text.split("\n"):
        norm_line = normalize(line)
        if acc <= pos < acc + len(norm_line) + 1:
            return line.strip()
        acc += len(norm_line) + 1
    return None
