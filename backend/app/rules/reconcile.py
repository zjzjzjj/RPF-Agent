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
    """骨架行 → (score_type, params 补充)。count 仅在有明确按件计分语言时。

    2026-10-27 修复：资质/证书**存在性即得分**行（"投标人具有 ISO9001…得 2 分"，
    无"每…得"语言）此前落到 subjective——证书复印件是确定性材料，应按 count/
    certificates 计数（有证书复印件就计 1 项），交 LLM 主观评必然偏低且不可复算。
    """
    if row.subjectivity == "subjective":
        return "subjective", {}
    text = row.text or ""
    # CMMI 认证独立计分项（"具有有效期内CMMI 认证二级及以上得 N 分"）：
    # 存在性即得分、单一证书。LLM 编译曾把此行标 match（判断项不计分）→
    # CMMI 满分 2 分整项丢失（2026-10-30 run 66 实证：四家 CMMI 2 分全丢，
    # 真实 云锦/拓扑/用友 5 = ISO3+CMMI2，系统只有 c47 ISO 的 3/3/3/1）。
    _m_cmmi = re.search(r"(?:具有|具备|通过|获得|拥有|提供)[^。；]{0,30}?(?:得|计)\s*(\d+(?:\.\d+)?)\s*分", text)
    if "CMMI" in text and _m_cmmi:
        return "count", {"field": "cmmi", "per_item": str(Decimal(_m_cmmi.group(1)))}
    m = _RE_PER_ITEM.search(text)
    if m:
        if _RE_CERT_KW.search(text):
            field = "certificates"
        elif _RE_PERF_KW.search(text):
            field = "performance"
        else:
            return "subjective", {}  # 计数对象不明 → 交 LLM 主观评，不猜
        params: dict[str, Any] = {"field": field, "per_item": m.group(1)}
        cap = re.search(r"最高(?:不超过|得)?\s*(\d+(?:\.\d+)?)\s*分", text)
        if cap:
            per = Decimal(m.group(1))
            if per > 0:
                params["max_items"] = str(int(Decimal(cap.group(1)) / per))
        return "count", params
    # 存在性即得分："具有/具备/通过 ISO9001…得 N 分"或"提供证书…得 N 分"——
    # 按 evidence/ 证书清单计数（1 项 = 该项得分，per_item 用满分 1:1）。
    # 只认客观类 + 细则点名硬材料 + 得分语言，杜绝误判主观行。
    if (
        _RE_CERT_KW.search(text)
        and re.search(r"(?:具有|具备|通过|获得|拥有|提供)[^。；]{0,12}?(?:得|计)\s*(\d+(?:\.\d+)?)\s*分", text)
    ):
        return "count", {"field": "certificates", "per_item": "1"}
    return "subjective", {}


def _generate_for_row(row: SkeletonRow, pages_by_no: dict[int, str],
                      category: str) -> ExtractedCriterion:
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
              "overcoverage_pruned": []}

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

    # score_type 校正：骨架是计分行（分值列有值），被 match（判断项，不计分）覆盖时
    # 分值永远兑现不了——按骨架映射改写为计分类型（prompt 里"match（满足条件得分）"
    # 误导 LLM 把"承诺X得2分"型计分行分给了 match，实测 Σ 96≠100 的 4 分缺口即此）。
    # veto（一票否决）不动；非 1:1 的 match 覆盖走缺口补齐。
    retyped: set[int] = set()
    for ri, row in enumerate(skeleton.rows):
        cis = covered_by[ri]
        if any(criteria[ci].score_type in SCORING_TYPES for ci in cis):
            continue
        match_cis = [ci for ci in cis
                     if criteria[ci].score_type == "match" and not criteria[ci].is_veto]
        if len(match_cis) != 1 or len(cis) != 1:
            continue  # 多覆盖/含 veto → 不动，走缺口补齐
        ci = match_cis[0]
        if ci in retyped:
            continue
        # 只改写 1:1 的 match（多行覆盖的 match 留作判断项，逐行走缺口）
        others = [j for j, lst in covered_by.items() if j != ri and ci in lst]
        if others:
            continue
        c = criteria[ci]
        new_type, params = _score_type_for_row(row)
        report["score_type_fixed"].append(
            {"name": c.name, "from": "match", "to": new_type})
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

    total = sum((c.max_score or Decimal(0) for c in out
                 if c.score_type in ("formula", "count", "subjective")), Decimal(0))
    report["final_sum"] = float(total)
    return out, report
