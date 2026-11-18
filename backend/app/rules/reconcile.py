# -*- coding: utf-8 -*-
"""抽取对账 + 缺口补齐：LLM 抽取结果 ↔ 评标细则表格骨架。

多媒体维保项目实证：LLM 对 17 个计分行碎行抽漏/同名合并，总分 91≠100。
骨架（scoring_skeleton.py）是确定性权威台账——本模块在落库前对账：

1. **覆盖对账**：骨架每行的细则文本须被某条 criterion 的 name/description/quote
   命中（规范化子串/前缀回退）；父项粒度抽取（一条 max=组分值）整组判已覆盖，
   不强行拆行（保持抽取粒度）。
2. **缺口补齐**：未覆盖行 → 确定性生成条目（分值取骨架权威值；quote 从页文本
   反查原文 span 生成，保证 verify_citation 可逐字核验；score_type 按主/客观列
   +文本特征映射）。表外报价段无人覆盖 → 生成 formula 项。
3. **分值校正**：1:1 覆盖但分值不符 → 以骨架为准改写（留痕 report，不猜档位）。

骨架解析失败 / 开关关闭 → 原样返回（回退纯 LLM，不劣于现状）。
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from app import config
from app.parsing.normalize import normalize_no_ws, normalize_no_ws_with_map
from app.rules.model import ExtractedCriterion
from app.rules.scoring_skeleton import (
    LedgerEntry,
    ScoreSkeleton,
    SkeletonRow,
    extract_score_skeleton,
)

_RE_PER_ITEM = re.compile(r"每(?:个|项|人|条|家|份)?[^。；，,]{0,12}?得\s*(\d+(?:\.\d+)?)\s*分")
_RE_CERT_KW = re.compile(r"证书|认证|资质|职称|操作证|上岗证")
_RE_PERF_KW = re.compile(r"业绩|案例|合同|项目经验|类似项目")


def _norm(text: str) -> str:
    return normalize_no_ws(text or "")


def _find_span(page_text: str, needle: str) -> tuple[int, int] | None:
    """忽略空白定位 needle 在 page_text 中的原文 [start, end)。"""
    needle_n = _norm(needle)
    if not needle_n:
        return None
    norm, idx = normalize_no_ws_with_map(page_text)
    pos = norm.find(needle_n)
    if pos < 0:
        return None
    return idx[pos], idx[pos + len(needle_n) - 1] + 1


def _quote_for_row(row: SkeletonRow, pages_by_no: dict[int, str]) -> str | None:
    """从页文本反查骨架行细则的逐字原文（quote 须逐字可核验）。"""
    page_text = pages_by_no.get(row.page_no, "")
    texts = [page_text] if page_text else []
    texts += [t for p, t in sorted(pages_by_no.items()) if p != row.page_no]
    for hay in texts:
        span = _find_span(hay, row.text)
        if span:
            return hay[span[0]:span[1]]
        # 前缀回退（表格列文字插入打断整句时）
        core = _norm(row.text)
        for size in (40, 24, 14):
            if len(core) <= size:
                continue
            prefix = row.text[: size + 4]  # 归一化前多取几个字符弥补空白
            span = _find_span(hay, prefix)
            if span:
                return hay[span[0]:span[1]]
    return None


# ---------------------------------------------------------------------------
# 覆盖判定
# ---------------------------------------------------------------------------

def _row_cores(row: SkeletonRow) -> list[str]:
    """行文本的特征串（去 '（N）' 序号前缀后取多个切片，容忍中段差异）。"""
    t = re.sub(r"^[（(]\d{1,2}[）)]\s*", "", row.text or "")
    cores = []
    if len(_norm(t)) >= 8:
        cores.append(t[:40])
    if len(_norm(t)) >= 24:
        cores.append(t[8:48])
    return cores or [t]


def _covers(crit_text_n: str, row: SkeletonRow) -> bool:
    if not crit_text_n:
        return False
    for core in _row_cores(row):
        core_n = _norm(core)
        if core_n and core_n in crit_text_n:
            return True
    return False


def _crit_text(c: ExtractedCriterion) -> str:
    return _norm(f"{c.name}\n{c.description or ''}\n{c.quote or ''}\n{c.required_evidence or ''}")


# 计分类型（进总分）；match/veto 是判断项，不计分（validators ⑨ 同口径）
SCORING_TYPES = ("formula", "count", "subjective")

# 严格章节标记："X部分（N 分）"（带分值后缀才算；正文"3.3 报价部分。"、"商务技术部分、"不算）
_RE_SECTION_MARK = re.compile(r"(商务|技术|价格|报价)部分\s*[（(]\s*\d+(?:\.\d+)?\s*分")
_SECTION_CAT = {"商务": "commercial", "技术": "technical",
                "价格": "price", "报价": "price"}


class _PageIndex:
    """页文本的归一化索引 + 章节标记（一次构建，逐行查询）。"""

    def __init__(self, pages_by_no: dict[int, str]):
        self.ranges: dict[int, tuple[int, int]] = {}
        self.full = "\n".join(pages_by_no[p] for p in sorted(pages_by_no))
        parts: list[str] = []
        pos = 0
        for pno in sorted(pages_by_no):
            n = _norm(pages_by_no[pno] or "")
            self.ranges[pno] = (pos, pos + len(n))
            parts.append(n)
            pos += len(n)
        self.full_n = "".join(parts)
        # 章节标记（归一化坐标）
        self.marks: list[tuple[int, str]] = []
        for m in _RE_SECTION_MARK.finditer(self.full):
            self.marks.append((len(_norm(self.full[: m.start()])), _SECTION_CAT[m.group(1)]))

    def category_at(self, norm_pos: int) -> str | None:
        best, best_pos = None, -1
        for pos, cat in self.marks:
            if pos <= norm_pos and pos > best_pos:
                best_pos, best = pos, cat
        return best

    def find(self, probe: str, page_no: int | None = None) -> int | None:
        pn = _norm(probe)
        if not pn:
            return None
        if page_no in self.ranges:
            s, e = self.ranges[page_no]
            local = self.full_n.find(pn, s, e)
            if local >= 0:
                return local
        found = self.full_n.find(pn)
        return found if found >= 0 else None


def _category_for_row(row: SkeletonRow, index: "_PageIndex") -> str | None:
    """骨架行 → 类别（行文本所在页的章节位置推导；无把握返回 None）。"""
    for probe in [row.text] + _row_cores(row) + [row.group]:
        if not probe:
            continue
        pos = index.find(probe, row.page_no)
        if pos is None:
            pos = index.find(probe)
        if pos is not None:
            return index.category_at(pos)
    return None


# ---------------------------------------------------------------------------
# 缺口生成
# ---------------------------------------------------------------------------

def _category_for_group(group: str, pages_by_no: dict[int, str]) -> str:
    """按「商务/技术/报价部分」标题在全文中的位置给组定类别。"""
    full = "\n".join(t for _, t in sorted(pages_by_no.items()))
    full_n = _norm(full)
    gpos = full_n.find(_norm(group))
    best, best_pos = "technical", -1
    for m in re.finditer(r"(商务|技术|价格|报价)部分", full):
        label = m.group(1)
        pos = len(_norm(full[: m.start()]))
        if pos <= gpos and pos > best_pos:
            best_pos = pos
            best = {"商务": "commercial", "技术": "technical",
                    "价格": "price", "报价": "price"}[label]
    return best


def _score_type_for_row(row: SkeletonRow) -> tuple[str, dict]:
    """骨架行 → (score_type, params 补充)。「主/客观分」列是 ground truth
    （2026-11-18 用户拍板全面覆盖：列值决定 score_type 族，不信 LLM 自报）。

    客观行按 check_infer.classify_scoring_form 的明示形态落 count 族
    （多档 tiers / 扣分式 deviation_deduct_table / per_item / 二值存在）；
    形态不明与全有全无 → subjective + _subjectivity_hint=objective 留痕
    （不猜、不硬映射 count——count 事实层无计数对象会悬置不进总分）。
    """
    if row.subjectivity == "subjective":
        return "subjective", {"_subjectivity_hint": "subjective"}
    text = row.text or ""
    # CMMI 认证独立计分项（"具有有效期内CMMI 认证二级及以上得 N 分"）：
    # 存在性即得分、单一证书。LLM 编译曾把此行标 match（判断项不计分）→
    # CMMI 满分 2 分整项丢失（2026-10-30 run 66 实证：四家 CMMI 2 分全丢，
    # 真实 云锦/拓扑/用友 5 = ISO3+CMMI2，系统只有 c47 ISO 的 3/3/3/1）。
    _m_cmmi = re.search(r"(?:具有|具备|通过|获得|拥有|提供)[^。；]{0,30}?(?:得|计)\s*(\d+(?:\.\d+)?)\s*分", text)
    if "CMMI" in text and _m_cmmi:
        return "count", {"field": "cmmi", "per_item": str(Decimal(_m_cmmi.group(1)))}
    from app.rules.check_infer import classify_scoring_form

    form = classify_scoring_form(text)
    if form is not None:
        kind, params = form
        col_hint = {"_subjectivity_hint": "objective"} if row.subjectivity == "objective" else {}
        if kind == "all_or_nothing":
            # 客观全有全无（"任一不能满足得0分、全部符合得满分"，如样品评审）：
            # 判定对象是实体样品/整体符合性，文本无计数对象 → subjective 引擎
            # 代判 + 客观标注留痕（OBJECTIVE_AS_SUBJECTIVE warn 披露）。
            return "subjective", {"_subjectivity_hint": "objective",
                                  "_objective_form": "all_or_nothing"}
        return "count", {**col_hint, **params, "_objective_form": kind}
    if row.subjectivity == "objective":
        # 客观列但形态不可判 → subjective 代判 + 客观标注留痕（不猜计数口径）
        return "subjective", {"_subjectivity_hint": "objective",
                              "_objective_form": "unparsed_objective"}
    return "subjective", {}


def _generate_for_row(row: SkeletonRow, pages_by_no: dict[int, str],
                      category: str) -> ExtractedCriterion:
    # 三级表分值 cell 里的子项名是权威条目名（"技术研发（0-2 分）"→技术研发）；
    # 无子项名退组名+子序号（浙江多媒体既有命名口径）
    if row.sub_name:
        label = row.sub_name
    else:
        label = f"{row.group}"
        if row.sub_idx is not None:
            label += f"（{row.sub_idx}）"
    first = re.split(r"[。；;]", row.text or "")
    hint = (first[0] if first else "")[:24]
    # 去 hint 开头的序号（细则文本常见「（1）投标人具有…」→ 与 label 的
    # 子序号拼接成「投标人资质实力（1）（1）投标人具有…」污染，2026-10-27 实证）
    hint = re.sub(r"^[（(]\s*\d{1,2}\s*[）)]\s*", "", hint)
    name = f"{label}{hint}" if hint else label
    score_type, params = _score_type_for_row(row)
    return ExtractedCriterion(
        name=name[:200],
        category=category,  # type: ignore[arg-type]
        score_type=score_type,  # type: ignore[arg-type]
        max_score=row.max_score,
        description=row.text,
        required_evidence=row.required_evidence,
        params=params,
        quote=_quote_for_row(row, pages_by_no),
        page_hint=row.page_no,
        confidence=1.0,
    )


def _generate_for_price(entry: LedgerEntry, pages_by_no: dict[int, str],
                        existing: list[ExtractedCriterion]) -> ExtractedCriterion | None:
    """表外报价段无人覆盖 → 生成 formula 项（参数尽量从原文确定性解析）。"""
    from app.rules.compiler import parse_price_formula_text

    parsed = parse_price_formula_text(entry.text)
    c = ExtractedCriterion(
        name=f"{entry.label}评分",
        category="price",
        score_type="formula",
        max_score=entry.max_score,
        description=entry.text,
        # 2026-10-28：未识别则不写默认形态（旧 or "deviation_deduct"/or "average"
        # 把无法识别的价格句式静默按偏离+算术平均生成，是静默算错入口）——
        # 由 PRICE_FORMULA_UNRECOGNIZED 阻断，交人工确认。
        formula_type=parsed.get("formula_type"),
        benchmark_method=parsed.get("benchmark_method"),
        quote=_quote_for_row(
            SkeletonRow(group=entry.label, group_total=entry.max_score,
                        sub_idx=None, subjectivity=None,
                        max_score=entry.max_score, text=entry.text,
                        page_no=entry.page_no),
            pages_by_no),
        page_hint=entry.page_no,
        confidence=1.0,
    )
    for key in ("k", "upper_bound", "lower_bound", "trim_ratio", "trim_count",
                "trim_threshold", "deduct_rate", "deduct_rate_down",
                "deduct_rate_up", "rounding"):
        val = parsed.get(key)
        if val is not None:
            setattr(c, key, val)
    return c


# ---------------------------------------------------------------------------
# 片段覆盖对账（P0，2026-11-18 测试7-1 实证）
# ---------------------------------------------------------------------------
# 形态：LLM 把一个骨架计分行拆成多条子项（"ISO27001 得1分""ISO20000 得1分"
# "ISO9000 得1分"），每条引文只是该行原文的一个分句。`_covers` 要求 40 字符
# 切片命中，三条都太短 → 该行被判「未覆盖」→ 缺口补齐又生成一条整行父项，
# 同一行计两次（103≠100）。这里只在**片段集合恰好闭合该行满分**时把片段登记
# 为覆盖；其余情况（分值不符/覆盖不足/重复引文/跨行歧义）一律只披露不改数，
# 由 TOTAL_SCORE 阻断人工处理——绝不静默删条目或猜分值。
_RE_MATERIAL_HEAD = re.compile(r"【\s*证明材料\s*】")
_RE_SCORE_ANCHOR = re.compile(r"得\s*\d|扣\s*\d|计\s*\d|满分|不得分")
_FRAGMENT_MIN_LEN = 8            # 归一化引文下限（与 _row_cores 的 8 字符同口径）
_FRAGMENT_LONG_LEN = 16          # 超过此长度不再要求计分锚点
_FRAGMENT_COVER_RATIO = 0.6      # 片段并集须覆盖评分正文的比例
_FRAGMENT_TYPES = ("count", "subjective")  # formula 不参与：价格 ledger 语义独立


def _row_label(row: SkeletonRow) -> str:
    name = row.group or ""
    return f"{name}｜{(row.text or '')[:24]}"


def _row_scoring_body(row: SkeletonRow) -> str:
    """行正文（去掉【证明材料】段与序号前缀）——片段必须命中计分正文。"""
    text = row.text or ""
    m = _RE_MATERIAL_HEAD.search(text)
    if m:
        text = text[: m.start()]
    text = re.sub(r"^\s*[（(]\d{1,2}[）)]\s*", "", text)
    return _norm(text)


def _fragment_probes(c: ExtractedCriterion) -> list[str]:
    """候选引文（quote → description → name）；短引文必须带计分锚点。"""
    probes: list[str] = []
    for raw in (c.quote, c.description, c.name):
        p = _norm(raw or "")
        if not p or len(p) < _FRAGMENT_MIN_LEN:
            continue
        if len(p) < _FRAGMENT_LONG_LEN and not _RE_SCORE_ANCHOR.search(p):
            continue
        probes.append(p)
    return probes


def _fragment_page_ok(c: ExtractedCriterion, row: SkeletonRow) -> bool:
    try:
        ph = int(c.page_hint) if c.page_hint is not None else 0
    except (TypeError, ValueError):
        ph = 0
    try:
        rp = int(row.page_no or 0)
    except (TypeError, ValueError):
        rp = 0
    if ph <= 0 or rp <= 0:
        return True
    return abs(ph - rp) <= 1


def _union_coverage(body_n: str, probes: list[str]) -> float:
    """片段并集（首次出现区间合并）覆盖评分正文的比例。"""
    if not body_n:
        return 0.0
    spans: list[tuple[int, int]] = []
    for p in probes:
        pos = body_n.find(p)
        if pos >= 0:
            spans.append((pos, pos + len(p)))
    if not spans:
        return 0.0
    spans.sort()
    merged = 0
    cur_s, cur_e = spans[0]
    for s, e in spans[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            merged += cur_e - cur_s
            cur_s, cur_e = s, e
    merged += cur_e - cur_s
    return merged / len(body_n)


def _reconcile_fragments(
    criteria: list[ExtractedCriterion],
    rows: list[SkeletonRow],
    *,
    covered_by: dict[int, list[int]],
    scoring_idx: list[int],
    rows_of: dict[int, set[int]],
    dropped_idx: set[int],
    index: "_PageIndex | None",
    report: dict[str, Any],
) -> None:
    """片段覆盖 pass：只原地登记 covered_by，其余只写 report（不改分值/不删条目）。"""
    def scoring_covered(ri: int) -> bool:
        return any(criteria[ci].score_type in SCORING_TYPES for ci in covered_by[ri])

    bodies = {ri: _row_scoring_body(row) for ri, row in enumerate(rows)}

    # 诊断：同一行被 ≥2 条计分条目完整覆盖——只披露，不自动去重
    for ri, row in enumerate(rows):
        cis = [ci for ci in covered_by[ri]
               if ci not in dropped_idx and criteria[ci].score_type in SCORING_TYPES]
        if len(cis) >= 2:
            report["multi_covered_rows"].append({
                "row_index": ri, "row": _row_label(row),
                "criteria": [criteria[ci].name for ci in cis],
                "sum": float(sum((criteria[ci].max_score or Decimal(0)) for ci in cis)),
            })

    uncovered = {ri for ri in range(len(rows)) if not scoring_covered(ri)}

    # 片段识别：未完整覆盖任何行 + 引文唯一命中某行评分正文
    groups: dict[int, list[tuple[int, list[str]]]] = {}
    for ci in scoring_idx:
        c = criteria[ci]
        if (ci in dropped_idx or rows_of.get(ci) or c.is_veto
                or c.score_type not in _FRAGMENT_TYPES
                or c.category == "price" or c.max_score is None):
            continue
        probes = _fragment_probes(c)
        if not probes:
            continue
        hit: int | None = None
        ambiguous: list[int] = []
        for p in probes:
            hits = [ri for ri, body in bodies.items()
                    if p in body and _fragment_page_ok(c, rows[ri])]
            if len(hits) == 1:
                hit = hits[0]
                break
            if len(hits) > 1:
                ambiguous = hits
                break
        if hit is None:
            if ambiguous:
                report["fragment_ambiguous"].append({
                    "name": c.name, "max_score": float(c.max_score),
                    "rows": [_row_label(rows[ri]) for ri in ambiguous]})
                continue
            shadow = [ri for ri in range(len(rows))
                      if ri not in uncovered
                      and any(p in bodies[ri] for p in probes)
                      and _fragment_page_ok(c, rows[ri])]
            if shadow:
                report["fragment_shadowed"].append({
                    "name": c.name, "max_score": float(c.max_score),
                    "rows": [_row_label(rows[ri]) for ri in shadow]})
            continue
        if hit not in uncovered:
            report["fragment_shadowed"].append({
                "name": c.name, "max_score": float(c.max_score),
                "rows": [_row_label(rows[hit])]})
            continue
        matched = [p for p in probes if p in bodies[hit]]
        groups.setdefault(hit, []).append((ci, matched))

    # 逐行裁决：只有片段分值严格闭合行满分且并集覆盖足够才登记
    for ri in sorted(groups):
        row = rows[ri]
        if row.max_score is None or row.max_score <= 0:
            continue
        pairs = groups[ri]
        cis = [ci for ci, _ in pairs]
        probes = [p for _, ps in pairs for p in ps]
        primary = [ps[0] for _, ps in pairs if ps]
        total = sum((criteria[ci].max_score or Decimal(0)) for ci in cis)
        coverage = _union_coverage(bodies[ri], probes)
        base = {"row_index": ri, "row": _row_label(row),
                "fragments": [criteria[ci].name for ci in cis]}
        duplicate = len(set(primary)) < len(primary)
        if not duplicate and total == row.max_score and coverage >= _FRAGMENT_COVER_RATIO:
            for ci in cis:
                if ci not in covered_by[ri]:
                    covered_by[ri].append(ci)
            report["fragment_covered"].append(
                {**base, "sum": float(total), "coverage": round(coverage, 3)})
            # 与既有 category 校正同口径（该 pass 已跑过，此处补登记片段）
            if index is not None:
                cat = _category_for_row(row, index)
                if cat is not None:
                    for ci in cis:
                        c = criteria[ci]
                        if c.category != cat:
                            report["category_fixed"].append(
                                {"name": c.name, "from": c.category, "to": cat})
                            c.category = cat
        else:
            reason = ("duplicate_probe" if duplicate
                      else "sum_mismatch" if total != row.max_score
                      else "low_coverage")
            report["fragment_unmatched"].append(
                {**base, "fragment_sum": float(total),
                 "row_max": float(row.max_score),
                 "coverage": round(coverage, 3), "reason": reason})


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def reconcile_with_skeleton(
    criteria: list[ExtractedCriterion],
    *,
    document_id: int,
    pages: list[tuple[int, str]] | None = None,
    skeleton: ScoreSkeleton | None = None,
) -> tuple[list[ExtractedCriterion], dict[str, Any]]:
    """对账补齐。返回 (新 criteria 列表, report 字典)。

    骨架不可用/未启用 → 原样返回（report["skipped"]=True）。
    """
    report: dict[str, Any] = {"skipped": True}
    if not config.scoring_skeleton_enabled():
        return criteria, report

    if skeleton is None:
        from app.db.models import Document
        from app.db.session import session_scope
        from sqlalchemy import select as sa_select

        with session_scope() as s:
            doc = s.get(Document, document_id)
            stored = doc.stored_path if doc else ""
        if not stored:
            return criteria, report
        if pages is None:
            from app.rules.compiler import _load_tender_pages

            pages = _load_tender_pages(document_id)
        skeleton = extract_score_skeleton(stored, pages)
    if skeleton is None:
        return criteria, report

    pages_by_no = dict(pages or [])
    index = _PageIndex(pages_by_no) if pages_by_no else None
    report = {"skipped": False,
              "rows": len(skeleton.rows),
              "target_total": float(skeleton.target_total()),
              "generated": [], "score_fixed": [], "price_generated": False,
              "score_type_fixed": [], "category_fixed": [],
              "overcoverage_pruned": [], "objective_as_subjective": [],
              "fragment_covered": [], "fragment_unmatched": [],
              "fragment_ambiguous": [], "fragment_shadowed": [],
              "multi_covered_rows": []}

    texts = [(c, _crit_text(c)) for c in criteria]
    # 行 → 覆盖它的 criteria（前缀/子串命中）
    covered_by: dict[int, list[int]] = {i: [] for i in range(len(skeleton.rows))}
    for ci, (_c, tn) in enumerate(texts):
        for ri, row in enumerate(skeleton.rows):
            if _covers(tn, row):
                covered_by[ri].append(ci)

    # 父项粒度：name 含组名 且 分值==组分值 → 整组覆盖
    for ci, (c, tn) in enumerate(texts):
        for gi, group in enumerate(sorted({r.group for r in skeleton.rows})):
            rows_g = [i for i, r in enumerate(skeleton.rows) if r.group == group]
            totals = {skeleton.rows[i].group_total for i in rows_g}
            gtotal = next(iter(totals)) if len(totals) == 1 else None
            if gtotal is None or c.max_score != gtotal:
                continue
            if _norm(group) in _norm(c.name) or any(covered_by[i] and ci in covered_by[i] for i in rows_g):
                for i in rows_g:
                    if ci not in covered_by[i]:
                        covered_by[i].append(ci)

    # score_type 校正（2026-11-18 列覆盖全面归正，用户拍板「主/客观分列=ground truth」）：
    # ① 骨架行有主/客观列标注、被 1:1 覆盖 → 按列+形态决策树归正（不信 LLM 自报；
    #    利群实证：财务制度/样品评审列标客观分却成 subjective）；
    # ② match（判断项，不计分）覆盖计分行 → 必然改写——match 带分值自相矛盾，
    #    分值永远兑现不了（利群 7 条实证 Σ 恒缺 15；prompt"match（满足条件得分）"
    #    误导是历史病根）。
    # veto/formula 不动；非 1:1 覆盖走缺口补齐（多行覆盖的合并项留原样）。
    retyped: set[int] = set()
    for ri, row in enumerate(skeleton.rows):
        cis = covered_by[ri]
        if len(cis) != 1:
            continue
        ci = cis[0]
        if ci in retyped:
            continue
        c = criteria[ci]
        if c.is_veto or c.score_type == "formula":
            continue
        others = [j for j, lst in covered_by.items() if j != ri and ci in lst]
        if others:
            continue  # 多行覆盖（父项/合并项）不动
        is_match = c.score_type == "match"
        has_column = row.subjectivity is not None
        if not (is_match or has_column):
            continue
        new_type, params = _score_type_for_row(row)
        if not is_match and new_type == c.score_type:
            # 与列一致：只补参数缺口（既有显式参数优先，不覆盖）
            if params:
                merged = dict(c.params or {})
                for k, v in params.items():
                    merged.setdefault(k, v)
                c.params = merged
            retyped.add(ci)
            continue
        report["score_type_fixed"].append(
            {"name": c.name, "from": c.score_type, "to": new_type,
             "by": "subj_column" if has_column else "form"})
        c.score_type = new_type  # type: ignore[assignment]
        if params:
            c.params = {**(c.params or {}), **params}
        retyped.add(ci)

    # 1:1 分值校正（恰一条 criterion 覆盖该行、且该 criterion 只覆盖这一行）
    for ri, row in enumerate(skeleton.rows):
        cis = covered_by[ri]
        if len(cis) != 1:
            continue
        c = criteria[cis[0]]
        others = [j for j, lst in covered_by.items() if j != ri and cis[0] in lst]
        if others:
            continue  # 多行覆盖（父项/合并项）不动
        if c.max_score != row.max_score and c.score_type != "veto":
            report["score_fixed"].append(
                {"name": c.name, "from": float(c.max_score) if c.max_score is not None else None,
                 "to": float(row.max_score)})
            c.max_score = row.max_score

    # category 校正：覆盖条目的类别以骨架章节位置为准（"1、商务部分（10 分）"标记定位）。
    # LLM 自报类别不稳（实测 c31 培训计划被标 commercial，node=None 时 category_map 管不到）。
    if index is not None:
        for ci, c in enumerate(criteria):
            if c.is_veto or c.score_type not in SCORING_TYPES:
                continue
            rows_cov = [ri for ri, lst in covered_by.items() if ci in lst]
            if not rows_cov:
                continue
            cats = {_category_for_row(skeleton.rows[ri], index) for ri in rows_cov}
            cats.discard(None)
            if len(cats) == 1:
                row_cat = cats.pop()
                if c.category != row_cat:
                    report["category_fixed"].append(
                        {"name": c.name, "from": c.category, "to": row_cat})
                    c.category = row_cat  # type: ignore[assignment]

    # 过覆盖裁剪（父子双计）：LLM 常同时抽出组父项（max=组分值）与子项
    # （4+4+2），两条都计分 → Σ 超出细则权威值（run 54 pkg22 Σ=109≠100 实证）。
    # 骨架行是权威：一条计分 criterion 覆盖的行集若被更细的计分条目完整平铺
    # 且分值之和相等 → 丢粗粒度保细粒度（评分粒度更有用），report 留痕。
    dropped_idx: set[int] = set()
    scoring_idx = [
        i for i, c in enumerate(criteria)
        if c.score_type in SCORING_TYPES and not c.is_veto
    ]
    rows_of = {i: {ri for ri, lst in covered_by.items() if i in lst} for i in scoring_idx}
    for i in scoring_idx:
        rows_i = rows_of[i]
        if len(rows_i) < 2:
            continue
        children = [j for j in scoring_idx if j != i and rows_of[j] and rows_of[j] < rows_i]
        remain = set(rows_i)
        chosen: list[int] = []
        for j in sorted(children, key=lambda j: len(rows_of[j]), reverse=True):
            if rows_of[j] <= remain:
                chosen.append(j)
                remain -= rows_of[j]
        if not remain and chosen:
            sum_children = sum((criteria[j].max_score or Decimal(0)) for j in chosen)
            if sum_children == (criteria[i].max_score or Decimal(0)):
                dropped_idx.add(i)
                report.setdefault("overcoverage_pruned", []).append(
                    {"name": criteria[i].name,
                     "kept": [criteria[j].name for j in chosen],
                     "max_score": float(criteria[i].max_score or 0)}
                )

    # 缺口补齐（match/veto 覆盖不计分——重写后仍无计分覆盖的行才算缺口）
    def _scoring_covered(ri: int) -> bool:
        return any(criteria[ci].score_type in SCORING_TYPES for ci in covered_by[ri])

    # 片段覆盖对账（必须在生成循环之前：片段已足额闭合的行不再生成整行父项）
    _reconcile_fragments(
        criteria, skeleton.rows,
        covered_by=covered_by, scoring_idx=scoring_idx,
        rows_of=rows_of, dropped_idx=dropped_idx,
        index=index, report=report,
    )

    out = [c for i, c in enumerate(criteria) if i not in dropped_idx]
    for ri, row in enumerate(skeleton.rows):
        if _scoring_covered(ri):
            continue
        category = ((_category_for_row(row, index) if index is not None else None)
                    or _category_for_group(row.group, pages_by_no))
        gen = _generate_for_row(row, pages_by_no, category)
        out.append(gen)
        report["generated"].append(gen.name)

    # 表外报价段（「商务报价部分」等复合组名用包含判定，不只看行首）
    for entry in skeleton.ledger:
        if "报价" in entry.label or "价格" in entry.label:
            has_price = any(
                c.score_type == "formula" or c.category == "price" for c in out)
            if not has_price:
                gen = _generate_for_price(entry, pages_by_no, out)
                if gen is not None:
                    out.append(gen)
                    report["price_generated"] = True

    # 客观列但落 subjective 代判的条目（全有全无/形态不可判）——诚实披露通道
    for c in out:
        of = (c.params or {}).get("_objective_form")
        if of in ("all_or_nothing", "unparsed_objective"):
            report["objective_as_subjective"].append(c.name)

    total = sum((c.max_score or Decimal(0) for c in out
                 if c.score_type in ("formula", "count", "subjective")), Decimal(0))
    report["final_sum"] = float(total)
    return out, report
