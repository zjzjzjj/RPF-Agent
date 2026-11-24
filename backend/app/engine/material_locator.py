# -*- coding: utf-8 -*-
"""目录驱动材料页定位（2026-11-24）。

用户方向：**识别投标文件的目录，在目录里找到相关材料的页码**——为存在性核验
提供页源。设计原则（用户定案）：
- **详细材料大多数不需要被阅读**（发票/合同/验收只判"有没有"），本模块不读
  内容、不喂 LLM、零 OCR——纯文本计算；
- 三路定位：目录条目（主）→ 详细评审索引指针 → material_tags/关键词兜底；
- **目录失败不阻断**（扫描件/残缺目录 → 兜底路；仍无信号 → 返回空，调用方
  行为与现状逐字节一致）。

输出 MaterialWindow（与 presence.LocatedWindow / case_evidence.SectionGuess
键形兼容）；页号集合供护栏"目录材料页 ⊆ 存在性核验覆盖"断言使用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app import config

# 材料类型全集（客观材料链）
MATERIAL_KINDS = (
    "performance", "contract", "invoice", "acceptance",
    "cert", "query_screenshot", "cmmi", "personnel",
)

# 标题/标签 → 材料类型：有序规则表（首中即停，长词/专词优先）
_KIND_RULES: tuple[tuple[str, str], ...] = (
    ("查询截图", "query_screenshot"),
    ("查询结果", "query_screenshot"),
    ("认证查询", "query_screenshot"),
    ("查询报告", "query_screenshot"),
    ("CMMI", "cmmi"),
    ("CSMM", "cmmi"),
    ("能力成熟度", "cmmi"),
    ("业绩", "performance"),
    ("类似项目", "performance"),
    ("类似产品", "performance"),
    ("发票", "invoice"),
    ("竣工", "acceptance"),
    ("验收", "acceptance"),
    ("完工", "acceptance"),
    ("业主证明", "acceptance"),
    ("用户证明", "acceptance"),
    ("使用证明", "acceptance"),
    ("中标通知书", "contract"),
    ("合同", "contract"),
    ("协议书", "contract"),
    ("证书", "cert"),
    ("认证", "cert"),
    ("资质", "cert"),
    ("ISO", "cert"),
    ("体系", "cert"),
    ("人员", "personnel"),
    ("社保", "personnel"),
    ("职称", "personnel"),
    ("简历", "personnel"),
    ("岗位", "personnel"),
)

# 无点线目录行的扩充词表（不动 presence._TOC_KEYWORDS——参与既有评分，红线）
from app.engine.presence import _TOC_KEYWORDS  # noqa: E402

_LOCATOR_TOC_KEYWORDS = tuple(_TOC_KEYWORDS) + (
    "业绩", "合同", "发票", "验收", "证书", "资质", "认证", "查询截图",
    "CMMI", "CSMM", "能力成熟度", "人员", "社保", "职称", "简历", "竣工", "完工",
    "中标通知书", "业主证明", "用户证明",
)

# 索引指针路的代表词（每类 2-3 个，"见投标文件（X-Y）页"形态）
_KIND_POINTER_WORDS: dict[str, tuple[str, ...]] = {
    "performance": ("业绩", "类似项目"),
    "contract": ("合同", "中标通知书"),
    "invoice": ("发票",),
    "acceptance": ("验收", "业主证明"),
    "cert": ("证书", "认证"),
    "query_screenshot": ("查询截图", "查询结果"),
    "cmmi": ("CMMI", "能力成熟度"),
    "personnel": ("人员", "社保", "职称"),
}

# 页级标签 → 材料类型（material_tags 解析期分类的宽松子串匹配）
_TAG_RULES: tuple[tuple[str, str], ...] = (
    ("查询截图", "query_screenshot"),
    ("查询结果", "query_screenshot"),
    ("CMMI", "cmmi"),
    ("成熟度", "cmmi"),
    ("发票", "invoice"),
    ("验收", "acceptance"),
    ("竣工", "acceptance"),
    ("业绩", "performance"),
    ("类似", "performance"),
    ("中标通知书", "contract"),
    ("合同", "contract"),
    ("证书", "cert"),
    ("认证", "cert"),
    ("资质", "cert"),
    ("社保", "personnel"),
    ("职称", "personnel"),
    ("人员", "personnel"),
    ("简历", "personnel"),
)

MAX_WINDOW_PAGES = 120
_TEXT_HEAD = 300


@dataclass
class MaterialWindow:
    document_id: int
    start: int
    end: int
    kind: str
    method: str  # toc | index | tag | keyword
    confidence: float
    keyword: str = ""
    page_nos: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "start": self.start,
            "end": self.end,
            "kind": self.kind,
            "method": self.method,
            "confidence": self.confidence,
            "keyword": self.keyword,
            "page_nos": list(self.page_nos),
        }


def _kind_of(text: str) -> str | None:
    for word, kind in _KIND_RULES:
        if word in text:
            return kind
    return None


def _is_map_page(page: dict) -> bool:
    """目录/索引页=地图不是领土：兜底关键词路必须跳过（条目标题里的
    "发票材料""合同复印件"字样不是材料本身，2026-11-24 测试实证）。"""
    from app.engine.presence import _DOTTED_TOC_RE, _INDEX_MARKERS, _text

    text = _text(page) or ""
    if "目录" in text[:200] or any(m in text for m in _INDEX_MARKERS):
        return True
    dotted = sum(1 for line in text.splitlines() if _DOTTED_TOC_RE.match(line.strip()))
    return dotted >= 2


def locate_material_sections(
    pages: list[dict], kinds: tuple[str, ...] | None = None,
) -> dict[str, list[MaterialWindow]]:
    """三路定位材料章节 → {kind: [MaterialWindow]}（按置信度降序）。

    纯文本计算；任何异常/无信号 → 该路静默跳过（目录失败不阻断，用户定案）。
    """
    want = set(kinds or MATERIAL_KINDS)
    out: dict[str, list[MaterialWindow]] = {k: [] for k in want}
    if not pages:
        return out
    try:
        from app.engine.presence import _document_offset, _page_no, _text, _toc_entries

        by_doc: dict[int, list[dict]] = {}
        for p in pages:
            try:
                by_doc.setdefault(int(p.get("document_id") or 0), []).append(p)
            except Exception:  # noqa: BLE001
                continue
        for doc_id, doc_pages in by_doc.items():
            doc_pages.sort(key=lambda q: int(q.get("page_no") or 0))
            known = {int(q.get("page_no") or 0) for q in doc_pages
                     if not q.get("known_blank")}
            if not known:
                continue
            last_page = max(known)
            # 路 1：目录条目（主路）
            try:
                offset = _document_offset(doc_pages)
                entries = _toc_entries(doc_pages, offset,
                                       plain_keywords=_LOCATOR_TOC_KEYWORDS)
            except Exception:  # noqa: BLE001
                entries = []
            toc_rows = sorted(
                ((target, title) for _tp, title, target in entries),
                key=lambda x: x[0])
            for i, (target, title) in enumerate(toc_rows):
                kind = _kind_of(title)
                if kind not in want or target < 1:
                    continue
                end = (toc_rows[i + 1][0] - 1) if i + 1 < len(toc_rows) else last_page
                end = min(end, target + MAX_WINDOW_PAGES - 1)
                if end < target:
                    end = target
                page_nos = [n for n in sorted(known) if target <= n <= end]
                if not page_nos:
                    continue
                out[kind].append(MaterialWindow(
                    document_id=doc_id, start=page_nos[0], end=page_nos[-1],
                    kind=kind, method="toc", confidence=0.9, keyword=title,
                    page_nos=page_nos))
            # 路 2：详细评审索引指针（"见投标文件（X-Y）页"）
            try:
                from app.evidence.case_evidence import PageRec, resolve_index_pages

                recs = [PageRec(_page_no(q), _text(q), str(q.get("source") or "text"))
                        for q in doc_pages]
                for kind in want:
                    for word in _KIND_POINTER_WORDS.get(kind, ()):
                        guess = resolve_index_pages(recs, word, line_window=3,
                                                    extend=True)
                        if not guess.found:
                            continue
                        start = max(1, int(guess.start))
                        end = min(last_page, int(guess.end or guess.start))
                        page_nos = [n for n in sorted(known) if start <= n <= end]
                        if page_nos:
                            out[kind].append(MaterialWindow(
                                document_id=doc_id, start=page_nos[0],
                                end=page_nos[-1], kind=kind, method="index",
                                confidence=0.8, keyword=word, page_nos=page_nos))
                        break  # 每类取首个命中的代表词即可
            except Exception:  # noqa: BLE001
                pass
            # 路 3：material_tags / 页首关键词兜底（连续页合并成 run）
            runs: dict[str, list[tuple[int, str]]] = {}
            for q in doc_pages:
                n = int(q.get("page_no") or 0)
                if n < 1 or q.get("known_blank") or _is_map_page(q):
                    continue
                tags = [str(t) for t in (q.get("material_tags") or [])]
                kind = None
                method = "tag"
                for t in tags:
                    for word, k in _TAG_RULES:
                        if word in t:
                            kind = k
                            break
                    if kind:
                        break
                if kind is None:
                    head = (_text(q) or "")[:_TEXT_HEAD]
                    for word, k in _KIND_RULES:
                        if word in head:
                            kind = k
                            method = "keyword"
                            break
                if kind in want:
                    runs.setdefault(kind, []).append((n, method))
            for kind, hits in runs.items():
                hits.sort()
                chunk: list[tuple[int, str]] = []
                for n, m in hits + [(-1, "")]:
                    if chunk and (n != chunk[-1][0] + 1):
                        page_nos = [x for x, _ in chunk]
                        out[kind].append(MaterialWindow(
                            document_id=doc_id, start=page_nos[0], end=page_nos[-1],
                            kind=kind,
                            method="tag" if any(mm == "tag" for _, mm in chunk) else "keyword",
                            confidence=0.6 if any(mm == "tag" for _, mm in chunk) else 0.5,
                            page_nos=page_nos))
                        chunk = []
                    if n > 0:
                        chunk.append((n, m))
    except Exception:  # noqa: BLE001 —— 定位失败不阻断（目录残缺/扫描件）
        pass
    for kind in out:
        out[kind].sort(key=lambda w: -w.confidence)
    return out


def material_page_index(
    pages: list[dict], kinds: tuple[str, ...] | None = None,
) -> dict[str, set[int]]:
    """kind → 页号集合（护栏"目录材料页 ⊆ 核验覆盖"断言用）。"""
    index: dict[str, set[int]] = {}
    try:
        for kind, windows in locate_material_sections(pages, kinds).items():
            merged: set[int] = set()
            for w in windows:
                merged.update(w.page_nos)
            if merged:
                index[kind] = merged
    except Exception:  # noqa: BLE001
        return {}
    return index


def locator_enabled() -> str:
    """BID_EVAL_MATERIAL_LOCATOR = shadow | on | off（默认 shadow=只写审计）。"""
    try:
        return config.material_locator_mode()
    except Exception:  # noqa: BLE001
        return "shadow"
