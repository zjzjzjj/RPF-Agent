# -*- coding: utf-8 -*-
"""材料存在性评分（presence scoring）：目录/索引定位 + 页级图像信号。

2026-11-19 用户指令与 run144 实证：
- 只读 PDF 文字层、扫描件不做内容 OCR；但“有没有材料”不能由空白文本层推出。
- 从详细评审索引表/目录定位评分项对应的页码范围，到对应位置看该页是否有
  实际材料（整页扫描/照片/设计稿，或有实质正文），有材料即按细则给分；
  不解析材料内容，结论标注“材料内容未核验”。
- 三态：present（给分）/ absent（确认缺失 0 分）/ unverified（需人工复核）。
  只有窗口已定位且窗口内全部是可读文本、确实无材料时才判 0；窗口含未 OCR
  扫描页时一律 unverified，不能把“没读到”当成“未提供”。

本模块纯确定性：不调 LLM、不跑 OCR、不读 DB。页 dict 由
`automatic_scoring_service._load_pages` 提供，`embedded_image_ratio` 已由
`annotate_page_readability` 挂好（probe 原 PDF，与 OCR 开关无关）。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app import config
from app.evidence.case_evidence import PageRec, _printed_page, resolve_index_pages

# 正文材料页下限：短于该值的页不当作“方案/正文材料”（页眉、标题页）
TEXT_MATERIAL_MIN_CHARS = 120
# 未 OCR 整页扫描的文本上限（与 material_index/rules._SPARSE_TEXT_CHARS 同口径）
SCAN_TEXT_MAX_CHARS = 200
# 混排扫描页：图像占比 ≥15% 且正文稀少（与 automatic_document_service
# MATERIAL_IMAGE_MIN_RATIO 同口径）。probe 的 embedded_image_ratio 只取
# 最大单图，拼接扫描页可能单图 <0.35 但整页仍是扫描件（run148 翌星
# p1847-1851 实证：0.24-0.33 + 45 字页眉，审计报告段被少算 5 页）。
MIXED_IMAGE_MIN_RATIO = 0.15
# 样品材料：照片/设计稿常非整页，图像阈值单独放宽
SAMPLE_IMAGE_MIN_RATIO = 0.15
# 单页指针向后续章节扩展的上限
MAX_WINDOW_PAGES = 20

_DATE_CUE_RE = re.compile(r"审计|财务报表|财务报告|年度财务|财务状况")
_YEAR_RE = re.compile(r"(20\d{2})\s*年")
_NUMBERED_HEADING_RE = re.compile(
    r"(?:^|\n)\s*(?:（\d+）|\d+(?:\.\d+)+)\s*\S"
)
_DOTTED_TOC_RE = re.compile(r"^(?P<title>.*?)[.。…·]{3,}\s*(?P<page>\d{1,4})\s*$")
_PLAIN_TOC_RE = re.compile(r"^(?P<title>.{4,80}?)(?P<page>\d{1,4})\s*$")
_INDEX_MARKERS = ("详细评审索引", "评审索引表", "索引表", "见投标文件")
_NOISE_TAGS = {"toc", "目录", "空白", "空白页", "分隔页", "隔页"}
_OCR_SOURCES = {"ocr", "ocr_img", "ocr_page", "ocr_mixed"}
_TOC_KEYWORDS = (
    "审计", "财务报表", "财务报告", "银行资信", "资信", "检测人员", "检测",
    "二维码", "防重码", "样品", "样张", "设计稿",
)
_SAMPLE_CUES = ("样品", "样张", "打样", "设计稿", "彩稿", "照片", "效果图")
_CUE_WORDS = (
    "审计报告", "财务报表", "财务报告", "资信证明", "银行资信", "检测人员",
    "检测", "二维码", "防重码", "管控", "配置方案",
)
_GENERIC_STOP = {
    "证明", "材料", "评分", "得分", "提供", "投标", "项目", "情况", "能力",
    "配置", "方案", "要求", "内容", "相关", "进行", "具有", "以及",
}


@dataclass
class LocatedWindow:
    document_id: int
    start: int
    end: int
    method: str
    confidence: float
    keyword: str = ""

    def as_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "start": self.start,
            "end": self.end,
            "method": self.method,
            "confidence": round(self.confidence, 3),
            "keyword": self.keyword,
        }


@dataclass
class PresenceResult:
    state: str  # present | absent | unverified
    score: Decimal | None
    detail: str
    windows: list[dict] = field(default_factory=list)
    material_pages: list[dict] = field(default_factory=list)
    years: dict[str, str] = field(default_factory=dict)
    reason_code: str = ""


# ---------------------------------------------------------------------------
# 路由判定
# ---------------------------------------------------------------------------
def _params(criterion: dict) -> dict:
    value = criterion.get("params")
    return value if isinstance(value, dict) else {}


def _decimal(value) -> Decimal | None:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return d if d.is_finite() else None


def is_presence_criterion(criterion: dict) -> bool:
    """existence + field=items 的计分项（c16/c17/c23/c29 形态）。"""
    if not config.presence_route_enabled():
        return False
    params = _params(criterion)
    per_item = _decimal(params.get("per_item"))
    return bool(
        criterion.get("score_type") == "count"
        and params.get("_objective_form") == "existence"
        and params.get("field") == "items"
        and not params.get("tiers")
        and not params.get("person_scope")
        and per_item is not None and per_item > 0
        and criterion.get("max_score") is not None
    )


def is_sample_criterion(criterion: dict) -> bool:
    """不纳入总分的样品评审项（in_total=false），按材料数量档位给建议分。"""
    if not config.presence_route_enabled():
        return False
    params = _params(criterion)
    return bool(
        criterion.get("score_type") == "subjective"
        and params.get("in_total") is False
        and "样品" in str(criterion.get("name") or "")
        and criterion.get("max_score") is not None
    )


# ---------------------------------------------------------------------------
# 关键词 / 线索
# ---------------------------------------------------------------------------
def _rule_text(criterion: dict) -> str:
    return " ".join(str(criterion.get(k) or "") for k in
                    ("name", "description", "source_quote", "required_evidence"))


def presence_keywords(criterion: dict) -> list[str]:
    """评分项名称/材料要求 → 定位关键词（长词优先，去通用停用词）。"""
    name = re.sub(r"^[（(]?\d+[）)]?\s*", "", str(criterion.get("name") or "").strip())
    name = re.sub(r"[（(]\s*\d+(?:\.\d+)?\s*分\s*[）)]\s*$", "", name).strip()
    out: list[str] = []

    def add(value: str | None) -> None:
        text = (value or "").strip()
        if len(text) >= 2 and text not in _GENERIC_STOP and text not in out:
            out.append(text)

    add(name)
    if name:
        for suffix in ("能力", "情况", "配置", "方案", "证明", "制度"):
            if name.endswith(suffix) and len(name) > len(suffix) + 1:
                add(name[: -len(suffix)])
    for key in ("required_artifacts", "required_artifacts_raw", "required_evidence"):
        for part in re.split(r"[-—–、，,；;：:（）()\s]+", str(criterion.get(key) or "")):
            add(part)
    whole = _rule_text(criterion)
    for cue in _CUE_WORDS:
        if cue in whole:
            add(cue)
    out.sort(key=lambda item: -len(item))
    return out[:24]


def _criterion_cues(criterion: dict) -> list[str]:
    whole = _rule_text(criterion)
    return [cue for cue in _CUE_WORDS if cue in whole]


# ---------------------------------------------------------------------------
# 页工具
# ---------------------------------------------------------------------------
def _page_no(page: dict) -> int:
    try:
        return int(page.get("page_no") or 0)
    except (TypeError, ValueError):
        return 0


def _text(page: dict) -> str:
    return str(page.get("text") or "")


def _group_by_doc(pages: list[dict]) -> dict[int, list[dict]]:
    groups: dict[int, list[dict]] = {}
    for page in pages:
        key = int(page.get("document_id") or 0)
        groups.setdefault(key, []).append(page)
    for doc_pages in groups.values():
        doc_pages.sort(key=_page_no)
    return groups


def _looks_like_index_page(text: str) -> bool:
    if not text:
        return False
    if any(marker in text for marker in _INDEX_MARKERS[:3]):
        return True
    lines = text.splitlines()
    dotted = sum(1 for line in lines if _DOTTED_TOC_RE.match(line.strip()))
    if dotted >= 3:
        return True
    return "见投标文件" in text


def _has_material_cue(text: str, keywords: list[str], cues: list[str]) -> bool:
    if any(keyword in text for keyword in keywords):
        return True
    return any(cue in text for cue in cues)


def _page_is_noise(page: dict) -> bool:
    tags = page.get("material_tags") or []
    return any(str(tag) in _NOISE_TAGS for tag in tags)


def _is_ocr_source(page: dict) -> bool:
    source = str(page.get("source") or "text").lower()
    return source in _OCR_SOURCES or source.startswith("ocr_") or bool(
        page.get("supplemented") or page.get("ocr_completed")
    )


def classify_material_page(
    page: dict, criterion: dict, *, keywords: list[str] | None = None,
    cues: list[str] | None = None,
) -> str:
    """页级材料判定：scan（整页扫描/照片）/ text（实质正文）/ none。"""
    if page.get("known_blank"):
        return "none"
    text = _text(page).strip()
    try:
        ratio = float(page.get("embedded_image_ratio") or 0)
    except (TypeError, ValueError):
        ratio = 0.0
    # 整页扫描优先于 material_tags：材料分类器会把“只有页码的扫描页”误标成
    # toc（run146 桐乡 p1174-1196 实证），按标签排除会把整段审计报告清空。
    if (ratio >= float(config.OCR_IMAGE_MIN_RATIO)
            or (ratio >= MIXED_IMAGE_MIN_RATIO
                and len(text) < SCAN_TEXT_MAX_CHARS)):
        return "scan"
    if _page_is_noise(page):
        return "none"
    kws = keywords if keywords is not None else presence_keywords(criterion)
    material_cues = cues if cues is not None else _criterion_cues(criterion)
    if (len(text) >= TEXT_MATERIAL_MIN_CHARS
            and not _looks_like_index_page(text)
            and _has_material_cue(text, kws, material_cues)):
        return "text"
    return "none"


def _is_unocr_scan(page: dict) -> bool:
    """整页扫描但未 OCR（文本只剩页眉）——存在性“看不见”的最主要来源。"""
    if page.get("known_blank"):
        return False
    if _is_ocr_source(page):
        return False
    if page.get("unreadable") or page.get("requires_vision"):
        return False
    try:
        ratio = float(page.get("embedded_image_ratio") or 0)
    except (TypeError, ValueError):
        return False
    return ((ratio >= float(config.OCR_IMAGE_MIN_RATIO)
             or ratio >= MIXED_IMAGE_MIN_RATIO)
            and len(_text(page).strip()) < SCAN_TEXT_MAX_CHARS)


def is_unverified_full_page_scan(page: dict) -> bool:
    """供 auto_review absence 防线复用：未 OCR 整页扫描 + 无实质文字。"""
    return _is_unocr_scan(page)


# ---------------------------------------------------------------------------
# 目录 / 年度
# ---------------------------------------------------------------------------
def _document_offset(doc_pages: list[dict]) -> int:
    counts: Counter[int] = Counter()
    total = 0
    for page in doc_pages:
        printed = _printed_page(_text(page))
        if printed:
            counts[_page_no(page) - printed] += 1
            total += 1
    if not counts or not total:
        return 0
    value, count = counts.most_common(1)[0]
    return int(value) if count / total >= 0.5 else 0


def _toc_entries(doc_pages: list[dict], offset: int) -> list[tuple[int, str, int]]:
    """目录条目 → [(toc_physical_page, title, target_physical_page)]。"""
    entries: list[tuple[int, str, int]] = []
    for page in doc_pages:
        text = _text(page)
        lines = text.splitlines()
        dotted = sum(1 for line in lines if _DOTTED_TOC_RE.match(line.strip()))
        if dotted < 2 and "目录" not in text[:200]:
            continue
        for line in lines:
            stripped = line.strip()
            match = _DOTTED_TOC_RE.match(stripped)
            if match:
                title = match.group("title").strip()
            else:
                match = _PLAIN_TOC_RE.match(stripped)
                if not match:
                    continue
                title = match.group("title").strip()
                if not any(keyword in title for keyword in _TOC_KEYWORDS):
                    continue
            try:
                printed = int(match.group("page"))
            except (TypeError, ValueError):
                continue
            if not 1 <= printed <= 5000 or not title:
                continue
            entries.append((_page_no(page), title, printed + offset))
    return entries


def _required_year_sets(text: str) -> list[set[int]]:
    """评分项要求的年报年度集合（主集/替代集），无年报语义时返回 []。"""
    if not _DATE_CUE_RE.search(text or ""):
        return []
    years = sorted({int(year) for year in _YEAR_RE.findall(text or "")})
    if len(years) < 2:
        return []
    sets: list[set[int]] = []
    for index in range(len(years) - 2):
        window = years[index:index + 3]
        if window[2] - window[0] == 2:
            sets.append(set(window))
    if not sets:
        anchor = years[-1]
        sets.append({anchor - 2, anchor - 1, anchor})
    # 主集优先：细则通常先写“近3年（2022、2023、2024）”，替代集在后。
    # 按最大年份降序，{2022,2023,2024} 先于 {2021,2022,2023}。
    sets.sort(key=lambda item: -max(item))
    return sets


def locate_year_sections(
    doc_pages: list[dict], years: set[int], offset: int,
    main_window: tuple[int, int] | None,
) -> dict[int, tuple[int, int]]:
    """年度 → 子区间。目录年度条目优先，其次窗口内 OCR 标题页。"""
    starts: dict[int, int] = {}
    entries = _toc_entries(doc_pages, offset)
    for year in sorted(years):
        target: int | None = None
        for _toc_page, title, physical in entries:
            if str(year) in title and _DATE_CUE_RE.search(title):
                if main_window and not (
                        main_window[0] - 10 <= physical <= main_window[1] + 10):
                    continue
                target = physical
                break
        if target is None:
            for page in doc_pages:
                page_no = _page_no(page)
                if main_window and not (main_window[0] <= page_no <= main_window[1]):
                    continue
                head = _text(page)[:300]
                if str(year) in head and _DATE_CUE_RE.search(head):
                    target = page_no
                    break
        if target is not None:
            starts[year] = target
    ordered = sorted(starts.items(), key=lambda item: item[1])
    result: dict[int, tuple[int, int]] = {}
    for index, (year, start) in enumerate(ordered):
        if index + 1 < len(ordered):
            end = ordered[index + 1][1] - 1
        else:
            end = start + MAX_WINDOW_PAGES
            if main_window:
                end = min(end, main_window[1])
        result[year] = (start, max(start, end))
    return result


# ---------------------------------------------------------------------------
# 窗口定位
# ---------------------------------------------------------------------------
def _extend_numbered(doc_pages: list[dict], start: int, cap: int) -> int:
    texts = {_page_no(page): _text(page) for page in doc_pages}
    end = start
    for page_no in range(start + 1, start + cap + 1):
        text = texts.get(page_no)
        if text is None:
            break
        if _NUMBERED_HEADING_RE.search(text[:200]):
            break
        end = page_no
    return end


def _dedupe_windows(windows: list[LocatedWindow]) -> list[LocatedWindow]:
    best: dict[tuple[int, int, int], LocatedWindow] = {}
    for window in windows:
        key = (window.document_id, window.start, window.end)
        current = best.get(key)
        if current is None or window.confidence > current.confidence:
            best[key] = window
    return sorted(best.values(), key=lambda w: (-w.confidence, w.start))


def _toc_windows(
    doc_id: int, entries: list[tuple[int, str, int]], keywords: list[str],
) -> list[LocatedWindow]:
    scored: list[tuple[int, int, str, str]] = []
    for _toc_page, title, physical in entries:
        matched = [keyword for keyword in keywords if keyword in title]
        if not matched:
            continue
        keyword = max(matched, key=len)
        score = len(keyword)
        if title.strip().startswith(keyword) or title.strip() == keyword:
            score += 3
        if any(mark in title for mark in ("管控", "控制", "管理", "方案", "质量", "防重码")):
            score += 2
        if any(mark in title for mark in ("设备", "机器", "装置", "喷码机")):
            score -= 2
        scored.append((score, physical, title, keyword))
    scored.sort(key=lambda item: (-item[0], item[1]))
    out: list[LocatedWindow] = []
    for score, start, _title, keyword in scored[:3]:
        if score <= 0:
            continue
        later = sorted(item[2] for item in entries if item[2] > start)
        end = later[0] - 1 if later else start + MAX_WINDOW_PAGES - 1
        end = min(end, start + 120)
        if end < start:
            continue
        out.append(LocatedWindow(
            document_id=doc_id, start=start, end=end,
            method=f"toc:{keyword}", confidence=0.8 if score >= 4 else 0.6,
            keyword=keyword,
        ))
    return out


def _heading_window(
    doc_id: int, doc_pages: list[dict], keywords: list[str],
) -> LocatedWindow | None:
    for page in doc_pages:
        text = _text(page)
        if not text or _looks_like_index_page(text):
            continue
        head = text[:300]
        matched = next((keyword for keyword in keywords if keyword in head), None)
        if matched is None:
            continue
        start = _page_no(page)
        if start <= 0:
            continue
        end = _extend_numbered(doc_pages, start, MAX_WINDOW_PAGES)
        return LocatedWindow(
            document_id=doc_id, start=start, end=end,
            method="heading", confidence=0.5, keyword=matched,
        )
    return None


def locate_windows(
    pages: list[dict], criterion: dict, *,
    keywords: list[str] | None = None, cues: list[str] | None = None,
) -> list[LocatedWindow]:
    """主文档多路候选窗口；一个都定位不到返回空列表。"""
    kws = [keyword for keyword in (keywords or presence_keywords(criterion)) if keyword]
    if not kws:
        return []
    groups = _group_by_doc(pages)
    docs = sorted(groups.items(), key=lambda item: len(item[1]), reverse=True)
    for doc_id, doc_pages in docs:
        recs = [PageRec(_page_no(page), _text(page), str(page.get("source") or "text"))
                for page in doc_pages]
        found: list[LocatedWindow] = []
        for keyword in kws:
            try:
                guess = resolve_index_pages(recs, keyword, line_window=3, extend=False)
            except Exception:  # noqa: BLE001 - 定位失败只降级，不失败整格
                continue
            if not guess.found:
                continue
            start, end = int(guess.start), int(guess.end)
            if start == end:
                end = _extend_numbered(doc_pages, start, MAX_WINDOW_PAGES)
            if end < start:
                continue
            if not any(start <= _page_no(page) <= end for page in doc_pages):
                continue
            found.append(LocatedWindow(
                document_id=doc_id, start=start, end=end,
                method=f"index:{keyword}", confidence=float(guess.confidence),
                keyword=keyword,
            ))
            # 详细评审索引表是官方口径：首个命中的关键词窗口即主窗口，
            # 不再为其余 20+ 个关键词重复扫描全文。
            break
        offset = _document_offset(doc_pages)
        found.extend(_toc_windows(doc_id, _toc_entries(doc_pages, offset), kws))
        heading = _heading_window(doc_id, doc_pages, kws)
        if heading is not None:
            found.append(heading)
        windows = _dedupe_windows(found)
        if windows:
            return windows
    return []


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------
def _window_pages(window: LocatedWindow, groups: dict[int, list[dict]]) -> list[dict]:
    return [page for page in groups.get(window.document_id, [])
            if window.start <= _page_no(page) <= window.end]


def _window_dicts(windows: list[LocatedWindow]) -> list[dict]:
    return [window.as_dict() for window in windows]


def _material_page_dicts(pages: list[dict]) -> list[dict]:
    return [{"document_id": int(page.get("document_id") or 0),
             "page_no": _page_no(page)} for page in pages]


def _score_value(criterion: dict, *, full: bool) -> Decimal:
    params = _params(criterion)
    maximum = _decimal(criterion.get("max_score")) or Decimal(0)
    if not full:
        return Decimal(0)
    per_item = _decimal(params.get("per_item")) or maximum
    return min(per_item, maximum).quantize(Decimal("0.01"))


def _present(
    criterion: dict, windows: list[LocatedWindow], material: list[dict],
    years: dict[str, str] | None = None, extra: str = "",
) -> PresenceResult:
    score = _score_value(criterion, full=True)
    first = windows[0] if windows else None
    span = (f"第 {first.start}–{first.end} 页（{first.method}）" if first else "已定位材料页")
    detail = (f"材料存在，内容未核验：{span}，材料页 {len(material)} 页"
              f"（扫描件/正文）；按存在性计 {score} 分。")
    if extra:
        detail += extra
    return PresenceResult(
        state="present", score=score, detail=detail,
        windows=_window_dicts(windows), material_pages=_material_page_dicts(material),
        years=years or {}, reason_code="presence_present",
    )


def _absent(
    criterion: dict, windows: list[LocatedWindow], detail: str,
    years: dict[str, str] | None = None,
) -> PresenceResult:
    return PresenceResult(
        state="absent", score=Decimal(0), detail=detail,
        windows=_window_dicts(windows), material_pages=[],
        years=years or {}, reason_code="presence_absent",
    )


def _unverified(
    criterion: dict, windows: list[LocatedWindow], detail: str,
    years: dict[str, str] | None = None,
) -> PresenceResult:
    return PresenceResult(
        state="unverified", score=None, detail=detail,
        windows=_window_dicts(windows), material_pages=[],
        years=years or {}, reason_code="presence_unverified",
    )


def _marker_ok(win_pages: list[dict], keywords: list[str], cues: list[str]) -> bool:
    head = "\n".join(_text(page)[:300] for page in win_pages[:3])
    return _has_material_cue(head, keywords, cues)


def _year_state(
    year: int, year_windows: dict[int, tuple[int, int]], doc_pages: list[dict],
    criterion: dict, keywords: list[str], cues: list[str],
) -> str:
    if year not in year_windows:
        return "unknown"
    start, end = year_windows[year]
    pages = [page for page in doc_pages if start <= _page_no(page) <= end]
    if not pages:
        return "unknown"
    if any(classify_material_page(page, criterion, keywords=keywords, cues=cues) != "none"
           for page in pages):
        return "present"
    if any(_is_unocr_scan(page) for page in pages):
        return "unknown"
    return "missing"


def _assess_years(
    criterion: dict, windows: list[LocatedWindow], groups: dict[int, list[dict]],
    keywords: list[str], cues: list[str],
) -> PresenceResult:
    scored: list[tuple[int, LocatedWindow, list[dict], list[dict]]] = []
    for window in windows:
        pages = _window_pages(window, groups)
        material = [page for page in pages
                    if classify_material_page(
                        page, criterion, keywords=keywords, cues=cues) != "none"]
        scored.append((len(material), window, pages, material))
    # 多年项的主窗口取“页跨度最大”的候选：财务制度/审计报告是长段材料，
    # 目录里其他含“财务”的短条目（run148 翌星 p87-111）不应抢主窗口。
    scored.sort(key=lambda item: (
        item[1].end - item[1].start, item[0], item[1].confidence), reverse=True)
    _material_count, primary, _pages, _material = scored[0]
    year_sets = _required_year_sets(_rule_text(criterion))
    all_years: set[int] = set()
    for year_set in year_sets:
        all_years |= year_set
    doc_pages = groups.get(primary.document_id, [])
    offset = _document_offset(doc_pages)
    # 年度区间按“全部候选窗口的并集”定位：单个 TOC 年度条目窗口只覆盖该年度，
    # 用它当 main_window 会把其他年度排除在外（合成用例 2022 曾因此 unknown）。
    main_start = min(window.start for window in windows)
    main_end = max(window.end for window in windows)
    year_windows = locate_year_sections(
        doc_pages, all_years, offset, (main_start, main_end))
    decidable_states: dict[str, str] | None = None
    pending_states: dict[str, str] = {}
    has_unknown = False
    for year_set in year_sets:
        states = {year: _year_state(
            year, year_windows, doc_pages, criterion, keywords, cues)
            for year in sorted(year_set)}
        for year, state in states.items():
            pending_states.setdefault(str(year), state)
            if state == "unknown":
                has_unknown = True
        if all(state == "present" for state in states.values()):
            material = [page for page in _window_pages(primary, groups)
                        if classify_material_page(
                            page, criterion, keywords=keywords, cues=cues) != "none"]
            satisfied = {str(year): "present" for year in sorted(year_set)}
            return _present(criterion, [primary], material, satisfied)
        if (decidable_states is None
                and all(state in ("present", "missing") for state in states.values())):
            decidable_states = {str(year): state for year, state in states.items()}
    if not any(item[3] for item in scored):
        if any(_is_unocr_scan(page) for window in windows
               for page in _window_pages(window, groups)):
            return _unverified(
                criterion, windows,
                "材料段存在未 OCR 扫描页，无法确认所需年度材料是否齐全；需人工复核",
                pending_states,
            )
    if decidable_states is not None and not has_unknown:
        return _absent(
            criterion, windows,
            "年度材料段完整可读但缺少所需年度的证明材料（内容未核验）；"
            "按细则“提供不全不得分”计 0 分",
            decidable_states,
        )
    return _unverified(
        criterion, windows,
        "年度完整性无法判定（目录/标题页未定位到全部年度，或含未 OCR 扫描页）；"
        "需人工复核",
        pending_states,
    )


def _assess_single(
    criterion: dict, windows: list[LocatedWindow], groups: dict[int, list[dict]],
    keywords: list[str], cues: list[str],
) -> PresenceResult:
    trusted: list[LocatedWindow] = []
    for window in windows:
        pages = _window_pages(window, groups)
        material = [page for page in pages
                    if classify_material_page(
                        page, criterion, keywords=keywords, cues=cues) != "none"]
        if material:
            return _present(criterion, [window], material)
        if (window.confidence >= 0.8
                or _marker_ok(pages, keywords, cues)):
            trusted.append(window)
    if not trusted:
        return _unverified(
            criterion, windows,
            "材料页定位不可靠（窗口首部未命中评分项/材料线索）；需人工复核",
        )
    scans = [page for window in trusted for page in _window_pages(window, groups)
             if _is_unocr_scan(page)]
    if scans:
        return _unverified(
            criterion, trusted,
            "索引指向的材料段含未 OCR 扫描页，无法确认是否提供；需人工复核",
        )
    first = trusted[0]
    return _absent(
        criterion, trusted,
        f"索引指向第 {first.start}–{first.end} 页，窗口内均为空白/标题页，"
        f"未见扫描件或正文材料（内容未核验）；按未提供计 0 分",
    )


def score_material_presence(criterion: dict, pages: list[dict]) -> PresenceResult:
    """existence/items 评分项的确定性存在性结论。"""
    keywords = presence_keywords(criterion)
    cues = _criterion_cues(criterion)
    windows = locate_windows(pages, criterion, keywords=keywords, cues=cues)
    label = str(criterion.get("name") or "材料")
    if not windows:
        return _unverified(
            criterion, [],
            f"未能在目录/详细评审索引中定位「{label}」的材料页码范围；需人工复核",
        )
    groups = _group_by_doc(pages)
    if _required_year_sets(_rule_text(criterion)):
        return _assess_years(criterion, windows, groups, keywords, cues)
    return _assess_single(criterion, windows, groups, keywords, cues)


# ---------------------------------------------------------------------------
# 样品评审（数量档位）
# ---------------------------------------------------------------------------
def _sample_material(page: dict) -> bool:
    """样品材料只认图像页（照片/设计稿/扫描件）；纯文字“样品要求”不算。"""
    if page.get("known_blank"):
        return False
    try:
        ratio = float(page.get("embedded_image_ratio") or 0)
    except (TypeError, ValueError):
        return False
    if ratio < SAMPLE_IMAGE_MIN_RATIO:
        return False
    text = _text(page).strip()
    return (any(cue in text for cue in _SAMPLE_CUES)
            or len(text) < SCAN_TEXT_MAX_CHARS)


def _sample_share(count: int) -> Decimal:
    if count <= 0:
        return Decimal(0)
    if count <= 2:
        return Decimal("0.3")
    if count <= 5:
        return Decimal("0.6")
    if count <= 9:
        return Decimal("0.8")
    return Decimal("1.0")


def score_sample_materials(
    criterion: dict, pages: list[dict],
) -> PresenceResult | None:
    """样品材料数量档位；定位不到样品材料章节时返回 None（回落 LLM 评分）。"""
    if not is_sample_criterion(criterion):
        return None
    keywords = ["样品评审", "样品", "打样", "设计稿", "样张"]
    windows = locate_windows(pages, criterion, keywords=keywords, cues=list(_SAMPLE_CUES))
    if not windows:
        return None
    groups = _group_by_doc(pages)
    # 标题回退必须落在“样品材料”段（照片/设计稿/样张/打样/彩稿/效果图/实物），
    # 否则“递交样品的要求/封装要求”这类响应正文也会被当作样品材料。
    usable: list[LocatedWindow] = []
    for window in windows:
        if window.method.startswith("heading"):
            head = "\n".join(_text(page)[:300]
                             for page in _window_pages(window, groups)[:3])
            if not any(cue in head for cue in
                       ("照片", "设计稿", "样张", "打样", "彩稿", "效果图", "实物")):
                continue
        usable.append(window)
    if not usable:
        return None
    best: tuple[int, LocatedWindow, list[dict]] | None = None
    for window in usable:
        win_pages = _window_pages(window, groups)
        sample_pages = [page for page in win_pages if _sample_material(page)]
        if sample_pages and (best is None or len(sample_pages) > best[0]):
            best = (len(sample_pages), window, sample_pages)
    if best is None:
        return None
    count, window, sample_pages = best
    maximum = _decimal(criterion.get("max_score")) or Decimal(0)
    share = _sample_share(count)
    score = (maximum * share).quantize(Decimal("0.01"))
    detail = (
        f"按样品材料数量档位自动评分（质量未核验，最终以样品评审专家分为准）："
        f"定位第 {window.start}–{window.end} 页（{window.method}），"
        f"样品材料页 {count} 页（照片/设计稿/扫描件），按 {int(share * 100)}% "
        f"计 {score} 分。"
    )
    return PresenceResult(
        state="present", score=score, detail=detail,
        windows=_window_dicts([window]),
        material_pages=_material_page_dicts(sample_pages),
        reason_code="sample_volume",
    )
