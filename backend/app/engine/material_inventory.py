# -*- coding: utf-8 -*-
"""Deterministic bid-material inventory for objective scoring.

The managed executor historically asked the LLM to extract every fact *and*
bind every fact to an exact source id.  A single binding failure discarded the
whole cell even when the contract, certificate or personnel table was plainly
present in the bid.  This module builds a page-bound inventory once per bidder
so the objective engines can count what the documents actually contain.

Design rules:
- Deterministic only: regex / PyMuPDF tables / existing case_evidence helpers.
- Every item carries the page number(s) that support it.
- Absence is only reported when the relevant section was located and readable;
  otherwise the caller falls back to the LLM path (never a fabricated zero).
- No project-specific codes or hardcoded bidder answers.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.engine.facts import BidFacts, extract_bid_facts
from app.engine.objective import normalize_cert


@dataclass
class ContractRecord:
    name: str
    amount_yuan: Decimal | None = None
    signed_date: str | None = None
    period: str | None = None
    pages: list[int] = field(default_factory=list)
    materials: dict[str, list[int]] = field(default_factory=dict)
    # 三态材料链（2026-11-24）：True=材料在 / False=可读确认缺 / None=不可读
    # 或未绑上。None 不得判缺（run155 实证：绑定断链把"在"误判"缺"整列 0）。
    materials_state: dict[str, bool | None] = field(default_factory=dict)
    # 图上/文本所见项目类型原词（"运维""系统集成"…，只转述不归类）——类型闸输入
    project_type: str | None = None
    # VL 人员归属/人员材料（2026-11-25 翻案通道收口）：person_names=材料上出现的
    # 评分人员姓名并集；person_states 四键三态（work_proof/experience_proof/
    # social_security/cert_proof），只升不降（VL True 可翻案）
    person_names: list[str] = field(default_factory=list)
    person_states: dict[str, bool | None] = field(default_factory=dict)
    person_proofs: dict[str, dict[str, bool]] = field(default_factory=dict)
    amount_corrected: bool = False   # 金额伪迹补正留痕（文本<1000 且 VL≥1000）
    source: str = "text"


@dataclass
class CertRecord:
    name: str
    normalized: str
    level: str | None = None
    valid_until: str | None = None
    pages: list[int] = field(default_factory=list)
    backing: bool | None = None
    valid_from: str | None = None
    certificate_number: str | None = None
    appraisal_id: str | None = None
    appraisal_is_lookup: bool = False
    document_id: int | None = None


@dataclass
class PersonRecord:
    name: str
    role: str = ""
    title: str = ""
    certs: list[str] = field(default_factory=list)
    years: Decimal | None = None
    experience: str = ""
    experience_pages: list[int] = field(default_factory=list)
    social_security: bool | None = None
    pages: list[int] = field(default_factory=list)
    social_months: int | None = None
    social_months_from_register: bool = False


@dataclass
class ValueItem:
    name: str
    page: int


@dataclass
class MaterialInventory:
    bidder_id: int
    pages: list[dict]
    full_text: str
    facts: BidFacts
    contracts: list[ContractRecord] = field(default_factory=list)
    certs: list[CertRecord] = field(default_factory=list)
    persons: list[PersonRecord] = field(default_factory=list)
    value_items: list[ValueItem] = field(default_factory=list)
    cert_section_present: bool = False
    cert_backing: dict[str, bool | None] = field(default_factory=dict)
    platform_query_certificates: set[str] = field(default_factory=set)
    # PR3（2026-11-24）：目录驱动定位页集（kind→页号）——存在性核验页源；
    # shadow=只审计，on=供护栏/判定消费
    locator_index: dict[str, set[int]] = field(default_factory=dict)
    performance_section_found: bool = False
    personnel_section_found: bool = False
    personnel_table_found: bool = False
    personnel_detail_found: bool = False
    value_section_found: bool = False
    audit: dict[str, Any] = field(default_factory=dict)

    def to_bid_facts(self) -> BidFacts:
        """Shape the inventory like the model-fact contract consumed by engines."""
        facts = extract_bid_facts(self.full_text)
        facts.performance_amounts = [
            c.amount_yuan for c in self.contracts if c.amount_yuan is not None
        ]
        cert_names: list[str] = []
        for cert in self.certs:
            key = cert.normalized or normalize_cert(cert.name)
            if key and key not in cert_names:
                cert_names.append(key)
        facts.certificates = cert_names
        facts.person_attributes = [
            {
                "name": p.name,
                "role": p.role,
                "title": p.title,
                "years": str(p.years) if p.years is not None else None,
                "certs": list(p.certs),
                "pages": list(p.pages),
            }
            for p in self.persons
        ]
        return facts

    def page_text(self, page_no: int) -> str:
        for page in self.pages:
            if int(page.get("page_no") or 0) == int(page_no):
                return str(page.get("text") or "")
        return ""


# ---------------------------------------------------------------------------
# Contract / performance evidence
# ---------------------------------------------------------------------------
_RE_CONTRACT_HEADING = re.compile(
    r"^\s*(?:[（(]?\d{1,3}[）)、.．]\s*)?(20\d{2})\s*年\s*([^\n]{2,50}?合同)\s*$",
    re.M,
)
_RE_CONTRACT_HEADING_NO_YEAR = re.compile(
    r"^\s*(?:[（(]?\d{1,3}[）)、.．]\s*)?([^\n]{2,50}?合同)\s*$", re.M,
)
_RE_CONTRACT_AMOUNT = re.compile(
    r"(?:合同(?:价款|金额|总价|价格)|成交金额|中标金额|采购金额|结算金额)"
    r"[^。\n]{0,30}?"
    r"(?:人民币|￥|¥)?\s*([0-9][0-9,，]*(?:\.[0-9]+)?)\s*(万元|元)",
)
# PR1（2026-11-24 run155"合同（无金额）"实证）：¥ 前缀金额兜底（保守，
# 不做无标签裸数字——税率/日期会误命中）
_RE_AMOUNT_LOOSE = re.compile(r"[￥¥]\s*([0-9][0-9,，]*(?:\.[0-9]+)?)\s*(万元|元)")
_RE_DATE_CN = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_RE_PERIOD = re.compile(
    r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*至\s*"
    r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)
_MATERIAL_KEYWORDS = {
    "invoice": ("发票",),
    "acceptance": ("验收",),
    "owner_proof": ("业主证明", "用户证明", "使用证明", "中标通知书"),
}


def _parse_amount(text: str) -> Decimal | None:
    match = _RE_CONTRACT_AMOUNT.search(text or "")
    if match is None:
        # 大写金额（"柒佰陆拾万元整"）优先于 ¥ 兜底；只在"大写"邻域找，
        # 防把投标保证金等其他大写金额当合同价。
        from app.engine.facts import parse_upper_amount

        idx = (text or "").find("大写")
        if idx >= 0:
            upper = parse_upper_amount(text[idx:idx + 80])
            if upper is not None:
                return upper
        match = _RE_AMOUNT_LOOSE.search(text or "")
        if match is None:
            return None
    try:
        value = Decimal(match.group(1).replace(",", "").replace("，", ""))
    except Exception:  # noqa: BLE001
        return None
    if match.group(2) == "万元":
        value *= Decimal(10_000)
    return value


def _first_date(text: str) -> str | None:
    match = _RE_DATE_CN.search(text or "")
    return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}" if match else None


def _period(text: str) -> str | None:
    match = _RE_PERIOD.search(text or "")
    if not match:
        return None
    return (
        f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
        f"~{match.group(4)}-{int(match.group(5)):02d}-{int(match.group(6)):02d}"
    )


def _materials_in(pages: list[dict], page_numbers: set[int]) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for page in pages:
        number = int(page.get("page_no") or 0)
        if page_numbers and number not in page_numbers:
            continue
        text = str(page.get("text") or "")
        for kind, keywords in _MATERIAL_KEYWORDS.items():
            if any(word in text for word in keywords):
                found.setdefault(kind, []).append(number)
    return found


def _contracts_from_text(pages: list[dict]) -> list[ContractRecord]:
    """Contract-block fallback for prose contracts (no performance table)."""
    records: list[ContractRecord] = []
    for page in pages:
        text = str(page.get("text") or "")
        number = int(page.get("page_no") or 0)
        year_heading = False
        for match in _RE_CONTRACT_HEADING.finditer(text):
            year, name = match.group(1), match.group(2).strip()
            year_heading = True
            records.append(ContractRecord(name=name, pages=[number], source="text",
                                          signed_date=f"{year}-01-01"))
        for match in _RE_CONTRACT_HEADING_NO_YEAR.finditer(text):
            name = match.group(1).strip()
            if not re.search(r"合同$", name):
                continue
            # The year heading and the full contract title on the same page
            # describe one agreement; do not count both.
            if year_heading:
                continue
            chinese = re.sub(r"[^一-龥]", "", name)
            if len(chinese) < 4:
                continue
            # A no-year heading is accepted only when the same page also names
            # the parties/amount; otherwise it is a document title.
            if not (_RE_CONTRACT_AMOUNT.search(text) and re.search(r"甲方|乙方|委托方|维保方", text)):
                continue
            if any(r.name == name and number in r.pages for r in records):
                continue
            records.append(ContractRecord(name=name, pages=[number], source="text"))
    # Merge amount/date/materials from the whole contract block, bounded by the
    # next contract heading (or 15 pages when it is the last one).  Invoices and
    # acceptance pages often follow several pages after the signature page.
    by_page = {int(p.get("page_no") or 0): str(p.get("text") or "") for p in pages}
    heading_pages = sorted({min(record.pages) for record in records})
    for record in records:
        start = min(record.pages)
        index = heading_pages.index(start)
        end = heading_pages[index + 1] - 1 if index + 1 < len(heading_pages) else start + 15
        block_pages = set(range(start, max(start, end) + 1))
        block_text = "\n".join(by_page.get(n, "") for n in sorted(block_pages))
        record.amount_yuan = _parse_amount(block_text)
        record.signed_date = record.signed_date or _first_date(block_text)
        record.period = _period(block_text)
        record.materials = _materials_in(pages, block_pages)
    # Deduplicate the same contract repeated by heading + table extraction.
    # The period (or signed date) distinguishes yearly renewals with the same
    # name and amount; without it three annual contracts collapse into one.
    deduped: dict[tuple[str, str, str], ContractRecord] = {}
    for record in records:
        key = (re.sub(r"\s+", "", record.name), str(record.amount_yuan),
               record.period or record.signed_date or "")
        if key not in deduped:
            deduped[key] = record
            continue
        kept = deduped[key]
        kept.pages = sorted(set(kept.pages) | set(record.pages))
        for kind, numbers in record.materials.items():
            kept.materials.setdefault(kind, [])
            kept.materials[kind] = sorted(set(kept.materials[kind]) | set(numbers))
        kept.amount_yuan = kept.amount_yuan or record.amount_yuan
        kept.signed_date = kept.signed_date or record.signed_date
        kept.period = kept.period or record.period
    return list(deduped.values())


def _contract_name_overlap(left: str, right: str) -> bool:
    a = re.sub(r"[^一-龥A-Za-z0-9]", "", left or "")
    b = re.sub(r"[^一-龥A-Za-z0-9]", "", right or "")
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


def _apply_heading_material_states(blocks: list, pages: list[PageRec]) -> None:
    """Bind explicit case subheadings even when page image statistics are unavailable."""
    by_no = {page.page_no: page for page in pages}
    patterns = {
        "contract": re.compile(r"合同(?:原件)?复印件|合同扫描件"),
        "invoice": re.compile(r"发票(?:原件)?复印件|发票扫描件|"
                              r"(?:^|\n)\s*(?:\d+(?:\.\d+)+\s*)?(?:合同执行中)?发票(?:复印件|扫描件)?\s*$", re.M),
        "acceptance": re.compile(
            r"验收报告|验收证明|验收单|终验证书|业主证明|用户证明"
        ),
    }
    for block in blocks:
        if getattr(block, "ambiguous", False) or not getattr(block, "pages", None):
            continue
        for material, pattern in patterns.items():
            hits = []
            for number in block.pages:
                page = by_no.get(number)
                text = str(page.text if page else "")
                match = pattern.search(text)
                # Real evidence sections commonly put the material heading and
                # the scanned copy on the same OCR page.  Only reject long
                # requirement/index text before the heading; content after it
                # is the proof itself and must not make the binding disappear.
                if match and len(text[:match.start()].strip()) <= 300:
                    hits.append(number)
            if hits and not block.status.get(material):
                block.status[material] = True
                block.pages_by_material[material] = hits[:3]
                block.reasons[material] = "material_heading"


def _contracts_from_case_table(pages: list[dict], pdf_path: str | None) -> list[ContractRecord]:
    if not pdf_path:
        return []
    try:
        from app.evidence.case_evidence import (
            PageRec, link_case_blocks, locate_section, parse_case_table_at_section,
        )
        from app.services.scoring_service import _cases_from_blocks
    except Exception:  # noqa: BLE001
        return []
    recs = [PageRec(int(p.get("page_no") or 0), str(p.get("text") or ""),
                    str(p.get("source") or "text")) for p in pages]
    section = locate_section(recs)
    if not section.found:
        return []
    try:
        rows = parse_case_table_at_section(pdf_path, section, recs, 4)
        if not rows:
            # 索引指针落在错段（run128 143：索引 128-169、真实业绩表 181）→
            # 以表头页（通用三元组判定，不猜项目）重试一次；仍空才放弃。
            from app.evidence.case_evidence import (
                SectionGuess, _find_case_table_header_page,
            )
            head = _find_case_table_header_page(recs)
            if head is not None and not (section.start <= head <= section.end):
                section = SectionGuess(
                    head, min(head + 20, max(r.page_no for r in recs)),
                    "heading_window", 0.6, None,
                )
                rows = parse_case_table_at_section(pdf_path, section, recs, 4)
        if not rows:
            return []
        blocks = link_case_blocks(rows, recs, section)
        _apply_heading_material_states(blocks, recs)
        # PR1（2026-11-24 run155 实证）：必须补跑三态分类——旧实现"跳过
        # classify_materials"使 pages_by_material/status 恒空，材料链 AND 闸把
        # "材料在但没绑上"误判"缺件"→ c43 整列 0。classify 失败只降级不阻塞
        # （退回旧语义，绝不因分类异常抬走整条案例链）。
        try:
            from app.evidence.case_evidence import classify_materials, page_image_ratios

            tag_pages = None
            if config.material_index_enabled() and config.case_tag_prior_enabled():
                try:
                    from app.material_index.hints import page_material_tags

                    doc_id = pages[0].get("document_id") if pages else None
                    if doc_id is not None:
                        tag_pages = page_material_tags(
                            int(doc_id),
                            {no for b in blocks for no in b.pages},
                            {int(p.get("page_no") or 0): str(p.get("text") or "")
                             for p in pages},
                        )
                except Exception:  # noqa: BLE001 —— 标签先验失败不阻塞三态
                    tag_pages = None
            ratios = {}
            try:
                ratios = page_image_ratios(pdf_path)
            except Exception:  # noqa: BLE001 —— 图像统计失败不阻断标题材料分类
                pass
            classify_materials(blocks, recs, ratios, tag_pages=tag_pages)
        except Exception:  # noqa: BLE001
            pass
        cases = _cases_from_blocks(blocks)
    except Exception:  # noqa: BLE001
        return []
    out: list[ContractRecord] = []
    for case in cases:
        name = str(case.get("project_name") or case.get("name") or "")
        if not name:
            continue
        amount = case.get("amount_yuan") or case.get("amount")
        try:
            amount_d = Decimal(str(amount)) if amount is not None else None
        except Exception:  # noqa: BLE001
            amount_d = None
        pages_found: list[int] = []
        # 生产者（_cases_from_blocks）写的是 material_pages 键；旧契约
        # contract_pages/invoice_pages/… 全库无写入方（run155 实证），保留
        # 仅为历史数据 fallback。
        mat = case.get("material_pages") or {}
        for nums in list(mat.values()) + [case.get("block_pages")] + [
                case.get(k) for k in
                ("contract_pages", "invoice_pages", "acceptance_pages", "evidence_pages")]:
            for number in nums or []:
                try:
                    pages_found.append(int(number))
                except Exception:  # noqa: BLE001
                    continue
        materials: dict[str, list[int]] = {}
        for kind, nums in mat.items():
            if nums:
                try:
                    materials[str(kind)] = sorted({int(n) for n in nums})
                except Exception:  # noqa: BLE001
                    continue
        for kind, key in (("contract", "contract_pages"),
                          ("invoice", "invoice_pages"),
                          ("acceptance", "acceptance_pages")):
            if kind not in materials and case.get(key):
                materials[kind] = sorted({int(n) for n in case[key]})
        materials_state = {k: case.get(k) for k in ("contract", "invoice", "acceptance")}
        out.append(ContractRecord(
            name=name, amount_yuan=amount_d,
            signed_date=str(case.get("sign_date") or "") or None,
            pages=sorted(set(pages_found)), materials=materials,
            materials_state=materials_state, source="table",
        ))
    return out


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------
_RE_LEVEL_TOKEN = r"[壹贰叁肆一二三四]级|一级|二级|三级"
_RE_QUALIFICATION_CERT = re.compile(
    r"(电子与智能(?:化)?(?:工|巟)程专业承包)[^。\n]{0,12}?(" + _RE_LEVEL_TOKEN + r")"
)
_RE_AV_INTEGRATION_CERT = re.compile(
    r"((?:音视频|音响)(?:系统)?集成(?:工|巟)程(?:综合技术)?能力|音视频集成工程|专业音响(?:工|巟)程设计施工(?:一体化)?能力)"
    r"[^。\n]{0,30}?(" + _RE_LEVEL_TOKEN + r")"
)
_RE_ISO_TOKEN = re.compile(
    r"ISO\s*/?\s*IEC?\s*(\d{4,5})|ISO\s*(\d{4,5})|GB/?T\s*(\d{4,5})"
)
_RE_SYSTEM_CERT = re.compile(
    r"(质量管理体系|环境管理体系|职业健康安全管理体系|信息技术服务管理体系|信息技术服务认证|信息安全管理体系)"
)
_RE_VALID_TO = re.compile(
    r"有效期(?:至|到)[：:]?\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)

_CERT_DATE = r"(20\d{2})\s*[-年./]\s*(\d{1,2})\s*[-月./]\s*(\d{1,2})\s*日?"
_RE_MATURITY_FAMILY = re.compile(r"(?<![A-Za-z])(?:CMMI|CSMM)(?=\b|Maturity|Apprais|Institute)|能力成熟度", re.I)
_RE_MATURITY_LEVEL = re.compile(
    r"(?:maturity\s*level|level|(?<![a-z])ML|成熟度等级?|评估等级|级别)\s*[:：]?\s*([1-5一二三四五ⅠⅡⅢⅣⅤ])(?!\d)"
    r"|([1-5一二三四五ⅠⅡⅢⅣⅤ])\s*级", re.I)
_RE_MATURITY_PERIOD = re.compile(_CERT_DATE + r"\s*(?:至|到|~|～|-|—|–|to)\s*" + _CERT_DATE, re.I)
_RE_MATURITY_EXPIRY = re.compile(
    r"(?:有效期\s*至|有效\s*至|到期日期|valid\s*(?:until|to|through)|expir\w*\s*(?:date)?)\s*[:：]?\s*"
    + _CERT_DATE, re.I)
_RE_MATURITY_FROM = re.compile(
    r"(?:有效期\s*自|签发日期|发证日期|Date\s+Issued|issued(?:\s+on)?|appraisal\s+date)\s*[:：]?\s*"
    + _CERT_DATE, re.I)
_ENGLISH_CERT_DATE = r"[A-Za-z]{3,9}\s*\d{1,2}\s*,?\s*20\d{2}(?!\d)"
_RE_ENGLISH_MATURITY_PERIOD = re.compile(
    r"valid\s*from\s*(" + _ENGLISH_CERT_DATE + r")\s*to\s*(" + _ENGLISH_CERT_DATE + r")", re.I,
)
_RE_ENGLISH_MATURITY_EXPIRY = re.compile(
    r"(?:expir(?:ation|y|es)\s*(?:date\s*)?|\bexp\.|valid\s*(?:until|through))\s*[:：]?\s*("
    + _ENGLISH_CERT_DATE + r")", re.I,
)
_APPRAISAL_LABEL = r"Appra(?:i|l|li)sal"  # OCR can read the letter i as l or li.
_RE_APPRAISAL_ID = re.compile(
    rf"(?:{_APPRAISAL_LABEL}\s*(?:(?:ID|number|no\.?)\s*[:：]?\s*#?|#)\s*|appraisals/)([0-9]{{3,}})(?![\w-])", re.I,
)
_RE_PARS_LOOKUP = re.compile(r"\bPARS\b|Published\s+Appraisal\s+Results|cmmiinstitute\.com/pars", re.I)
_RE_PARS_ID = re.compile(r"(?<!\w)ID\s*[:：]\s*#?\s*([0-9]{3,})(?![\w-])", re.I)
_RE_PARS_VALIDITY = re.compile(rf"{_APPRAISAL_LABEL}\s+Validity\s*[:：]?\s*", re.I)
_RE_MATURITY_PROOF = re.compile(
    r"has\s*been\s*(?:successfully\s*)?appraised\s*at|certificate\s*of\s*appraisal|"
    rf"benchmark\s*{_APPRAISAL_LABEL}\s*#\s*[0-9]{{3,}}(?![\w-])|证书编号|评估编号|认证编号|签发日期|发证日期|评估机构|date\s+issued",
    re.I,
)


def bind_maturity_appraisals(records: list[dict], pages: list[dict] | None = None) -> list[dict]:
    """Apply a published validity period to the same appraisal on a certificate."""
    def appraisal_key(record: dict) -> tuple | None:
        appraisal_id = str(record.get("appraisal_id") or "")
        document_id = record.get("document_id")
        if document_id and re.fullmatch(r"[0-9]{3,}", appraisal_id):
            return document_id, appraisal_id
        return None

    by_appraisal = {}
    for record in records:
        if record.get("appraisal_is_lookup") is not True or not record.get("valid_until"):
            continue
        key = appraisal_key(record)
        if key is not None:
            by_appraisal.setdefault(key, []).append(record)

    certificates = []
    for original in records:
        if original.get("appraisal_is_lookup") is True:
            continue
        cert = dict(original)
        lookups = by_appraisal.get(appraisal_key(cert), [])
        periods = {(lookup.get("valid_from"), lookup["valid_until"]) for lookup in lookups}
        if cert.get("backing") is True and len(periods) == 1:
            start, end = periods.pop()
            if (cert.get("valid_from") in (None, start) and cert.get("valid_until") in (None, end)
                    and (not cert.get("valid_from") or not cert.get("valid_until"))):
                cert["valid_from"] = cert.get("valid_from") or start
                cert["valid_until"] = cert.get("valid_until") or end
                cert["appraisal_lookup_pages"] = sorted({lookup["page_no"] for lookup in lookups
                                                         if lookup.get("page_no") is not None})
        if not cert.get("valid_until") and cert.get("page_no") is not None:
            source = " ".join(
                str(page.get("text") or "") for page in (pages or [])
                if page.get("document_id") == cert.get("document_id")
                and page.get("page_no") == cert.get("page_no")
            )
            maturity = _RE_MATURITY_FAMILY.search(source)
            maturity_source = source[maturity.start():] if maturity else source
            next_iso = _RE_ISO_TOKEN.search(maturity_source[1:])
            if next_iso:
                maturity_source = maturity_source[:next_iso.start() + 1]
            if (len(maturity_source.strip()) >= 80
                    and re.search(r"CMMI|CSMM|Maturity|Appraisal", maturity_source, re.I)
                    and not re.search(
                        r"有效期|有效至|到期日期|Expiration|Valid\s*(?:from|until|through|to)|"
                        r"Appraisal\s+Validity", maturity_source, re.I)):
                cert["confirmed_missing_validity"] = True
        certificates.append(cert)
    def title_only(cert: dict) -> bool:
        if cert.get("backing") is not True or cert.get("valid_until") or cert.get("appraisal_lookup_pages"):
            return False
        source = " ".join(
            str(page.get("text") or "") for page in (pages or [])
            if page.get("document_id") == cert.get("document_id")
            and page.get("page_no") == cert.get("page_no")
        )
        return not (_RE_MATURITY_PROOF.search(source)
                    or re.search(r"CMMI|CSMM", source, re.I)
                    and re.search(r"Maturity|Appraisal|Level|证书编号|评估编号|发证", source, re.I))

    return [cert for cert in certificates if not title_only(cert)]


def maturity_certificate_details(pages: list[dict], visual_records: list[dict] | None = None) -> list[dict]:
    """Extract CMMI/CSMM details from a certificate page, excluding mentions and rule echoes."""
    from datetime import date, datetime

    from app.engine.objective import _maturity_level as _parse_maturity_level

    def date_value(groups):
        try:
            return date(*map(int, groups)).isoformat()
        except ValueError:
            return None

    def english_date(value):
        value = re.sub(r"\s*([A-Za-z]+)\s*(\d{1,2})\s*,?\s*(20\d{2})", r"\1 \2 \3", value)
        for form in ("%b %d %Y", "%B %d %Y"):
            try:
                return datetime.strptime(value, form).date().isoformat()
            except ValueError:
                continue
        return None

    records = list(visual_records or [])
    for page in pages:
        page_text = str(page.get("text") or "")
        family = _RE_MATURITY_FAMILY.search(page_text)
        if not family:
            continue
        # A physical page can contain another certification, so use the maturity
        # section and preserve a certificate phrase directly before its family name.
        start = family.start()
        prefix = page_text[:start]
        previous_iso = list(_RE_ISO_TOKEN.finditer(prefix))
        prefix_start = previous_iso[-1].end() if previous_iso else 0
        # Do not let proof labels from an earlier platform certificate-detail
        # section pull unrelated ISO validity dates into the CMMI section.
        proof_prefix = (
            [] if _is_platform_detail_page(page_text[:start])
            else list(_RE_MATURITY_PROOF.finditer(prefix, prefix_start))
        )
        if proof_prefix:
            start = proof_prefix[-1].start()
        text = page_text[start:]
        next_family = _RE_ISO_TOKEN.search(text)
        if next_family:
            text = text[:next_family.start()]
        lookup_anchor = _RE_PARS_VALIDITY.search(text)
        lookup_brand = _RE_PARS_LOOKUP.search(page_text)
        is_lookup = lookup_anchor is not None and lookup_brand is not None
        ids = {match[1] for match in _RE_APPRAISAL_ID.finditer(text)}
        if is_lookup:
            ids.update(match[1] for match in _RE_PARS_ID.finditer(text))
        appraisal_id = next(iter(ids)) if len(ids) == 1 else None
        fingerprint = _RE_MATURITY_PROOF.search(text) or re.search(
            r"certificat\w+|CERTIFICATE", page_text, re.I,
        )
        if is_lookup:
            lookup_period = _RE_MATURITY_PERIOD.match(text, lookup_anchor.end())
            if appraisal_id and lookup_period:
                records.append({
                    "name": family[0].upper(), "backing": False, "appraisal_is_lookup": True,
                    "appraisal_id": appraisal_id, "cert_level": _parse_maturity_level(text),
                    "valid_from": date_value(lookup_period.groups()[:3]),
                    "valid_until": date_value(lookup_period.groups()[3:]),
                    "document_id": page.get("document_id"), "page_no": page.get("page_no"),
                })
            continue
        if not fingerprint:
            continue
        if re.search(r"提供(?:有效期内|[^。；\n]{0,12}证书复印件)|每(?:提供|项|个)[^。；\n]{0,15}得\s*\d+\s*分", text):
            continue
        # Printed section titles can give a different level from the certificate.
        certificate = re.sub(r"(?im)^\s*(?:\d+\s*[.\-]\s*){2,}\d*\.?\s*(?:CMMI|CSMM)[^\n]{0,45}\n",
                             "", text)
        levels = list(_RE_MATURITY_LEVEL.finditer(certificate))
        level = next((candidate for candidate in levels if candidate[1]), levels[0] if levels else None)
        period = _RE_MATURITY_PERIOD.search(text)
        expiry = _RE_MATURITY_EXPIRY.search(text)
        start = _RE_MATURITY_FROM.search(text)
        english_period = _RE_ENGLISH_MATURITY_PERIOD.search(text)
        english_expiry = _RE_ENGLISH_MATURITY_EXPIRY.search(text)
        valid_from = date_value(period.groups()[:3]) if period else date_value(start.groups()) if start else None
        valid_until = date_value(period.groups()[3:]) if period else date_value(expiry.groups()) if expiry else None
        if english_period:
            valid_from = valid_from or english_date(english_period[1])
            valid_until = valid_until or english_date(english_period[2])
        elif english_expiry:
            valid_until = valid_until or english_date(english_expiry[1])
        if not valid_until and re.search(r"Benchmark\s+Appraisal", text, re.I):
            completed = re.search(
                r"completed\s+on\s+([A-Za-z]+\s+\d{1,2},?\s+20\d{2})",
                text, re.I,
            )
            completed_on = english_date(completed.group(1)) if completed else None
            if completed_on:
                # A CMMI Benchmark Appraisal remains valid for three years from
                # its completion date when the certificate copy omits a separate
                # expiry line but still prints the appraisal completion date.
                from datetime import timedelta

                completed_date = date.fromisoformat(completed_on)
                valid_from = valid_from or completed_on
                valid_until = (completed_date.replace(year=completed_date.year + 3)
                               - timedelta(days=1)).isoformat()
        records.append({
            "name": family[0].upper(), "backing": True,
            "cert_level": next((value for value in level.groups() if value), None) if level else None,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "document_id": page.get("document_id"), "page_no": page.get("page_no"),
            "appraisal_id": appraisal_id,
        })
    bound = bind_maturity_appraisals(records, pages)
    used_lookup_pages = {
        page
        for cert in bound
        for page in cert.get("appraisal_lookup_pages") or []
    }
    lookup_periods: dict[str, set[tuple[str | None, str | None]]] = {}
    for lookup in records:
        if lookup.get("appraisal_is_lookup") is True and lookup.get("valid_until"):
            lookup_periods.setdefault(str(lookup.get("appraisal_id") or ""), set()).add(
                (lookup.get("valid_from"), lookup.get("valid_until"))
            )
    for lookup in records:
        if (lookup.get("appraisal_is_lookup") is not True
                or lookup.get("page_no") in used_lookup_pages
                or not lookup.get("valid_until") or not lookup.get("cert_level")
                or len(lookup_periods.get(str(lookup.get("appraisal_id") or ""), set())) != 1):
            continue
        bound.append({
            "name": lookup.get("name"), "backing": True,
            "cert_level": lookup.get("cert_level"),
            "valid_from": lookup.get("valid_from"),
            "valid_until": lookup.get("valid_until"),
            "document_id": lookup.get("document_id"),
            "page_no": lookup.get("page_no"),
            "appraisal_id": lookup.get("appraisal_id"),
            "official_lookup": True,
        })
    return bound


def requires_platform_certificate_query(criterion: dict) -> bool:
    rule = "\n".join(str(criterion.get(key) or "") for key in
                     ("required_evidence", "description", "source_quote", "name"))
    if (re.search(r"资质实力[（(]1[）)]|体系认证", rule)
            and re.search(r"ISO\s*(?:/IEC\s*)?\d|质量管理体系", rule, re.I)):
        return True
    return bool(re.search(r"cx\.cnca\.cn|认证认可信息公共服务平台", rule, re.I)
                and "截图" in rule and re.search(r"ISO\s*(?:/IEC\s*)?\d", rule, re.I))


def platform_query_families(text: str, *, require_seal: bool) -> set[str]:
    families, _ = platform_query_details(text, require_seal=require_seal)
    return families


_CERTIFICATE_NUMBER = re.compile(
    r"(?:证书编号|证书号码|证书号|注册号|certificat\w+\s+(?:no\.?|number))"
    r"\s*[:：#]?\s*([0-9A-Z]+(?:[./-][0-9A-Z]+)*)", re.I,
)


def certificate_numbers(text: str) -> set[str]:
    return {match[1].upper() for match in _CERTIFICATE_NUMBER.finditer(text)
            if 5 <= len(match[1]) <= 50 and any(char.isdigit() for char in match[1])}


def certificate_families(text: str) -> set[str]:
    labels = [m[0] for m in _RE_ISO_TOKEN.finditer(text.upper())]
    labels.extend(m[0] for m in _RE_SYSTEM_CERT.finditer(text))
    return {normalize_cert(label) for label in labels}


_CERT_PLATFORM_BRAND = re.compile(
    r"cx\.cnca\.cn|认证认可信息公共服务平台|国家市场监督管理总局|国家市场监管总局|"
    r"State\s+Administration\s+for\s+Market\s+Regulation",
    re.I,
)
_CERT_DETAIL_PAGE = re.compile(
    r"当前位置\s*[：:]?\s*认证结果|证书详情|认证信息|证书信息|平台截图",
    re.I,
)


def _is_platform_detail_page(text: str) -> bool:
    """认证平台详情页的确定性判定（不依赖 OCR 是否识别印章）。"""
    normalized = re.sub(r"\s+", "", text or "")
    if not _CERT_PLATFORM_BRAND.search(normalized):
        return False
    if not _CERT_DETAIL_PAGE.search(normalized):
        return False
    fields = sum(field in normalized for field in (
        "证书编号", "认证项目", "认证依据", "有效期", "证书状态", "机构批准号",
    ))
    return fields >= 2


def platform_query_details(text: str, *, require_seal: bool) -> tuple[set[str], set[str]]:
    normalized = re.sub(r"IS0(?=[/A-Z0-9])", "ISO", text or "", flags=re.I)
    if not _CERT_PLATFORM_BRAND.search(normalized):
        return set(), set()
    detail_page = _is_platform_detail_page(normalized)
    if not detail_page and not re.search(
            r"查询结果|查询页|查询截图|认证信息|证书信息|证书详情|平台截图", normalized):
        return set(), set()
    # 详情页本身是强证据；OCR 常漏掉页面印章字样，不能因此把已绑定的
    # 证书详情降成 pending。普通截图标题/要求句仍按原闸门过滤。
    if not detail_page and re.search(r"未见|无公章|未加盖|未显示|须提供|请提供|不得分", normalized):
        return set(), set()
    if require_seal and not detail_page and not re.search(
            r"红(?:色)?(?:圆形)?(?:公章|印章|公司章)|盖有[^，。；]{0,20}(?:公章|印章)|"
            r"可见[^，。；]{0,20}(?:公章|印章)|加盖[^，。；]{0,20}公章|"
            r"(?:公章|印章)(?:印迹|清晰可见)|红章", normalized):
        return set(), set()
    return certificate_families(normalized), certificate_numbers(normalized)


def _level_from(token: str) -> str | None:
    raw = re.sub(r"\s+", "", token or "")
    mapping = {"壹级": "一级", "贰级": "二级", "叁级": "三级", "肆级": "四级"}
    return mapping.get(raw, raw or None)


def _certs_from_pages(pages: list[dict]) -> list[CertRecord]:
    records: list[CertRecord] = []
    seen: set[tuple[str, str | None]] = set()

    def add(name: str, page_no: int, level: str | None = None,
            valid_until: str | None = None) -> None:
        normalized = normalize_cert(name)
        key = (normalized or name, level)
        if key in seen:
            return
        seen.add(key)
        records.append(CertRecord(name=name, normalized=normalized, level=level,
                                  valid_until=valid_until, pages=[page_no]))

    for page in pages:
        number = int(page.get("page_no") or 0)
        text = str(page.get("text") or "")
        if not text:
            continue
        # A narrative mention ("公司 2002 年导入 ISO9000 系列") is not a
        # certificate copy.  Require certificate-page signals before counting.
        cert_context = bool(re.search(
            r"认证证书|证书编号|证书号码|符合.{0,30}标准|有效期|发证日期|认证范围|"
            r"资质类别及等级|资质证书|认证机构|评估证书|成熟度|评估机构|CMMI|CSMM",
            text,
        ))
        if not cert_context:
            continue
        valid = None
        valid_match = _RE_VALID_TO.search(text)
        if valid_match:
            valid = f"{valid_match.group(1)}-{int(valid_match.group(2)):02d}-{int(valid_match.group(3)):02d}"
        for match in _RE_QUALIFICATION_CERT.finditer(text):
            add(match.group(1).strip(), number, _level_from(match.group(2)), valid)
        for match in _RE_AV_INTEGRATION_CERT.finditer(text):
            add(match.group(1).strip(), number, _level_from(match.group(2)), valid)
        for match in _RE_ISO_TOKEN.finditer(text):
            raw = match.group(0)
            if raw and "9000" not in re.sub(r"\D", "", raw):
                add(raw, number, None, valid)
        for match in _RE_SYSTEM_CERT.finditer(text):
            add(match.group(1), number, None, valid)
        if re.search(r"CMMI|CSMM|能力成熟度", text, re.I):
            add("CMMI", number, None, valid)
    return records


# ---------------------------------------------------------------------------
# Personnel
# ---------------------------------------------------------------------------
_RE_PERSON_ROW = re.compile(
    r"^\s*(\d{1,2})\s+([一-龥]{2,4})\s+(男|女)\s+(\d{2})\b"
)
_RE_PERSON_NAME = re.compile(r"^\s*(\d{1,2})\s+([一-龥]{2,4})\s+(男|女)\b")
_RE_YEARS = re.compile(r"(\d{1,2})\s*年(?:以上|余|多)?")
_RE_PAGE_REF = re.compile(r"P\s*(\d{1,3})", re.I)
_SENIOR_TITLE = re.compile(r"高级(?:电气)?工程师")
_ASSISTANT_TITLE = re.compile(r"助理工程师")
_MID_TITLE = re.compile(r"(?<!高级)(?<!助理)(?<!维修)(?<!设备)工程师")
_CERT_TOKENS = (
    "电工证", "特种作业操作证", "高处作业", "登高证",
    "音响系统设备维修工程师", "投影机维修工程师",
    "中控系统维修工程师", "调音台维修工程师", "音箱维修工程师",
    "功放维修工程师", "数字音频处理器维修工程师", "灯光系统维修工程师",
    "高级音响调音员", "音响调音员", "注册二级建造师", "二级建造师",
)
_ROLE_KEYWORDS = (
    "驻场", "维保负责人", "总负责人", "项目负责人", "项目顾问", "培训师",
    "维保人员", "强电作业",
)


def _person_title(window: str) -> str:
    if _SENIOR_TITLE.search(window):
        return "高级工程师"
    if _ASSISTANT_TITLE.search(window):
        return "助理工程师"
    if _MID_TITLE.search(window):
        return "工程师"
    return ""


def _person_certs(window: str) -> list[str]:
    out: list[str] = []
    for token in _CERT_TOKENS:
        if token in window and token not in out:
            out.append(token)
    return out


def _person_role(window: str, name: str) -> str:
    for line in window.splitlines():
        if name not in line:
            continue
        for keyword in _ROLE_KEYWORDS:
            if keyword in line:
                return keyword
    for keyword in _ROLE_KEYWORDS:
        if keyword in window:
            return keyword
    return ""


_BAD_PERSON_NAMES = {
    "姓名", "性别", "年龄", "职称", "学历", "专业", "序号", "项目", "负责",
    "团队", "人员", "投标", "中标", "公司", "单位", "法定", "代表", "经理",
    "本科", "大专", "专科", "研究", "工程", "技术", "服务", "维护",
}
_RE_LEADER_NAME = re.compile(r"姓名[：:\s]*([一-龥]{2,4})")
_RE_NAME_GENDER = re.compile(r"([一-龥]{2,4})\s*(男|女)")


def _clean_cell(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _table_header_map(row: list) -> dict[str, int] | None:
    cells = [_clean_cell(cell) for cell in row]
    name_idx = next((i for i, cell in enumerate(cells) if "姓名" in cell), None)
    if name_idx is None:
        return None
    header: dict[str, int] = {"name": name_idx}
    for key, words in (
        ("title", ("职称",)),
        ("certs", ("上岗证书", "证书")),
        ("role", ("职责", "岗位", "本项目中")),
        ("experience", ("项目经历", "经历", "经验")),
        ("gender", ("性别",)),
        ("age", ("年龄",)),
    ):
        for index, cell in enumerate(cells):
            if any(word in cell for word in words):
                header[key] = index
                break
    if "title" not in header and "certs" not in header:
        return None
    return header


def _table_title(raw: str) -> str:
    text = _clean_cell(raw)
    if not text or text in ("无", "/", "-"):
        return ""
    if "高级音响" in text or "音响调音" in text:
        return ""
    if "高级" in text and "工程师" in text:
        return "高级工程师"
    if "助理" in text and "工程师" in text:
        return "助理工程师"
    if "高级" in text:
        return "高级工程师"
    if "中级" in text or ("工程师" in text and "维修" not in text):
        return "工程师"
    return ""


def _table_certs(raw: str) -> list[str]:
    text = _clean_cell(raw)
    if not text or text in ("无", "/", "-"):
        return []
    out: list[str] = []
    for token in _CERT_TOKENS:
        if token in text and token not in out:
            out.append(token)
    # Preserve generic certificates that are not in the fixed token list.
    for piece in re.split(r"[、，,；;]", text):
        piece = re.sub(r"[（(]\s*[Pp]?\d[\d\-~～]*\s*[)）]", "", piece).strip()
        if len(piece) >= 3 and piece not in out and "工程师" in piece:
            out.append(piece)
    return out


def _persons_from_tables(pages: list[dict], pdf_path: str | None) -> list[PersonRecord]:
    if not pdf_path:
        return []
    try:
        import pymupdf as fitz
    except Exception:  # noqa: BLE001
        return []
    records: dict[str, PersonRecord] = {}
    try:
        document = fitz.open(pdf_path)
    except Exception:  # noqa: BLE001
        return []
    try:
        scanned = 0
        for page in pages:
            text = str(page.get("text") or "")
            # Only scan explicit team-table pages.  A generic "姓名+职称"
            # prefilter matched dozens of credit reports/resumes and made
            # find_tables dominate the run's wall clock.
            if "项目团队人员情况表" not in text and "人员情况表" not in text:
                continue
            scanned += 1
            if scanned > 12:
                break
            number = int(page.get("page_no") or 0)
            try:
                tables = document[number - 1].find_tables()
            except Exception:  # noqa: BLE001
                continue
            for table in tables.tables:
                rows = table.extract()
                header: dict[str, int] | None = None
                for row in rows:
                    if header is None:
                        header = _table_header_map(row)
                        continue
                    if header is None:
                        continue
                    name = _clean_cell(row[header["name"]] if header["name"] < len(row) else "")
                    if not name or name in _BAD_PERSON_NAMES or not re.fullmatch(r"[一-龥]{2,6}", name):
                        continue
                    title = _table_title(row[header["title"]] if header.get("title") is not None
                                         and header["title"] < len(row) else "")
                    certs = _table_certs(row[header["certs"]] if header.get("certs") is not None
                                         and header["certs"] < len(row) else "")
                    role = _clean_cell(row[header["role"]] if header.get("role") is not None
                                       and header["role"] < len(row) else "")
                    experience = str(row[header["experience"]] if header.get("experience") is not None
                                     and header["experience"] < len(row) else "" or "")
                    years = None
                    for match in _RE_YEARS.finditer(_clean_cell(experience)):
                        value = int(match.group(1))
                        if 0 < value <= 40:
                            years = Decimal(value)
                            break
                    person = records.get(name)
                    if person is None:
                        person = PersonRecord(name=name, pages=[number])
                        records[name] = person
                    if title and not person.title:
                        person.title = title
                    for cert in certs:
                        if cert not in person.certs:
                            person.certs.append(cert)
                    if role and not person.role:
                        person.role = role
                    if experience and not person.experience:
                        person.experience = experience.strip()
                    if years is not None and person.years is None:
                        person.years = years
                    refs = sorted({int(m.group(1)) for m in _RE_PAGE_REF.finditer(str(row))})
                    for ref in refs:
                        if ref not in person.experience_pages:
                            person.experience_pages.append(ref)
    finally:
        document.close()
    return list(records.values())


def _is_personnel_table_page(text: str) -> bool:
    if "项目团队人员情况表" in text or "人员情况表" in text:
        return True
    return "姓名" in text and (
        "上岗证书" in text or "本项目中的职责" in text
        or ("职称" in text and ("团队" in text or "人员" in text))
    )


def _person_window(text: str, match, matches: list, index: int) -> str:
    start = max(0, match.start() - 120)
    end = matches[index + 1].start() if index + 1 < len(matches) else match.end() + 320
    return text[start:min(len(text), end)]


def _persons_from_leader_forms(pages: list[dict]) -> list[PersonRecord]:
    """Read the vertical '项目负责人情况表' form (name + title)."""
    out: list[PersonRecord] = []
    for page in pages:
        text = str(page.get("text") or "")
        if "项目负责人情况表" not in text and "负责人情况表" not in text:
            continue
        number = int(page.get("page_no") or 0)
        name_match = _RE_LEADER_NAME.search(text)
        if not name_match:
            continue
        name = name_match.group(1)
        if name in _BAD_PERSON_NAMES:
            continue
        title = ""
        title_match = re.search(r"职称\s*[：:]?\s*([^\n]{1,16})", text)
        if title_match:
            title = _table_title(title_match.group(1))
        if not title:
            compact = re.sub(r"\s+", "", text)
            for token in ("高级工程师", "高级工程", "中级", "助理工程师"):
                if token in compact:
                    title = _table_title(token)
                    break
        out.append(PersonRecord(name=name, role="项目负责人", title=title, pages=[number]))
    return out


def _persons_from_pages(pages: list[dict]) -> list[PersonRecord]:
    records: dict[str, PersonRecord] = {}
    for page in pages:
        text = str(page.get("text") or "")
        number = int(page.get("page_no") or 0)
        if not _is_personnel_table_page(text):
            continue
        matches = list(_RE_NAME_GENDER.finditer(text))
        candidates: list[tuple[str, str]] = []
        for index, match in enumerate(matches):
            candidates.append((match.group(1), _person_window(text, match, matches, index)))
        # Leader form: "姓名 翁士教 ... 职称 中级" has no adjacent 男/女 token.
        for match in _RE_LEADER_NAME.finditer(text):
            name = match.group(1)
            if name not in _BAD_PERSON_NAMES:
                candidates.append((name, text[max(0, match.start() - 80):match.end() + 320]))
        for name, window in candidates:
            if name in _BAD_PERSON_NAMES or len(name) < 2:
                continue
            compact = re.sub(r"\s+", "", window)
            person = records.get(name)
            if person is None:
                person = PersonRecord(name=name, pages=[number])
                records[name] = person
            elif number not in person.pages:
                person.pages.append(number)
            if not person.role:
                person.role = _person_role(compact, name)
            if not person.title:
                person.title = _person_title(compact)
            certs = _person_certs(compact)
            for cert in certs:
                if cert not in person.certs:
                    person.certs.append(cert)
            if person.years is None:
                for candidate in _RE_YEARS.finditer(compact):
                    value = int(candidate.group(1))
                    if 0 < value <= 40:
                        person.years = Decimal(value)
                        break
            refs = sorted({int(m.group(1)) for m in _RE_PAGE_REF.finditer(window)})
            for ref in refs:
                if ref not in person.experience_pages:
                    person.experience_pages.append(ref)
    # Keep a stable order (first appearance on the page) and drop empty rows.
    return [person for person in records.values() if person.name]


def _overlay_personnel_years(persons: list[PersonRecord], pages: list[dict]) -> None:
    """Map work experience and graduation proof to named persons."""
    if not persons:
        return
    from datetime import date

    reference_year = date.today().year
    normalized = lambda value: re.sub(r"\s+", "", str(value or ""))
    for person in persons:
        if person.years is not None or len(normalized(person.name)) < 2:
            continue
        for page in pages:
            text = str(page.get("text") or "")
            if normalized(person.name) not in normalized(text):
                continue
            match = re.search(
                r"毕业时间\s*[:：]?\s*(19\d{2}|20\d{2})",
                text,
            )
            if not match:
                continue
            graduation_year = int(match.group(1))
            years = reference_year - graduation_year
            if 5 <= years <= 55:
                person.years = Decimal(years)
                if page.get("page_no") is not None and int(page["page_no"]) not in person.experience_pages:
                    person.experience_pages.append(int(page["page_no"]))
            break
    experience_text = "\n".join(
        str(page.get("text") or "") for page in pages
        if re.search(r"人员专业配置|团队人员.*说明|从事.*年", str(page.get("text") or ""))
    )
    if not experience_text:
        return
    for person in persons:
        if person.years is not None:
            continue
        role = person.role or ""
        if role and role in experience_text:
            window = experience_text[experience_text.find(role):][:300]
            match = _RE_YEARS.search(window)
            if match and 0 < int(match.group(1)) <= 40:
                person.years = Decimal(int(match.group(1)))



def _attach_person_case_proofs(
    contracts: list[ContractRecord], persons: list[PersonRecord], pages: list[dict],
) -> None:
    """Bind people to cases only through person-specific proof sections."""
    ordered = sorted(pages, key=lambda page: int(page.get("page_no") or 0))
    normalized = lambda value: re.sub(r"\s+", "", str(value or "")).lower()
    starts: dict[str, int] = {}
    for person in persons:
        name = normalized(person.name)
        if len(name) < 2:
            continue
        best: tuple[int, int] | None = None
        for page in ordered:
            text = str(page.get("text") or "")
            if name not in normalized(text):
                continue
            heading = re.search(
                rf"(?:^|\n)\s*(?:\d+(?:[.\-]\d+)*\s*)?"
                rf"(?:(?:驻场|驻点)?(?:运维)?工程师\s*[-—]\s*)?{re.escape(person.name)}\s*$",
                text, re.M,
            )
            section_heading = re.search(
                rf"(?:^|\n)\s*2[.-]10[^\n]{{0,30}}{re.escape(person.name)}\s*$",
                text, re.M,
            )
            proof = re.search(r"证明材料|经验证明|项目经理业绩|实施经验|项目经验", text)
            score = (40 if section_heading and len(text.strip()) < 1000 else
                     30 if heading and len(text.strip()) < 2500 else 0)
            score += 10 if proof else 0
            if score:
                candidate = (score, -int(page.get("page_no") or 0))
                if best is None or candidate > best:
                    best = candidate
        if best is not None:
            starts[person.name] = -best[1]

    def record_match_strength(record: ContractRecord, text: str) -> int:
        compact = normalized(text)
        title = normalized(record.name)
        if len(title) >= 6 and title in compact:
            return 3
        if record.amount_yuan is not None:
            amount = re.sub(r"\D", "", str(record.amount_yuan))
            if amount and amount in re.sub(r"\D", "", compact):
                return 2
        grams = {title[i:i + 4] for i in range(max(0, len(title) - 3))}
        shared = sum(1 for gram in grams if gram and gram in compact)
        return 1 if len(title) >= 8 and shared >= 2 else 0
    for person in persons:
        start = starts.get(person.name)
        if start is None:
            continue
        later = [value for name, value in starts.items()
                 if name != person.name and value > start]
        section_boundaries = [
            int(page.get("page_no") or 0) for page in ordered
            if int(page.get("page_no") or 0) > start
            and re.search(r"2[.-]10[.-]\d+(?![.\-\d])|2[.-]11(?:[.\-]|\s)", str(page.get("text") or ""))
        ]
        if later:
            end = min(later) - 1
        elif section_boundaries:
            end = min(section_boundaries) - 1
        else:
            end = min(start + 12, max(int(p.get("page_no") or 0) for p in ordered))
        section = [p for p in ordered if start <= int(p.get("page_no") or 0) <= end]
        section_text = "\n".join(str(p.get("text") or "") for p in section)
        proof_pages = [
            p for p in section
            if re.search(r"项目经验|经验证明|项目经理业绩|实施经验|中标通知书|合同复印件|业绩证明",
                         str(p.get("text") or ""))
        ]
        context = [
            p for p in ordered
            if start - 2 <= int(p.get("page_no") or 0) < start
            and person.name in re.sub(r"\s+", "", str(p.get("text") or ""))
            and re.search(r"项目负责人情况表|项目团队人员情况表|业绩及承担", str(p.get("text") or ""))
        ]
        if not proof_pages:
            continue
        person_text = "\n".join(str(p.get("text") or "") for p in context + proof_pages)
        if not re.search(r"项目经验|经验证明|项目经理业绩|实施经验|中标通知书|合同复印件|业绩证明", person_text):
            continue
        experience = bool(re.search(
            r"工作经验证明|工作年限|个人简历|项目经历|从事.{0,12}\d+\s*年|项目经理业绩|实施经验",
            person_text,
        ))
        social = person.social_security is True or (person.social_months or 0) >= 6
        sealed = bool(re.search(r"\u76d6\u7ae0|\u516c\u7ae0|\u7b7e\u7ae0|\u7535\u5b50\u5370\u7ae0", person_text))
        proof_titles = [
            normalized(record.name) for record in contracts
            if record.source == "person_proof" and person.name in (record.person_names or [])
        ]
        matched = [
            (record_match_strength(record, person_text), record)
            for record in contracts
            if record.source != "person_proof"
            and not any(_contract_name_overlap(record.name, title) for title in proof_titles)
        ]
        best_strength = max((strength for strength, _record in matched), default=0)
        for strength, record in matched:
            if strength <= 0 or strength < best_strength:
                continue
            if not re.search(r"项目经验\s*[1２３一二三]|类似项目.{0,16}(?:经验证明|工作经验)|项目经理业绩|实施经验", person_text):
                continue
            proof = {
                "name_reflected": True,
                "work_proof": True,
                "experience_proof": experience,
                "social_security": social,
            }
            record.person_names = list(dict.fromkeys([*record.person_names, person.name]))
            record.person_proofs[person.name] = proof
            if sealed:
                record.materials_state["seal"] = True
            for source, target in (("work_proof", "work_proof"),
                                   ("experience_proof", "experience_proof"),
                                   ("social_security", "social_security")):
                if proof.get(source) is True:
                    record.person_states[target] = True


def _person_case_records_from_pages(
    pages: list[dict], persons: list[PersonRecord],
) -> list[ContractRecord]:
    """Create person cases only from explicit proof-section project titles."""
    ordered = sorted(pages, key=lambda page: int(page.get("page_no") or 0))
    normalized = lambda value: re.sub(r"\s+", "", str(value or "")).lower()
    title_patterns = (
        re.compile(r"(?:项目名称|合同名称)\s*[:：]?\s*([^\n]{6,100}?(?:项目|合同|服务))"),
        re.compile(
            r"(?:类似项目实施/服务经验证明|类似项目经验证明|项目经验|经验证明)"
            r"\s*[123]\s*[:：-]?\s*(?:[A-Z]{1,4}[-\u2013]?\d{3,}\s*)?"
            r"([^\n]{6,100}?(?:项目|合同|服务))"
        ),
        re.compile(r"合同详见[^\n]{0,80}?[“\"]([^”\"]{6,100}?(?:项目|合同|服务))[”\"]"),
    )

    def titles_for(text_value: str) -> list[str]:
        flat = re.sub(r"\s+", " ", text_value or "")
        out: list[str] = []
        for pattern in title_patterns:
            for match in pattern.finditer(flat):
                title = re.sub(r"\s+", " ", match.group(1)).strip(" ；;。：:-－")
                title = re.sub(r"^(?:业主证明|用户证明|合同编号|H[-\u2013]?\d+)\s*", "", title)
                title = re.sub(r"\s+(?:H[-\u2013]?\d+|合同编号|签订地点).*?$", "", title)
                compact = normalized(title)
                if not (6 <= len(compact) <= 80):
                    continue
                if any(word in title for word in (
                        "2025-2028年行业烟叶平台运维项目", "类似项目实施",
                        "类似项目经验", "项目经验证明", "工作经验证明",
                        "合同详见", "业主证明", "用户证明")):
                    continue
                title = re.sub(r"^\d{8,}\s*", "", title)
                if re.match(r"^2\.8(?:\.|\s)", title):
                    continue
                if title not in out:
                    out.append(title)
        return out[:2]

    marker_pattern = re.compile(
        r"(?:经验证明|项目经验)\s*[123]|项目经理业绩|工作经验证明"
    )
    records: list[ContractRecord] = []
    seen: set[tuple[str, str, tuple[int, ...]]] = set()
    for marker in ordered:
        marker_text = str(marker.get("text") or "")
        if not marker_pattern.search(marker_text):
            continue
        start_no = int(marker.get("page_no") or 0)
        window = [
            page for page in ordered
            if start_no <= int(page.get("page_no") or 0) <= start_no + 4
        ]
        combined = "\n".join(str(page.get("text") or "") for page in window)
        titles = titles_for(combined)
        if not titles:
            continue
        for person in persons:
            role = str(person.role or "")
            name = normalized(person.name)
            named_marker = name and name in normalized(marker_text)
            leader_marker = (
                ("负责人" in role or "项目经理" in role)
                and re.search(r"项目负责人|项目经理", marker_text)
            )
            member_marker = (
                ("驻场" in role or "驻点" in role)
                and bool(re.search(r"两名|多名|驻场运维工程师|驻场服务人员", marker_text))
            )
            if not (named_marker or leader_marker or member_marker):
                continue
            if named_marker and not re.search(
                    rf"(?:驻场|驻点|运维|服务人员|项目负责人|项目经理|参与).{{0,120}}{re.escape(name)}|"
                    rf"{re.escape(name)}.{{0,120}}(?:驻场|驻点|运维|服务人员|项目负责人|项目经理|参与)",
                    normalized(combined)):
                continue
            sealed = bool(re.search(r"盖章|公章|签章|电子印章", combined))
            contract_state = True if re.search(
                r"合同复印件|合同扫描件|中标通知书|业绩证明|合同\s*(?:签订|编号|金额)|项目名称",
                combined,
            ) else None
            social = person.social_security is True or (person.social_months or 0) >= 5
            experience = bool(marker_pattern.search(combined))
            construction_support = bool(re.search(
                r"现场技术支持|驻点[^。；]{0,30}支持|提供\s*\d+\s*年[^。；]{0,20}支持",
                combined,
            ))
            explicit_service = bool(re.search(
                r"(?:运维|维保|维护)服务|系统(?:运维|维保|维护)|驻场(?:运维|服务)|技术服务",
                combined,
            ))
            project_type = (
                "类似系统运维项目实施/服务经验证明"
                if explicit_service or construction_support
                else "系统建设项目/开发合同"
            )
            for title in titles:
                key = (person.name, normalized(title))
                if key in seen:
                    continue
                seen.add(key)
                proof = {
                    "name_reflected": True,
                    "work_proof": True,
                    "experience_proof": experience,
                    "social_security": social,
                }
                records.append(ContractRecord(
                    name=title,
                    pages=[int(page.get("page_no") or 0) for page in window],
                    project_type=project_type,
                    person_names=[person.name],
                    person_proofs={person.name: proof},
                    person_states={
                        "work_proof": True,
                        "experience_proof": experience,
                        "social_security": social,
                    },
                    materials_state={
                        "contract": contract_state,
                        "seal": True if sealed else None,
                    },
                    source="person_proof",
                ))
    return records


def _overlay_social_security(persons: list[PersonRecord], pages: list[dict]) -> None:
    social_sources = [
        str(page.get("text") or "") for page in pages
        if re.search(r"社保|参保|社会保险|缴费", str(page.get("text") or ""))
    ]
    social_text = "\n".join(social_sources)
    if not social_text.strip():
        return
    unit_wide = bool(re.search(r"单位专用|参保缴费总人数|人员信息如下", social_text))
    for person in persons:
        if person.name and person.name in social_text:
            person.social_security = True
        elif unit_wide:
            person.social_security = True
        else:
            person.social_security = False
        if not person.name:
            continue
        paid_periods = set()
        for source in social_sources:
            if person.name not in source or not re.search(r"参保人员信息|缴费明细|个人参保证明|参保证明", source):
                continue
            rows = re.finditer(rf"{re.escape(person.name)}\s+(?:\d{{17}}[\dXx]|\d{{15}})\s+"
                               r"(20\d{2})(\d{2})\s*(?:至|到|[-—–~～])?\s*"
                               r"(20\d{2})(\d{2})\s+(\d{1,3})(?!\d)", source)
            for row in rows:
                from_year, from_month, to_year, to_month, paid_months = map(int, row.groups())
                start = 12 * from_year + from_month
                end = 12 * to_year + to_month
                months = end - start + 1
                if 1 <= from_month <= 12 and 1 <= to_month <= 12 and paid_months == months and 0 < months <= 120:
                    paid_periods.update(range(start, end + 1))
        if paid_periods:
            latest_month = max(paid_periods)
            start_month = latest_month
            while start_month - 1 in paid_periods:
                start_month -= 1
            person.social_months = latest_month - start_month + 1
            person.social_months_from_register = True


# ---------------------------------------------------------------------------
# Value-added services
# ---------------------------------------------------------------------------
_RE_VALUE_HEADING = re.compile(
    r"^\s*(?:[（(]?\d+(?:\.\d+)*[）)]?|[一二三四五六七八九十]+[、.])?\s*"
    r"(?:我司承诺)?(?:提供)?[^\n]{0,24}增值服务(?:方案|承诺|清单)?(?:如下)?\s*[:：]?$", re.M,
)
_RE_VALUE_NUMBERED = re.compile(r"^\s*\d{1,2}\s*、\s*([^\n]{2,40})\s*$", re.M)
_RE_VALUE_CHINESE = re.compile(r"^\s*[一二三四五六七八九十]+\s*、\s*([^\n]{2,40})\s*$", re.M)
_RE_VALUE_LETTER = re.compile(r"^\s*[A-DＡ-Ｄ]\s*[)）.、]\s*([^\n]{2,40})\s*$", re.M)
_RE_VALUE_SUBHEADING = re.compile(
    r"^\s*\d+(?:\.\d+)+\s+([^\n]{2,50}(?:增值|服务|保障|支持|优化|监控|"
    r"备件|维修|节能|协同|集成|报告|平台|系统)[^\n]{0,20})\s*$", re.M,
)
_RE_VALUE_EXCLUDE = (
    "清单", "如下", "包括", "投标人", "中标人", "如不能", "得分",
    "页", "见投标文件", "人员", "员工", "驻场", "核心目标", "方案框架", "增值服务",
)
_RE_VALUE_COMMITMENT = re.compile(r"增值服务\s*(?:承诺函|实施方案|如下|清单)|2\.\d+(?:\.\d+)*\s*增值服务")
_RE_VALUE_RULE_PAGE = re.compile(r"每提供一项.{0,20}得\s*\d|最高不超过\s*\d+\s*分|【证明材料】")
_RE_VALUE_EXTRA = re.compile(r"增加|额外|再增加|延长|补充|免费|新增")
_RE_VALUE_CONCRETE = re.compile(
    r"人员|工程师|支持|服务期|月|培训|名额|备件|巡检|保障|加固|优化|咨询|服务|检查|检测|升级|租赁|建议"
)
_RE_VALUE_STANDARD = re.compile(
    r"故障|SLA|响应时间|解决时间|本地化响应|到场机制|培训|业务共创|成本|效率|管理成本|价值"
)
_RE_VALUE_CATEGORIES = (
    ("extra_staff", re.compile(r"(?:增加|新增|额外).{0,12}(?:名|人).{0,12}(?:技术|支持|工程师|人员)")),
    ("extra_period", re.compile(r"(?:增加|延长|补充).{0,12}(?:月|服务期|服务时间)")),
    ("extra_training", re.compile(r"(?:额外|增加|补充).{0,12}(?:培训|名额)")),
    ("priority_support", re.compile(r"重保|高峰保障|高并发")),
    ("security", re.compile(r"数据安全|安全加固")),
    ("maintenance_check", re.compile(r"健康检查|设备检查|检测|巡检|寿命检测")),
    ("optimization_advice", re.compile(r"优化建议|采购建议|布局建议|降本增效")),
    ("equipment_rental", re.compile(r"设备租赁|租赁服务|临时增加")),
)
_RE_VALUE_ORDINARY = re.compile(
    r"驻场服务|电话支持|远程服务|本地(?:化)?响应|到场机制|故障(?:SLA|响应)|"
    r"应急服务|系统性能优化|性能优化|日常(?:培训|支持)|餐饮|食宿|交通|差旅|咨询服务"
)


def _value_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", raw or "").strip(" ：:；;，,。")
    return name[:80]


def _value_item_category(name: str) -> str | None:
    compact = re.sub(r"[\s（）()“”']+", "", name or "")
    compact = compact.replace("非维保", "")
    if (compact == "增值服务" or re.search(r"需求书|服务团队|投标文件|履约|偏离|说明", compact)
            or (_RE_VALUE_ORDINARY.search(compact)
                                 and not _RE_VALUE_EXTRA.search(compact))):
        return None
    for category, pattern in _RE_VALUE_CATEGORIES:
        if pattern.search(compact):
            return category
    if not _RE_VALUE_CONCRETE.search(compact):
        return None
    if _RE_VALUE_STANDARD.search(compact) and not (
            _RE_VALUE_EXTRA.search(compact) or _RE_VALUE_CATEGORIES[4][1].search(compact)):
        return None
    return "concrete_service"


def _value_items_from_pages(pages: list[dict]) -> tuple[list[ValueItem], bool]:
    for index, page in enumerate(pages):
        text = str(page.get("text") or "")
        heading = _RE_VALUE_HEADING.search(text)
        if heading is None:
            continue
        window = pages[index:index + 8]
        window_text = "\n".join(str(candidate.get("text") or "") for candidate in window)
        if _RE_VALUE_RULE_PAGE.search(text) and not _RE_VALUE_COMMITMENT.search(window_text):
            continue
        items: list[ValueItem] = []
        seen_names: set[str] = set()
        for candidate in window:
            candidate_text = str(candidate.get("text") or "")
            page_no = int(candidate.get("page_no") or 0)
            regions = [candidate_text]
            if _RE_VALUE_COMMITMENT.search(candidate_text):
                commitment = candidate_text
                for marker in ("增值服务承诺函", "增值服务实施方案", "增值服务如下", "增值服务清单"):
                    if marker in commitment:
                        commitment = commitment.split(marker, 1)[-1]
                        break
                commitment = commitment.split("特此承诺", 1)[0]
                regions = [commitment]
            for region in regions:
                for pattern in (_RE_VALUE_NUMBERED, _RE_VALUE_CHINESE, _RE_VALUE_LETTER):
                    for match in pattern.finditer(region):
                        name = _value_name(match.group(1))
                        key = re.sub(r"\s+", "", name).lower()
                        if not _value_item_category(name) or key in seen_names:
                            continue
                        seen_names.add(key)
                        items.append(ValueItem(name=name, page=page_no))
                for match in _RE_VALUE_SUBHEADING.finditer(region):
                    name = _value_name(match.group(1))
                    key = re.sub(r"\s+", "", name).lower()
                    if _value_item_category(name) and key not in seen_names:
                        seen_names.add(key)
                        items.append(ValueItem(name=name, page=page_no))
            if len(items) >= 3:
                break
        return items[:3], True
    return [], False


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
def build_inventory(bidder_id: int, pages: list[dict]) -> MaterialInventory:
    full_text = "\n".join(str(p.get("text") or "") for p in pages)
    facts = extract_bid_facts(full_text)
    # 主标书 = 页数最多的 PDF 文档。项目包把 1 页《开标一览表.pdf》当独立文档上传，
    # 常排在主标书之前；旧实现取"第一份 PDF"会让业绩表/人员表解析打在 1 页文件上，
    # 恒定 0 合同（run128 142/144、run126 c13 五家实证）。表解析只喂该文档自己的
    # 页——页号必须与文件对齐，跨文档合并页会撞号。
    by_doc: dict[int, list[dict]] = {}
    for page in pages:
        by_doc.setdefault(int(page.get("document_id") or 0), []).append(page)
    pdf_docs = [
        (doc_id, str(doc_pages[0].get("stored_path")))
        for doc_id, doc_pages in by_doc.items()
        if doc_pages and str(doc_pages[0].get("stored_path") or "").lower().endswith(".pdf")
    ]
    main_doc_id, pdf_path = (max(pdf_docs, key=lambda item: len(by_doc[item[0]]))
                             if pdf_docs else (None, None))
    main_pages = by_doc.get(main_doc_id, []) if main_doc_id is not None else []
    contracts = _contracts_from_case_table(main_pages, pdf_path)
    for record in contracts:
        record.materials_state["_case_block_complete"] = True
    contracts.extend(_contracts_from_text(pages))
    # A performance-table row and the prose contract copy often describe the
    # same agreement.  Merge the table row into the page-bound text record so
    # the same amount is not counted twice.
    text_records = [record for record in contracts if record.source != "table"]
    merged_table_ids: set[int] = set()
    for table_record in [record for record in contracts if record.source == "table"]:
        for existing in text_records:
            if (table_record.amount_yuan is not None
                    and existing.amount_yuan == table_record.amount_yuan
                    and _contract_name_overlap(existing.name, table_record.name)):
                existing.pages = sorted(set(existing.pages) | set(table_record.pages))
                for kind, numbers in table_record.materials.items():
                    existing.materials.setdefault(kind, [])
                    existing.materials[kind] = sorted(set(existing.materials[kind]) | set(numbers))
                merged_table_ids.add(id(table_record))
                break
    contracts = [record for record in contracts if id(record) not in merged_table_ids]
    # Prefer table records for the same contract, then dedupe across sources.
    deduped: dict[tuple[str, str, str], ContractRecord] = {}
    for record in contracts:
        # Same contract name/amount recurs every year; the period (or signed
        # date) is what distinguishes three separate yearly agreements.
        key = (re.sub(r"\s+", "", record.name), str(record.amount_yuan),
               record.period or record.signed_date or "")
        current = deduped.get(key)
        if current is None:
            # A year heading and a table row often carry slightly different
            # names for the same agreement.  Same amount + overlapping pages is
            # one contract; distinct yearly contracts have disjoint page blocks.
            for existing in deduped.values():
                same_identity = (
                    re.sub(r"\s+", "", existing.name) == re.sub(r"\s+", "", record.name)
                    or not re.sub(r"\s+", "", existing.name)
                    or not re.sub(r"\s+", "", record.name)
                )
                same_period = (
                    not existing.period or not record.period
                    or existing.period == record.period
                ) and (
                    not existing.signed_date or not record.signed_date
                    or existing.signed_date == record.signed_date
                )
                overlapping = bool(set(record.pages) & set(existing.pages))
                same_named_overlap = (
                    same_identity and overlapping
                    and re.sub(r"\s+", "", existing.name) == re.sub(r"\s+", "", record.name)
                )
                if ((record.amount_yuan is not None
                     and existing.amount_yuan == record.amount_yuan
                     and overlapping and same_identity and same_period)
                        or same_named_overlap):
                    current = existing
                    break
            if current is None:
                deduped[key] = record
                continue
        current.pages = sorted(set(current.pages) | set(record.pages))
        current.materials = {**record.materials, **current.materials}
        current.amount_yuan = current.amount_yuan or record.amount_yuan
        current.signed_date = current.signed_date or record.signed_date
        current.period = current.period or record.period
        if record.source == "table":
            current.source = "table"
    contracts = list(deduped.values())
    # A readable table case with no detected attachment headings has confirmed
    # absent proof pages, not an unresolved OCR fact.  Keep explicit material
    # hits as True and only mark a required attachment False when the whole case
    # block supplied no material signal at all.
    for record in contracts:
        if record.source == "table" and not record.materials:
            for kind in ("contract", "invoice", "acceptance"):
                record.materials_state.setdefault(kind, False)

    # PR3（2026-11-24）：目录驱动存在性信号（纯文本计算；shadow=只审计）
    locator_index: dict[str, set[int]] = {}
    try:
        from app.engine import material_locator

        if material_locator.locator_enabled() in ("on", "shadow"):
            locator_index = material_locator.material_page_index(pages)
    except Exception:  # noqa: BLE001 —— 定位失败不阻断
        locator_index = {}
    certs = _certs_from_pages(pages)
    if locator_index.get("cmmi") and not any(
            (c.normalized or "") == "CMMI" for c in certs):
        # 扫描 CMMI 页无 cert_context 词被文本路跳过（run155 c45 全 0 残余
        # 场景）→ 目录列示的 CMMI 窗口登记为存在性记录：backing=None
        # → 计入+疑问（不静默 0），内容核验留给点读/VL
        certs.append(CertRecord(name="CMMI", normalized="CMMI",
                                pages=[min(locator_index["cmmi"])]))
    # Ensure the generic facts list reflects the structured certificate scan.
    cert_names = [c.normalized or c.name for c in certs]
    facts.certificates = list(dict.fromkeys(cert_names))
    try:
        from app.engine.objective import classify_cert_backing, detect_cert_section
        page_pairs = [(str(p.get("source") or "text"), str(p.get("text") or "")) for p in pages]
        _shot_wins = [(_w.start, _w.end) for _w in (
            (material_locator.locate_material_sections(pages).get("query_screenshot") or [])
            if locator_index else []
        )]
        detection = detect_cert_section(pages, locator_windows=_shot_wins or None)
        cert_section = (detection.state is True, detection.basis)
        cert_section_state = detection.state  # 三态进 audit（None=盲态不猜）
        cert_section_hits = len(detection.hits)
        backing = classify_cert_backing(facts, page_pairs, cert_section)
    except Exception:  # noqa: BLE001
        cert_section = (False, "")
        cert_section_state = None
        cert_section_hits = 0
        backing = {}
    for cert in certs:
        key = cert.normalized or cert.name
        if key in backing:
            cert.backing = backing[key]
    persons = _persons_from_tables(main_pages, pdf_path)
    personnel_table_found = bool(persons)
    if not persons:
        persons = _persons_from_pages(pages)
    by_name = {person.name: person for person in persons}
    for leader in _persons_from_leader_forms(pages):
        existing = by_name.get(leader.name)
        if existing is not None:
            if not existing.title:
                existing.title = leader.title
            if not existing.role:
                existing.role = leader.role
        else:
            persons.append(leader)
            by_name[leader.name] = leader
    _overlay_personnel_years(persons, pages)
    _overlay_social_security(persons, pages)
    # Build person proof cases only after identity, work-year and social overlays
    # are complete, so their required-material flags describe the same immutable
    # person record that the scorer consumes.
    contracts.extend(_person_case_records_from_pages(pages, persons))
    _attach_person_case_proofs(contracts, persons, pages)
    value_items, value_found = _value_items_from_pages(pages)
    performance_found = any(
        "业绩" in str(p.get("text") or "") or "合同" in str(p.get("text") or "")
        for p in pages
    )
    personnel_found = any("姓名" in str(p.get("text") or "") and
                          re.search(r"人员|团队|项目负责人", str(p.get("text") or ""))
                          for p in pages)
    return MaterialInventory(
        bidder_id=bidder_id,
        pages=pages,
        full_text=full_text,
        facts=facts,
        contracts=contracts,
        certs=certs,
        persons=persons,
        value_items=value_items,
        cert_section_present=bool(cert_section[0]),
        cert_backing=backing,
        locator_index=locator_index,
        performance_section_found=performance_found,
        personnel_section_found=personnel_found,
        personnel_table_found=personnel_table_found,
        personnel_detail_found=any(
            p.title or p.certs or p.years is not None or p.experience for p in persons
        ),
        value_section_found=value_found,
        audit={
            "contracts": len(contracts),
            "certificates": len(certs),
            "persons": len(persons),
            "personnel_table": personnel_table_found,
            "personnel_detail": any(
                p.title or p.certs or p.years is not None or p.experience for p in persons
            ),
            "value_items": len(value_items),
            "cert_section_present": bool(cert_section[0]),
            # 三态与命中数（2026-11-18 ADR 0015：bool 字段保持兼容 fastpath，
            # None 盲态语义留痕不猜）
            "cert_section_state": cert_section_state,
            "cert_section_hits": cert_section_hits,
            "locator_kinds": {k: len(v) for k, v in locator_index.items() if v},
        },
    )
