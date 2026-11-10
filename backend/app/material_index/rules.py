# -*- coding: utf-8 -*-
"""材料分类·确定性规则层（零 LLM）。

判定优先级（先命中先得）：
1. 目录/索引页 → other/toc（不进证据检索，与 selection.index_text_weight 互补）
2. DocNode 章节标题链（投标侧词表）→ 强命中（conf 0.9）
3. 页内短标题行关键词 → 强命中（conf 0.85）
4. 全文关键词计数（≥2 词 0.6 / 1 词 0.5）→ 弱命中
5. 章节区间继承：强命中页向后传播到碎页（文本 <200 字或 source≠text），
   直到下一强命中/目录页/20 页上限（conf 0.7）

表格/图片段随所在页归属；同页跨类强标题落 seg_kind='text' 例外行。
本模块纯文本逻辑（不碰 PDF/DB），layout 段由 service 检测后传入——便于单测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.material_index.catalog import (
    MATERIAL_KEYWORDS,
    all_keywords,
    category_for_keyword,
    normalize_tag,
)
from app.parsing.normalize import normalize_no_ws

# 标题行长度上限（同 category_map._MAX_TITLE_LEN 口径）
_MAX_TITLE_LEN = 24
_PROSE_MARKS = ("。", "，", "；")
# 全文弱命中：至少 N 个不同类别词命中才采信
_WEAK_MIN_HITS = 2
# 继承传播上限（页）
_INHERIT_MAX_PAGES = 20
# 碎页判据：文本短于该值视为无实质内容
_SPARSE_TEXT_CHARS = 200

# 目录/索引页判据（与 engine/selection._RE_TOC_LINE 同源；结构性、零项目拟合）
_RE_TOC_LINE = re.compile(
    r"(?:[.。…·]{3,}\s*\d{1,3}\s*$)"
    r"|(?:[（(]?\s*(?:第\s*)?\d{1,3}\s*页\s*[)）]?\s*$)"
    r"|(?:见\s*投标\s*文件)"
    r"|(?:详见\s*第?\s*\d)"
)

CONF_STRONG = 0.85
CONF_NODE = 0.9
CONF_WEAK_MULTI = 0.6
CONF_WEAK_SINGLE = 0.5
CONF_INHERIT = 0.7
CONF_SEGMENT = 0.75
# 目录行占比 ≥ 该值判目录页
_TOC_FRAC = 0.4


@dataclass
class PageInput:
    page_no: int
    text: str
    source: str = "text"


@dataclass
class LayoutSeg:
    """service 从 PDF 版面检测出的表格/图片段（rules 不碰 PDF）。"""

    page_no: int
    seg_kind: str  # table|image
    seg_index: int
    bbox: tuple[float, float, float, float] | None = None
    area_ratio: float | None = None


@dataclass
class TagRow:
    page_no: int
    seg_kind: str  # page|table|image|text
    seg_index: int = 0
    bbox_json: dict | None = None
    category: str = "other"
    tags: list[str] = field(default_factory=list)
    method: str = "rule"
    confidence: float = 0.0
    basis: str = ""


def is_toc_page(text: str) -> bool:
    """目录/索引页：目录点线/页码引用行占比过半。"""
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    if not lines:
        return False
    hits = sum(1 for ln in lines if _RE_TOC_LINE.search(ln))
    return hits / len(lines) >= _TOC_FRAC


def _title_lines(text: str) -> list[str]:
    """页内短标题行（≤24 字、无句读）——OCR 碎页/扫描页的标题也在此。"""
    out: list[str] = []
    for ln in (text or "").split("\n"):
        s = ln.strip()
        if not s or len(s) > _MAX_TITLE_LEN:
            continue
        if any(m in s for m in _PROSE_MARKS):
            continue
        out.append(s)
    return out


def _match_keywords(lines: list[str]) -> tuple[str | None, list[str], str]:
    """在候选标题行里找首个命中词 → (category, tags, 原词)。"""
    for line in lines:
        for cat, kw in all_keywords():
            if kw in line:
                return cat, [normalize_tag(kw)], kw
    return None, [], ""


def _weak_hits(text: str) -> tuple[str | None, list[str], int]:
    """全文关键词计数（跨类别取命中词最多的类别；平手按声明序）。"""
    norm = normalize_no_ws(text or "")
    counts: dict[str, int] = {}
    tags: dict[str, list[str]] = {}
    for cat, kws in MATERIAL_KEYWORDS:
        for kw in kws:
            if kw in text or normalize_no_ws(kw) in norm:
                counts[cat] = counts.get(cat, 0) + 1
                tags.setdefault(cat, [])
                t = normalize_tag(kw)
                if t not in tags[cat]:
                    tags[cat].append(t)
    if not counts:
        return None, [], 0
    best_cat, best_n = None, 0
    for cat, n in counts.items():  # dict 序 = MATERIAL_KEYWORDS 声明序（平手先声明者胜）
        if n > best_n:
            best_cat, best_n = cat, n
    if best_cat is None or best_n < 1:
        return None, [], 0
    total = sum(counts.values())
    if total < _WEAK_MIN_HITS:
        return best_cat, tags.get(best_cat, []), total
    return best_cat, tags.get(best_cat, []), total


def classify_pages(
    pages: list[PageInput],
    node_titles: dict[int, list[str]] | None = None,
    layout_segs: list[LayoutSeg] | None = None,
) -> list[TagRow]:
    """页级分类 + 段级归属。node_titles: page_no → 该页所在章节标题链（深→浅）。"""
    node_titles = node_titles or {}
    layout_segs = layout_segs or []
    results: list[TagRow] = []
    # 页级中间状态：page_no → (category, tags, conf, basis) 或 None
    verdicts: dict[int, tuple[str, list[str], float, str] | None] = {}
    strong_pages: set[int] = set()

    for p in pages:
        if is_toc_page(p.text):
            verdicts[p.page_no] = ("other", ["toc"], 0.9, "目录/索引页判据")
            results.append(
                TagRow(p.page_no, "page", category="other", tags=["toc"],
                       confidence=0.9, basis="目录/索引页判据")
            )
            continue

        # 2. 章节标题链
        chain = node_titles.get(p.page_no) or []
        cat, tags, kw = _match_keywords(chain)
        if cat:
            verdicts[p.page_no] = (cat, tags, CONF_NODE, f"章节标题链命中「{kw}」")
            strong_pages.add(p.page_no)
            results.append(
                TagRow(p.page_no, "page", category=cat, tags=tags,
                       confidence=CONF_NODE, basis=f"章节标题链命中「{kw}」")
            )
            continue

        # 3. 页内短标题行
        lines = _title_lines(p.text)
        cat, tags, kw = _match_keywords(lines)
        if cat:
            verdicts[p.page_no] = (cat, tags, CONF_STRONG, f"页内标题行命中「{kw}」")
            strong_pages.add(p.page_no)
            results.append(
                TagRow(p.page_no, "page", category=cat, tags=tags,
                       confidence=CONF_STRONG, basis=f"页内标题行命中「{kw}」")
            )
            continue

        # 4. 全文弱命中
        cat, tags, n = _weak_hits(p.text)
        if cat and n >= _WEAK_MIN_HITS:
            conf = CONF_WEAK_MULTI
            verdicts[p.page_no] = (cat, tags, conf, f"全文关键词命中 {n} 处")
            results.append(
                TagRow(p.page_no, "page", category=cat, tags=tags,
                       confidence=conf, basis=f"全文关键词命中 {n} 处")
            )
        elif cat:
            verdicts[p.page_no] = (cat, tags, CONF_WEAK_SINGLE, "全文关键词命中 1 处")
            results.append(
                TagRow(p.page_no, "page", category=cat, tags=tags,
                       confidence=CONF_WEAK_SINGLE, basis="全文关键词命中 1 处")
            )
        else:
            verdicts[p.page_no] = None

    # 5. 章节区间继承（碎页链式归入最近的前一个强命中章节；
    #    弱命中/目录页是边界，继承页可继续向下传播）
    anchor: int | None = None
    for p in sorted(pages, key=lambda x: x.page_no):
        v = verdicts.get(p.page_no)
        if p.page_no in strong_pages:
            anchor = p.page_no
            continue
        if v is not None:
            anchor = None  # 弱命中/目录等判定页是传播边界
            continue
        if anchor is not None and p.page_no - anchor <= _INHERIT_MAX_PAGES and (
            len(p.text or "") < _SPARSE_TEXT_CHARS or p.source != "text"
        ):
            a_cat, a_tags, _c, _b = verdicts[anchor] or ("other", [], 0, "")
            verdicts[p.page_no] = (a_cat, list(a_tags), CONF_INHERIT, f"继承自 p{anchor}")
            results.append(
                TagRow(p.page_no, "page", category=a_cat, tags=list(a_tags),
                       confidence=CONF_INHERIT, basis=f"继承自 p{anchor}")
            )
            continue  # 继承页保持 anchor，链式传播
        anchor = None

    # 未判出的页落 other（LLM 兜底层只接手这些）
    for p in pages:
        if verdicts.get(p.page_no) is None:
            verdicts[p.page_no] = ("other", [], 0.0, "规则层未判出")
            results.append(
                TagRow(p.page_no, "page", category="other", tags=[],
                       confidence=0.0, basis="规则层未判出")
            )

    results.extend(_classify_segments(pages, verdicts, layout_segs))
    results.sort(key=lambda r: (r.page_no, r.seg_kind, r.seg_index))
    return results


def _classify_segments(
    pages: list[PageInput],
    verdicts: dict[int, tuple[str, list[str], float, str] | None],
    layout_segs: list[LayoutSeg],
) -> list[TagRow]:
    """表格/图片段归属：有同页表名命中 → 用表名词类别，否则继承页类别。"""
    page_by_no = {p.page_no: p for p in pages}
    out: list[TagRow] = []
    for seg in layout_segs:
        v = verdicts.get(seg.page_no)
        page_cat = v[0] if v else "other"
        tags: list[str] = []
        basis = ""
        # 表名/材料名代理：同页短标题行命中词（"开标一览表"→price）
        p = page_by_no.get(seg.page_no)
        is_toc = bool(v and "toc" in v[1])
        lines = [] if is_toc else (_title_lines(p.text) if p else [])
        seg_cat, tags, kw = _match_keywords(lines)
        if seg_cat:
            category, basis = seg_cat, f"段名命中「{kw}」"
            conf = CONF_SEGMENT
        else:
            category, basis = page_cat, "继承所在页类别"
            conf = CONF_SEGMENT - 0.05
            if v:
                tags = [t for t in v[1] if t != "toc"]
        out.append(
            TagRow(
                page_no=seg.page_no,
                seg_kind=seg.seg_kind,
                seg_index=seg.seg_index,
                bbox_json=(
                    {"x0": seg.bbox[0], "y0": seg.bbox[1], "x1": seg.bbox[2], "y1": seg.bbox[3],
                     "area_ratio": seg.area_ratio}
                    if seg.bbox
                    else ({"area_ratio": seg.area_ratio} if seg.area_ratio is not None else None)
                ),
                category=category,
                tags=tags,
                confidence=conf,
                basis=basis,
            )
        )

    # 同页跨类强标题 → text 段例外行（技术方案页里插的社保表之类）
    for p in pages:
        v = verdicts.get(p.page_no)
        if v is None:
            continue
        if "toc" in v[1]:
            continue  # 目录页的"标题"只是目录条目，不是跨类材料
        page_cat = v[0]
        seen: set[str] = set()
        for line in _title_lines(p.text):
            for cat, kw in all_keywords():
                if kw not in line:
                    continue
                if cat == page_cat or cat in seen:
                    continue
                seen.add(cat)
                out.append(
                    TagRow(
                        page_no=p.page_no,
                        seg_kind="text",
                        seg_index=len(seen),
                        category=cat,
                        tags=[normalize_tag(kw)],
                        confidence=CONF_STRONG,
                        basis=f"同页跨类标题「{kw}」",
                    )
                )
    return out


# LLM 兜底层的候选页判定（与分类口径同源，避免两处漂移）
def needs_llm(row: TagRow) -> bool:
    return row.seg_kind == "page" and row.confidence < CONF_WEAK_MULTI and "toc" not in row.tags
