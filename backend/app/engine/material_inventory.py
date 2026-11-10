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
    source: str = "text"


@dataclass
class CertRecord:
    name: str
    normalized: str
    level: str | None = None
    valid_until: str | None = None
    pages: list[int] = field(default_factory=list)
    backing: bool | None = None


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
    r"(?:合同(?:价款|金额|总价|价格)|合同总价)[^。\n]{0,30}?"
    r"(?:人民币|￥|¥)?\s*([0-9][0-9,，]*(?:\.[0-9]+)?)\s*(万元|元)",
)
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
    if not match:
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
            return []
        blocks = link_case_blocks(rows, recs, section)
        # Fast inventory: skip page_image_ratios/classify_materials, which scan
        # the whole PDF.  Table rows are accepted as structured contract claims;
        # prose blocks still supply amount/date/material pages.
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
        for key in ("contract_pages", "invoice_pages", "acceptance_pages", "evidence_pages"):
            for number in case.get(key) or []:
                try:
                    pages_found.append(int(number))
                except Exception:  # noqa: BLE001
                    continue
        materials = {}
        for kind, keys in (("contract", ("contract_pages",)),
                           ("invoice", ("invoice_pages",)),
                           ("acceptance", ("acceptance_pages",))):
            for key in keys:
                if case.get(key):
                    materials[kind] = sorted({int(n) for n in case[key]})
        out.append(ContractRecord(
            name=name, amount_yuan=amount_d,
            signed_date=str(case.get("sign_date") or "") or None,
            pages=sorted(set(pages_found)), materials=materials, source="table",
        ))
    return out


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------
_RE_LEVEL_TOKEN = r"[壹贰叁肆一二三四]级|一级|二级|三级"
_RE_QUALIFICATION_CERT = re.compile(
    r"(电子与智能化工程专业承包)[^。\n]{0,12}?(" + _RE_LEVEL_TOKEN + r")"
)
_RE_AV_INTEGRATION_CERT = re.compile(
    r"(音视频集成工程)[^。\n]{0,30}?(" + _RE_LEVEL_TOKEN + r")"
)
_RE_ISO_TOKEN = re.compile(
    r"ISO\s*/?\s*IEC?\s*(\d{4,5})|ISO\s*(\d{4,5})|GB/?T\s*(\d{4,5})"
)
_RE_SYSTEM_CERT = re.compile(
    r"(质量管理体系|环境管理体系|职业健康安全管理体系|信息技术服务管理体系|信息安全管理体系)"
)
_RE_VALID_TO = re.compile(
    r"有效期(?:至|到)[：:]?\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)


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
            r"资质类别及等级|资质证书|认证机构",
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
        if re.search(r"\bCMMI\b|CMMI\s*[一二三四五1-5]\s*级", text):
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
    """Map '项目团队人员专业配置说明' experience text to named persons."""
    if not persons:
        return
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


def _overlay_social_security(persons: list[PersonRecord], pages: list[dict]) -> None:
    social_text = "\n".join(
        str(page.get("text") or "") for page in pages
        if re.search(r"社保|参保|社会保险|缴费", str(page.get("text") or ""))
    )
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


# ---------------------------------------------------------------------------
# Value-added services
# ---------------------------------------------------------------------------
_RE_VALUE_HEADING = re.compile(
    r"^\s*(?:[（(]?\d+(?:\.\d+)*[）)]?|[一二三四五六七八九十]+[、.])?\s*"
    r"增值服务(?:方案|承诺|清单)?\s*$", re.M,
)
_RE_VALUE_NUMBERED = re.compile(r"^\s*\d{1,2}\s*、\s*([^\n]{2,40})\s*$", re.M)
_RE_VALUE_CHINESE = re.compile(r"^\s*[一二三四五六七八九十]+\s*、\s*([^\n]{2,40})\s*$", re.M)
_RE_VALUE_LETTER = re.compile(r"^\s*[A-DＡ-Ｄ]\s*[)）.、]\s*([^\n]{2,40})\s*$", re.M)
_RE_VALUE_SUBHEADING = re.compile(
    r"^\s*\d+(?:\.\d+){2,}\s+([^\n]{2,50}(?:增值|服务|保障|支持|优化|监控|"
    r"备件|维修|节能|协同|集成|报告|平台|系统)[^\n]{0,20})\s*$", re.M,
)
_RE_VALUE_EXCLUDE = (
    "清单", "如下", "包括", "投标人", "中标人", "如不能", "得分",
    "页", "见投标文件", "人员", "员工", "核心目标", "方案框架", "增值服务",
)


def _value_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", raw or "").strip(" ：:；;，,。")
    return name[:40]


def _value_items_from_pages(pages: list[dict]) -> tuple[list[ValueItem], bool]:
    for index, page in enumerate(pages):
        text = str(page.get("text") or "")
        heading = _RE_VALUE_HEADING.search(text)
        if heading is None:
            continue
        items: list[ValueItem] = []
        seen: set[str] = set()
        candidates: list[tuple[str, int]] = []
        for candidate in pages[index:index + 5]:
            candidates.append((str(candidate.get("text") or ""), int(candidate.get("page_no") or 0)))
        for candidate_text, page_no in candidates:
            for pattern in (_RE_VALUE_SUBHEADING, _RE_VALUE_CHINESE, _RE_VALUE_LETTER,
                            _RE_VALUE_NUMBERED):
                for match in pattern.finditer(candidate_text):
                    name = _value_name(match.group(1))
                    if len(name) < 2 or len(name) > 40 or name in seen:
                        continue
                    if any(word in name for word in _RE_VALUE_EXCLUDE):
                        continue
                    seen.add(name)
                    items.append(ValueItem(name=name, page=page_no))
            if len(items) >= 8:
                break
        if items:
            return items, True
    return [], False


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
def build_inventory(bidder_id: int, pages: list[dict]) -> MaterialInventory:
    full_text = "\n".join(str(p.get("text") or "") for p in pages)
    facts = extract_bid_facts(full_text)
    pdf_path = next((str(p.get("stored_path")) for p in pages
                     if str(p.get("stored_path") or "").lower().endswith(".pdf")), None)
    contracts = _contracts_from_case_table(pages, pdf_path)
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
                if (record.amount_yuan is not None
                        and existing.amount_yuan == record.amount_yuan
                        and set(record.pages) & set(existing.pages)):
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

    certs = _certs_from_pages(pages)
    # Ensure the generic facts list reflects the structured certificate scan.
    cert_names = [c.normalized or c.name for c in certs]
    facts.certificates = list(dict.fromkeys(cert_names))
    try:
        from app.engine.objective import cert_evidence_present, classify_cert_backing
        page_pairs = [(str(p.get("source") or "text"), str(p.get("text") or "")) for p in pages]
        cert_section = cert_evidence_present(page_pairs)
        backing = classify_cert_backing(facts, page_pairs, cert_section)
    except Exception:  # noqa: BLE001
        cert_section = (False, "")
        backing = {}
    for cert in certs:
        key = cert.normalized or cert.name
        if key in backing:
            cert.backing = backing[key]
    persons = _persons_from_tables(pages, pdf_path)
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
        elif leader.title:
            persons.append(leader)
            by_name[leader.name] = leader
    _overlay_personnel_years(persons, pages)
    _overlay_social_security(persons, pages)
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
        },
    )
