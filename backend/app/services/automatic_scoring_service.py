"""Evidence-backed automatic evaluation with per-cell, resumable checkpoints.

Business nonconformity is a completed assessment. Infrastructure failure is not a
score or a qualification verdict. All writes are made by the coordinator so each
cell's score, citations, audit and completion marker commit together.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
import uuid
import time
from queue import Empty, SimpleQueue
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from app import config
from app.db.models import (
    Bidder, Criterion, Document, DocumentPage, EvaluationRun, EvidenceCitation,
    ReviewRecord, ScoreRecord, utcnow,
)
from app.db.session import session_scope
from app.engine.facts import BidFacts
from app.engine.models import PriceBid
from app.engine.objective import (
    normalize_cert, parse_required_artifacts, score_attribute_tier, score_count,
    score_count_tier, score_deviation_deduct_table, score_value_linear, score_value_tier,
)
from app.errors import AppError, LLMNotConfigured, ValidationFailed
from app.jobs.queue import JobCancelled
from app.llm.base import LLMError

log = logging.getLogger(__name__)

_ENGINES = {
    "attribute_tier": score_attribute_tier, "count_tier": score_count_tier,
    "deviation_deduct_table": score_deviation_deduct_table,
    "value_tier": score_value_tier, "value_linear": score_value_linear,
}


@dataclass
class CellOutcome:
    verdict: str
    reasoning: str
    score: Decimal | None = None
    evidence: list[dict] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)
    basis: str = "deterministic"
    attempts: int = 1
    raw: dict = field(default_factory=dict)
    warnings: list[dict] = field(default_factory=list)


def _decimal(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LLMError(f"{label}不是有效数值") from exc
    if not number.is_finite():
        raise LLMError(f"{label}不是有限数值")
    return number


def _score_in_range(value: Any, criterion: dict) -> Decimal:
    score = _decimal(value, "得分")
    maximum = _decimal(criterion.get("max_score"), "满分")
    if score < 0 or score > maximum:
        raise LLMError(f"得分 {score} 超出细则范围 0–{maximum}")
    return score.quantize(Decimal("0.01"))


def _load_pages(bidder_id: int) -> tuple[list[dict], str]:
    from app.services.automatic_document_service import annotate_page_readability

    with session_scope() as s:
        docs = s.execute(select(Document).where(Document.bidder_id == bidder_id)
                         .order_by(Document.id)).scalars().all()
        if not docs:
            raise LLMError("未找到投标文件，无法执行自动评审")
        pages: list[dict] = []
        data_class = "synthetic"
        for doc in docs:
            if doc.parse_status not in ("parsed", "degraded", "ocr_required"):
                raise LLMError(f"投标文件 {doc.id} 尚未成功解析")
            if doc.data_class == "confidential":
                data_class = "confidential"
            elif doc.data_class == "redacted_sample" and data_class != "confidential":
                data_class = "redacted_sample"
            rows = s.execute(select(DocumentPage).where(DocumentPage.document_id == doc.id)
                             .order_by(DocumentPage.page_no)).scalars().all()
            if not rows:
                raise LLMError(f"投标文件 {doc.id} 没有可评审页面")
            if doc.page_count and len(rows) != doc.page_count:
                raise LLMError(f"投标文件 {doc.id} 的解析页数不完整，不能判定材料缺失")
            unreadable_pages = set()
            for warning in doc.warnings_json or []:
                text = str(warning)
                match = re.search(r"第\s*(\d+)\s*页", text)
                if match and any(token in text for token in
                                 ("失败", "产出过少", "疑似扫描件", "跳过", "未识别")):
                    unreadable_pages.add(int(match.group(1)))
            document_pages = []
            for page in rows:
                known_blank = page.source == "skipped" and not (page.text or "").strip()
                unreadable = not known_blank and (
                    page.page_no in unreadable_pages
                    or not (page.text or "").strip()
                    or (not page.has_text_layer and page.source not in ("ocr", "ocr_img"))
                )
                document_pages.append({
                    "document_id": doc.id, "page_no": page.page_no,
                    "text": page.text or "", "source": page.source or "text",
                    "has_text_layer": page.has_text_layer,
                    "stored_path": doc.stored_path, "sha256": doc.sha256,
                    "page_count": doc.page_count,
                    "width": page.width, "height": page.height,
                    "known_blank": known_blank, "unreadable": unreadable,
                    "requires_vision": unreadable,
                })
            pages.extend(annotate_page_readability(document_pages, doc.stored_path, doc.warnings_json))
        return pages, data_class


def _page_text(pages: list[dict]) -> str:
    return "\n".join(str(p.get("text") or "") for p in pages)


def _is_visual_requirement(criterion: dict) -> bool:
    # A cited law can mention signatures while this actual clause only asks for
    # a document/section. Determine capability from the frozen item requirement.
    # Language clauses exempt signatures/seals from Chinese-text requirements;
    # the exemption itself does not require checking a signature/seal image.
    # Strip only a complete enumeration, retaining any actual visual obligation
    # elsewhere in the same sentence or another field.
    item = r"(?:签字|签章|盖章|印章|签名|专用名称|专有名称|专有名词|专业术语|专用术语|技术术语|商标)"
    language_exception = (
        rf"除了?\s*{item}(?:\s*[、，,及和或/]\s*{item})*\s*(?:等\s*)?"
        r"(?:(?:特殊)?(?:情形|情况)\s*)?(?:以)?外"
    )
    parts = []
    for key in ("name", "description", "required_evidence"):
        for clause in re.split(r"[。；;\n]", str(criterion.get(key) or "")):
            if re.search(r"中文|汉语|汉字|语言|文字", clause):
                clause = re.sub(language_exception, "", clause)
            parts.append(clause)
    text = " ".join(parts)
    return any(word in text for word in ("签字", "签章", "盖章", "公章", "印章", "签名", "照片"))


def _requested_visual_pages(previous: dict | None, pages: list[dict]) -> tuple[bool, set[tuple[int, int]]]:
    """Use unresolved page-image requests to fetch sources, never as verdict facts."""
    message = str((previous or {}).get("error") or "")
    if not any(term in message for term in ("页图", "图像", "图片", "签章", "公章", "印章", "盖章", "签字")):
        return False, set()
    numbers = set()
    for match in re.finditer(r"第?\s*(\d{1,5})\s*(?:[-—–~～至到]\s*(\d{1,5}))?\s*页", message):
        start, end = int(match[1]), int(match[2] or match[1])
        if 0 < start <= end <= max((int(p["page_no"]) for p in pages), default=0):
            numbers.update(range(start, end + 1))
    # A request cannot cause an out-of-document read or arbitrary file access.
    return True, {(int(p["document_id"]), int(p["page_no"])) for p in pages if int(p["page_no"]) in numbers}


def _supplement_pages(criterion: dict, pages: list[dict], *, visual: bool, exhaustive: bool = False) -> list[dict]:
    """Supplement only relevant source pages; retain originals and file/page IDs."""
    from app.engine.auto_review import select_candidate_pages

    copied = [dict(p) for p in pages]
    requested = set(criterion.get("_visual_page_keys") or [])
    if visual and exhaustive:
        candidates = [p for p in copied if not p.get("known_blank")]
    elif visual and requested:
        candidates = [p for p in copied if (int(p["document_id"]), int(p["page_no"])) in requested]
    else:
        candidates = select_candidate_pages(criterion, copied, max_pages=8)
    # Exhaustive counts/absence require every unreadable page, including scans
    # whose headings cannot yet be retrieved by a text query.
    candidates = candidates + [p for p in copied if (p.get("unreadable") or p.get("requires_vision")) and not p.get("known_blank")]
    targets = {(int(p["document_id"]), int(p["page_no"])) for p in candidates}
    if not targets:
        raise LLMError("自动补检没有定位到可读取的候选页")
    if visual:
        from app.evidence.renderer import render_page_png

        for page in copied:
            if (page["document_id"], page["page_no"]) not in targets:
                continue
            if not str(page.get("stored_path") or "").lower().endswith(".pdf"):
                continue
            path = render_page_png(page["sha256"], page["stored_path"], page["page_no"],
                expected_page_count=page.get("page_count"), expected_width=page.get("width"),
                expected_height=page.get("height"), expected_rotation=page.get("rotation"))
            content = path.read_bytes()
            page["image_url"] = "data:image/png;base64," + base64.b64encode(content).decode("ascii")
            page["image_sha256"] = hashlib.sha256(content).hexdigest()
        if not any(p.get("image_url") for p in copied):
            raise LLMError("需要视觉评审的原页无法渲染")
        return copied
    from app.parsing.pdf_pymupdf import ocr_pages_text

    by_doc: dict[int, list[int]] = {}
    for page in copied:
        # Successful OCR engines can return no text for an actual photograph.
        # Keep that original-page visual obligation instead of repeating OCR.
        if page.get("ocr_textless_issues"):
            continue
        if page.get("supplemented") and not page.get("unreadable") and not page.get("requires_vision"):
            continue
        if (page["document_id"], page["page_no"]) in targets and (
            page.get("source") != "text" or not page.get("has_text_layer", True)
            or page.get("unreadable") or page.get("requires_vision")
            or len(str(page.get("text") or "")) < config.OCR_MIXED_PAGE_MAX_CHARS
            or _is_visual_requirement(criterion)
        ) and str(page.get("stored_path") or "").lower().endswith(".pdf"):
            by_doc.setdefault(page["document_id"], []).append(page["page_no"])
    for doc_id, page_numbers in by_doc.items():
        doc_pages = [p for p in copied if p["document_id"] == doc_id]
        textless: dict[int, list[dict]] = {}

        def require_visual(issue):
            textless.setdefault(int(issue["page_no"]), []).append(dict(issue))

        extracted = ocr_pages_text(doc_pages[0]["stored_path"], page_numbers,
                                   min_image_ratio=0.10, on_textless=require_visual)
        for page in doc_pages:
            extra = extracted.get(page["page_no"])
            if extra and extra.strip():
                page["text"] = (page["text"] + "\n" + extra).strip()
                page["source"] = "ocr"
                page["supplemented"] = True
                if (page["page_no"] not in textless
                        and len(re.sub(r"\s", "", extra)) >= config.OCR_PAGE_MIN_CHARS):
                    page["unreadable"] = False
                    page["requires_vision"] = False
            if page["page_no"] in textless:
                page["ocr_textless_issues"] = textless[page["page_no"]]
                page["unreadable"] = True
                page["requires_vision"] = True
                page["readability_reason"] = "原页部分图像未识别出文字，须以原页图像实际核验"
    return copied


def _is_duplicate_summary(criterion: dict, criteria: list[dict]) -> bool:
    """A match-with-points parent fully covered by scoring children."""
    if criterion.get("score_type") != "match" or criterion.get("max_score") is None:
        return False
    from app.parsing.normalize import normalize_no_ws

    node = criterion.get("source_node_id")
    if not node:
        return False
    parent_text = normalize_no_ws(str(criterion.get("description") or ""))
    if not parent_text:
        return False
    covered = []
    for sibling in criteria:
        if sibling is criterion or sibling.get("source_node_id") != node:
            continue
        if sibling.get("score_type") not in ("count", "subjective", "formula"):
            continue
        if sibling.get("max_score") is None:
            continue
        child_text = normalize_no_ws(str(sibling.get("description") or ""))
        if child_text and (child_text[:40] in parent_text or parent_text[:40] in child_text):
            covered.append(_decimal(sibling.get("max_score"), "满分"))
    return len(covered) >= 2 and sum(covered, Decimal(0)) <= _decimal(
        criterion.get("max_score"), "满分")


def _route_criterion(criterion: dict) -> dict:
    from app.services.execution_version import execution_config_value

    if criterion.get("score_type") != "subjective" or not execution_config_value("objective_route_enabled"):
        return dict(criterion)
    from app.rules.check_infer import infer_objective_route

    route = infer_objective_route(criterion)
    if route is None:
        return dict(criterion)
    engine, params = route
    return {**criterion, "_objective_route": engine,
            "params": {**(criterion.get("params") or {}), **params,
                       **({"engine": engine} if engine != "count" else {})}}


def _validate_objective_rule(criterion: dict, params: dict) -> None:
    """Do not let legacy primitives turn incomplete parameters into zero points."""
    engine = params.get("engine") or "count"
    maximum = _decimal(criterion.get("max_score"), "客观项满分")
    if engine in ("count_tier", "attribute_tier", "value_tier") or params.get("tiers"):
        tiers = params.get("tiers")
        if not isinstance(tiers, list) or not tiers:
            raise LLMError("冻结客观分档规则缺少完整档位")
        amount_tiers = any(isinstance(t, dict) and ("min_amount_yuan" in t or "max_amount_yuan" in t) for t in tiers)
        if amount_tiers and params.get("field") != "performance" and not params.get("person_scope"):
            raise LLMError("金额分档只能用于逐案例业绩，不能套用其他计数口径")
        for tier in tiers:
            if not isinstance(tier, dict):
                raise LLMError("冻结客观分档结构无效")
            if amount_tiers:
                if engine not in ("count", "count_tier") or "min_count" in tier or not any(
                    tier.get(k) is not None for k in ("min_amount_yuan", "max_amount_yuan")
                ):
                    raise LLMError("金额档位结构混杂或缺少明确边界")
                if _decimal(tier.get("per_item"), "金额档每项分值") < 0:
                    raise LLMError("金额档每项分值不能为负")
                bounds = [_decimal(tier[k], "金额档边界") for k in ("min_amount_yuan", "max_amount_yuan")
                          if tier.get(k) is not None]
                if any(v < 0 for v in bounds) or (len(bounds) == 2 and bounds[0] >= bounds[1]):
                    raise LLMError("金额档边界无效")
                continue
            if not 0 <= _decimal(tier.get("score"), "档位分值") <= maximum:
                raise LLMError("冻结客观分档的分值无效")
            if engine == "value_tier":
                bounds = [tier[key] for key in ("min_value", "max_value") if tier.get(key) is not None]
                if not bounds or any(_decimal(v, "档位边界") < 0 for v in bounds):
                    raise LLMError("连续值分档缺少合法边界")
                if len(bounds) == 2 and _decimal(bounds[0], "档位下界") > _decimal(bounds[1], "档位上界"):
                    raise LLMError("连续值分档边界顺序错误")
            elif engine == "attribute_tier":
                attr = params.get("attr") or "years"
                key = "min" if attr == "years" else "level" if attr == "title" else "match"
                if tier.get(key) is None and not tier.get("match_any"):
                    raise LLMError("属性分档缺少明确的匹配条件")
                if key == "min" and _decimal(tier["min"], "年限档位") < 0:
                    raise LLMError("年限档位不能为负")
            else:
                key = "min_count" if tier.get("min_count") is not None else "min_amount_yuan"
                value = _decimal(tier.get(key), "计数或金额档位边界")
                if value < 0 or (key == "min_count" and value != value.to_integral_value()):
                    raise LLMError("计数分档边界无效")
    elif engine == "count" and _decimal(params.get("per_item"), "每项分值") <= 0:
        raise LLMError("冻结计数规则缺少合法的每项分值")
    elif engine == "deviation_deduct_table":
        penalties = [_decimal(params[k], "偏离扣分率") for k in ("per_missing", "per_negative") if k in params]
        if not penalties or any(v < 0 for v in penalties) or not any(v > 0 for v in penalties):
            raise LLMError("冻结偏离扣分规则缺少明确扣率")
    elif engine == "value_linear":
        if (_decimal(params.get("base"), "线性计分基线") < 0
                or _decimal(params.get("step"), "线性计分步长") <= 0
                or _decimal(params.get("per"), "线性每档分值") <= 0):
            raise LLMError("冻结线性计分规则不完整")
    if params.get("max_items") is not None:
        cap = _decimal(params["max_items"], "计数上限")
        if cap < 0 or cap != cap.to_integral_value():
            raise LLMError("计数上限必须是非负整数")


def _inject_deterministic_cert_backing(criterion: dict, decision, pages: list[dict]) -> None:
    """Count certificates from page text instead of trusting per-book model flags."""
    certificates = decision.facts.get("certificates") or []
    if not certificates:
        return
    from app.engine.facts import extract_bid_facts
    from app.engine.objective import cert_evidence_present, classify_cert_backing

    facts = extract_bid_facts(_page_text(pages))
    facts.certificates = list(certificates)
    page_pairs = [(str(p.get("source") or "text"), str(p.get("text") or "")) for p in pages]
    section = cert_evidence_present(page_pairs)
    backing = classify_cert_backing(facts, page_pairs, section)
    if backing:
        decision.facts["cert_backing"] = backing
        decision.facts["cert_section_present"] = bool(section[0])


def _verified_count(criterion: dict, decision, pages: list[dict]) -> tuple[Decimal, str, dict]:
    """AI supplies evidence-backed facts; existing rule engines supply arithmetic."""
    values = dict(decision.facts or {})
    if not decision.coverage_complete:
        raise LLMError("客观计分事实尚未覆盖全部文件页，不能用局部检索结果确定完整计数")
    params = dict(criterion.get("params") or {})
    _validate_objective_rule(criterion, params)
    amount_tiers = any(isinstance(t, dict) and ("min_amount_yuan" in t or "max_amount_yuan" in t)
                       for t in params.get("tiers") or [])
    facts = BidFacts(text=_page_text(pages))
    certificates = values.get("certificates", [])
    if not isinstance(certificates, list) or any(not isinstance(v, str) or not v.strip() for v in certificates):
        raise LLMError("已核验证书清单格式无效")
    cert_keys = [normalize_cert(cert) for cert in certificates]
    if len(cert_keys) != len(set(cert_keys)):
        raise LLMError("证书清单存在重复，须合并同一证书的跨页证明后重算")
    facts.certificates = list(certificates)
    facts.performance_amounts = [_decimal(v, "业绩金额") for v in values.get("performance_amounts", [])]
    facts.person_attributes = list(values.get("person_attributes") or [])
    people = [re.sub(r"\s", "", str(p.get("name") or ""))
              for p in facts.person_attributes if isinstance(p, dict)]
    if len(people) != len(set(people)):
        raise LLMError("人员清单存在重复，须合并同一人员的证明材料后重算")
    for name in ("response_hours", "warranty_months"):
        if name in values:
            number = _decimal(values[name], name)
            if number < 0:
                raise LLMError(f"{name}不能小于零")
            values[name] = str(number)
    prepared = {**criterion, "verified_facts": values}
    backing = values.get("cert_backing") or {}
    if certificates:
        if not isinstance(backing, dict):
            raise LLMError("证书背书核验结果无效")
        backing = {normalize_cert(str(k)): v for k, v in backing.items()}
        if any(not isinstance(backing.get(normalize_cert(cert)), bool) for cert in certificates):
            raise LLMError("每本证书都必须完成实际证明材料核验")
        prepared["cert_backing"] = backing
    if params.get("engine") == "attribute_tier" and (params.get("attr") or "years") in ("years", "title"):
        attr = params.get("attr") or "years"
        for person in facts.person_attributes:
            if not isinstance(person, dict) or not person.get("name") or person.get(attr) is None:
                raise LLMError("人员属性尚未逐人核验，不能按未命中档位计零分")
            if attr == "years" and _decimal(person[attr], "从业年限") < 0:
                raise LLMError("从业年限不能为负数")
    requirements = str(criterion.get("required_evidence") or "")
    if not requirements:
        rule_text = str(criterion.get("description") or "") + "\n" + str(criterion.get("source_quote") or "")
        material_names = parse_required_artifacts(rule_text)
        if "【证明材料】" in rule_text or re.search(r"提供.{0,30}(?:合同|发票|验收|业主证明|社保)", rule_text):
            requirements = rule_text
        elif material_names:
            requirements = "、".join(material_names)
    if requirements:
        # Keep the original requirement: noun extraction can omit short labels
        # such as “合同、发票”, which must still be applied as hard material gates.
        prepared["required_artifacts"] = requirements
        prepared["required_artifact_names"] = parse_required_artifacts(requirements)
        prepared["required_artifacts_raw"] = requirements
    material_rule = requirements + str(criterion.get("description") or "") + str(criterion.get("source_quote") or "")
    if certificates and "截图" in material_rule:
        if not isinstance(values.get("cert_section_present"), bool):
            raise LLMError("证书所需查询截图尚未核验，不能默认已提供")
        if values["cert_section_present"] is False:
            prepared["cert_backing"] = {normalize_cert(cert): False for cert in certificates}
    cases = values.get("verified_cases")
    if cases is not None:
        if not isinstance(cases, list):
            raise LLMError("逐案例核验结果不是列表")
        prepared["verified_cases"] = cases
        from app.engine.objective import _normalize_role, _required_flags

        flags = _required_flags(
            str(prepared.get("required_artifacts") or ""),
            person_scope=str(params.get("person_scope") or ""),
            criterion_name=str(criterion.get("name") or ""),
            rule_text=str(criterion.get("description") or ""),
        )
        identities = set()
        for case in cases:
            if not isinstance(case, dict) or not str(case.get("name") or "").strip():
                raise LLMError("案例缺少可核验名称")
            if (str(case.get("sanitized") or "ok") != "ok"
                    or any(case.get(key) for key in ("unresolved", "verification_failed", "abstain", "provisional"))):
                raise LLMError("案例仍有引文或材料核验失败，须补评后再计算完整数量")
            identity = (re.sub(r"\s", "", case["name"]), str(case.get("person") or ""))
            if identity in identities:
                raise LLMError("案例清单存在重复，须合并同一案例的跨页证明材料后重算")
            identities.add(identity)
            # Missing material flags (None) are handled by score_count's
            # three-state gate (counted + marked unverified).  Refusing here
            # skipped every performance cell on real scans (run 88/89).
            if params.get("person_scope"):
                role = _normalize_role(case)
                if role is None:
                    raise LLMError("人员案例未标明负责人或团队成员归属")
                case["role"] = role
            if params.get("min_amount_yuan") or amount_tiers:
                if case.get("amount_yuan") is None or _decimal(case["amount_yuan"], "案例金额") < 0:
                    raise LLMError("案例金额门槛仍无可核验的金额")
            if params.get("person_scope") == "leader" and "运维" in (
                str(criterion.get("name") or "") + str(criterion.get("description") or "")
            ) and not isinstance(case.get("type_eligible"), bool):
                raise LLMError("类似运维案例的项目类型尚未核验")
    if isinstance(values.get("cert_section_present"), bool):
        prepared["cert_section_present"] = values["cert_section_present"]
    engine = params.get("engine")
    if engine and engine not in _ENGINES and engine != "count":
        raise LLMError(f"尚未支持的客观评分原语：{engine}")
    scorer = _ENGINES.get(engine, score_count)
    if amount_tiers and cases is None:
        raise LLMError("金额分档缺少逐案例金额与证明材料核验")
    if engine == "count_tier" and (params.get("field") == "performance" or params.get("person_scope") or amount_tiers):
        # A tier changes arithmetic, not the evidence requirements for each case.
        # The legacy tier primitive only counts amounts; the verified case path
        # applies material/type/amount gates before the same frozen count tiers.
        if cases is None:
            raise LLMError("业绩分档缺少逐案例证明材料核验")
        scorer = score_count
    result = scorer(prepared, params, facts)
    if result.unresolved:
        if decision.confirmed_absent and decision.coverage_complete and result.unresolved == "no_facts":
            rule = requirements + str(criterion.get("description") or "") + str(criterion.get("source_quote") or "")
            if engine in (None, "count", "count_tier", "attribute_tier"):
                score = Decimal(0)
                if engine != "attribute_tier" and params.get("tiers") and not amount_tiers:
                    from app.engine.objective import _tier_score
                    score = _tier_score(params["tiers"], Decimal(0)) or Decimal(0)
                return _score_in_range(score, criterion), f"完整检索确认符合计分条件的数量为 0，按冻结计数规则计 {score} 分", values
            if re.search(r"不(?:予)?得分|不计分|(?:计|得|为|扣至)\s*0\s*分", rule):
                return Decimal(0), "完整材料检索确认无所需证明，按细则明确规定计 0 分", values
        raise LLMError(f"客观项事实或评分口径仍未解决：{result.unresolved}")
    if result.unverified_counted and result.score_verified_only is not None:
        # Frozen count semantics already count materials with unreadable pages
        # and flag them; keep the score but expose the verified-only value for
        # audit instead of skipping the whole objective cell.
        values["unverified_counted"] = result.unverified_counted
        values["score_verified_only"] = str(result.score_verified_only)
        return (_score_in_range(result.score, criterion),
                result.detail + (f"；{result.unverified_counted} 个案例材料未核验，按冻结口径计入并标注"
                                 f"（剔除未核验后 {result.score_verified_only} 分）"),
                values)
    if result.zero_reason in ("unreadable", "cases_unverified", "gate_named_missing"):
        raise LLMError("证明材料核验尚未完成，不能将暂定分作为最终分")
    # Heuristic conflict alone is discharged by the evidence-backed second pass;
    # missing factual inputs above remain execution failures.
    return _score_in_range(result.score, criterion), result.detail, values


def _verified_price_limit(criterion: dict, decision) -> tuple[str, str, dict]:
    """Never infer annual compliance from a confirmed total or a model verdict."""
    params = criterion.get("params") or {}
    frozen = criterion.get("_frozen_bidder") or {}
    values = {}
    missing = []
    comparisons = []
    exceeded = False
    checks = [("bid_price", params.get("max_amount_yuan"))] if params.get("max_amount_yuan") is not None else []
    checks += [(f"yearly_bid_price_{i}", cap) for i, cap in enumerate(params.get("yearly_max_amount_yuan") or [], 1)]
    for key, cap in checks:
        limit = _decimal(cap, "最高限价")
        value = decision.facts.get(key)
        if key == "bid_price" and frozen.get("price_status") == "confirmed":
            value = frozen.get("bid_price")
            if decision.facts.get(key) is not None and _decimal(decision.facts[key], key) != _decimal(value, key):
                raise LLMError("原页总报价与冻结已确认报价冲突，须补抽核验")
        if value is None:
            missing.append(key)
            continue
        number = _decimal(value, key)
        if number < 0 or limit <= 0:
            raise LLMError("报价或最高限价数值无效")
        values[key] = str(number)
        exceeded |= number > limit
        comparisons.append(f"{key}: {number} {'>' if number > limit else '≤'} {limit} 元")
    if exceeded:
        return "fail", "；".join(comparisons), values
    if missing:
        if not (decision.confirmed_absent and decision.coverage_complete):
            raise LLMError("必需报价明细尚未核验：" + "、".join(missing) + "；须继续检索，全文确认缺失后按条款判不符合，不能以总价代替年度报价")
        return "fail", "；".join(comparisons + ["完整查阅后确认缺少必需报价明细：" + "、".join(missing)]), values
    if decision.confirmed_absent:
        raise LLMError("报价事实齐全但同时声明报价缺失，须重新核验")
    if criterion.get("_price_limit_rule_sources") and decision.verdict == "fail":
        # A generic ceiling clause can also contain a conditional item-price
        # requirement. Passing the numeric ceiling cannot override that finding.
        return "fail", "；".join(comparisons + ["条款其他条件的原页核验不符合，详见评审理由"]), values
    return "pass", "；".join(comparisons), values


def _assess_cell(criterion: dict, pages: list[dict], data_class: str, gateway_factory, *, reviewer=None) -> CellOutcome:
    from app.engine.auto_review import (is_external_requirement, is_price_limit_requirement,
                                        resolve_price_limit_requirement, review_criterion)
    from app.services.review_activity import report

    c = _route_criterion(criterion)
    if c.get("_external_fast") and is_external_requirement(c):
        reason = (
            "该横向条款依赖投标文件之外的事实，系统按外部事实边界结项；"
            "由评标委员会核验，不影响参评。"
        )
        return CellOutcome(
            "unverifiable", reason, None, [], pages, "external", 1,
            {"external_requirement": True, "coverage_complete": True},
            [{"verdict_source": "external", "message": reason}],
        )
    objective_score = c.get("score_type") == "count" or bool(c.get("_objective_route"))
    direct_subjective = (
        c.get("score_type") == "subjective"
        and not c.get("_objective_route")
        and config.subjective_direct_enabled()
    )
    if direct_subjective:
        c = {**c, "_subjective_direct": True}
    price_rule = resolve_price_limit_requirement(c)
    price_limit = is_price_limit_requirement(price_rule)
    non_scoring = c.get("score_type") in ("veto", "match")
    # Vision is reserved for eligibility/signature clauses.  Counting and
    # subjective scoring use text/OCR plus deterministic material gates;
    # forcing image batches made the model fail the visual-evidence contract
    # and skipped every count cell (run 87: 34 count cells).
    needs_visual = _is_visual_requirement(c) and not is_external_requirement(c) and non_scoring
    execution_attempt = uuid.uuid4().hex
    previous: dict | None = {"execution_attempt": execution_attempt}
    attempts: list[dict] = []
    current_pages = pages
    from app.services.execution_version import execution_config_value, continue_after_errors

    rounds = 0 if direct_subjective else int(execution_config_value("auto_review_rounds"))
    review_fn = reviewer or review_criterion
    for round_no in range(rounds + 1):
        preparation_step = False
        try:
            report(f"{'初评' if round_no == 0 else f'补评 {round_no}/{rounds}'}")
            visual = False
            requested_visual, requested_pages = _requested_visual_pages(previous, current_pages)
            supplement_rule = {**c, "_visual_page_keys": sorted(requested_pages)}
            if round_no == 1:
                report("补做 OCR")
                current_pages = _supplement_pages(c, current_pages, visual=False)
                textless_pages = {(int(p["document_id"]), int(p["page_no"]))
                                  for p in current_pages if p.get("ocr_textless_issues")}
                if textless_pages and not needs_visual:
                    supplement_rule["_visual_page_keys"] = sorted(requested_pages | textless_pages)
                if needs_visual or requested_visual or textless_pages:
                    visual = True
                    current_pages = _supplement_pages(supplement_rule, current_pages, visual=True)
            elif round_no >= 2:
                visual = requested_visual or needs_visual or any(
                    p.get("unreadable") or p.get("requires_vision") for p in current_pages)
                if visual:
                    current_pages = _supplement_pages(supplement_rule, current_pages, visual=True, exhaustive=needs_visual)
            # Signature/stamp presence requires original-page visual evidence.
            if round_no == 0 and needs_visual:
                preparation_step = True
                raise LLMError("签章或图像事实需要补读原页后由视觉模型核验")
            if (round_no == 0 and not direct_subjective
                    and any(p.get("unreadable") or p.get("requires_vision") for p in current_pages)):
                preparation_step = True
                raise LLMError("关联材料存在不可读页的读取故障，先补做 OCR 或视觉核验再评审")
            decision = review_fn(
                None if visual else gateway_factory(), c, current_pages, data_class=data_class,
                previous=previous, round_no=round_no, use_vision=visual,
            )
            if getattr(decision, "_review_pages", None) is not None:
                current_pages = decision._review_pages
            if decision.missing_evidence and not direct_subjective:
                raise LLMError("评审证据尚未解决：" + "；".join(decision.missing_evidence))
            is_visual = any(e.get("kind") == "visual" for e in decision.evidence)
            if needs_visual and not is_visual:
                raise LLMError("签章结论缺少原页视觉核验依据")
            basis = "visual" if is_visual else "evidence"
            if direct_subjective and not decision.evidence:
                basis = "model"
            if decision.verdict == "unverifiable":
                if direct_subjective:
                    decision.verdict = "pass" if (decision.score or 0) > 0 else "fail"
                elif not non_scoring or not is_external_requirement(c):
                    raise LLMError("文件内应评事实不能作为外部证据边界结项")
                elif not decision.coverage_complete:
                    raise LLMError("外部记录尚未完成全部关联材料的可读性核验，不能将读取故障标为无法证实")
                else:
                    basis = "external"
            elif not decision.evidence and not (decision.confirmed_absent and decision.coverage_complete):
                if not direct_subjective:
                    raise LLMError("评审结论缺少可验证的原文或原页证据")
            elif not decision.evidence and decision.confirmed_absent:
                basis = "absence"
            score = None
            detail = decision.reasoning
            if price_limit:
                verdict, formula, values = _verified_price_limit(price_rule, decision)
                decision.verdict = verdict
                detail += "；确定性核验：" + formula
                decision.reasoning = detail
            elif objective_score:
                _inject_deterministic_cert_backing(c, decision, current_pages)
                score, formula, values = _verified_count(c, decision, current_pages)
                detail += "；确定性计算：" + formula
            elif not non_scoring:
                score = _score_in_range(decision.score, c)
            raw = decision.model_dump(mode="json")
            if direct_subjective:
                # Direct subjective mode records unresolved reading requests as
                # caveats, not as cell-level blockers rejected by _valid_cell.
                raw["subjective_direct"] = True
                raw["missing_evidence"] = []
            raw["execution_attempt"] = execution_attempt
            raw["attempt_history"] = attempts + [{"round": round_no, "state": "completed"}]
            raw["readability_issues"] = [{"document_id": p["document_id"], "page_no": p["page_no"],
                "issues": p["ocr_textless_issues"], "image_sha256": p.get("image_sha256")}
                for p in current_pages if p.get("ocr_textless_issues")]
            if objective_score or price_limit:
                raw["verified_facts"] = values
                raw["calculation"] = formula
            if price_rule.get("_price_limit_rule_sources"):
                raw["price_limit_rule_sources"] = price_rule["_price_limit_rule_sources"]
            if decision.confirmed_absent and score == 0:
                raw["zero_reason"] = "confirmed_absent"
            warnings = [{"verdict_source": "llm", "message": detail}]
            if direct_subjective and decision.missing_evidence:
                warnings.append({
                    "message": "主观直评未解决的证据请求：" + "；".join(decision.missing_evidence),
                    "missing_evidence": list(decision.missing_evidence),
                    "provisional": True,
                })
            return CellOutcome(
                decision.verdict, detail, score, list(decision.evidence), current_pages,
                basis, round_no + 1, raw, warnings,
            )
        except JobCancelled:
            raise
        except (LLMNotConfigured, AppError) as exc:
            exc.attempt_history = attempts + [{"round": round_no, "state": "failed", "error": str(exc)}]
            raise
        except Exception as exc:
            from app.llm.request_sizing import ReviewContextExceeded
            from app.parsing.pdf_runtime import OCRProcessingError

            if isinstance(exc, (ReviewContextExceeded, OCRProcessingError)) or (isinstance(exc, LLMError) and (
                    exc.transient or exc.review_attempts_exhausted)):
                # Transport retries belong to the provider; an exhausted HTTP
                # or contract repair must not trigger a full OCR/visual reread.
                exc.attempt_history = attempts + [{"round": round_no, "state": "failed", "error": str(exc)}]
                raise
            previous = {"execution_attempt": execution_attempt, "error": str(exc), "round": round_no}
            attempts.append({**previous, "state": "failed"})
            if continue_after_errors() and not preparation_step:
                exc.attempt_history = attempts
                raise
            if round_no == rounds:
                error = LLMError(f"自动补评 {rounds} 轮后仍未完成：{exc}")
                error.attempt_history = attempts
                raise error from exc
    raise LLMError("自动评审未产生有效结论")


def _fallback_score_for(criterion: dict) -> Decimal | None:
    """Provisional score for a cell whose evidence/model path failed.

    Objective cells settle at 0 unless a deterministic fast path has already
    produced a value.  Subjective cells fall back to the lowest frozen level
    (or 0 when the rule has no levels).  Veto/match cells carry a conclusion,
    not a numeric score.
    """
    score_type = criterion.get("score_type")
    maximum = criterion.get("max_score")
    if score_type not in ("veto", "match") and maximum is not None:
        if score_type == "subjective":
            minimums: list[Decimal] = []
            for level in criterion.get("levels") or []:
                try:
                    if level.get("min") is not None:
                        minimums.append(Decimal(str(level["min"])))
                except (InvalidOperation, TypeError, ValueError):
                    continue
            if minimums:
                return max(Decimal(0), min(minimums))
        return Decimal(0)
    if score_type == "match" and maximum is not None:
        return Decimal(0)
    return None


def _write_closure_fallback(s, row: ScoreRecord, criterion: dict, error: BaseException) -> None:
    score_type = criterion.get("score_type")
    value = _fallback_score_for(criterion)
    detail = str(error).strip()[:500] or type(error).__name__
    reason = (
        f"自动评审未产生有效结论，按暂定 {value} 分兜底（非证据结论）：{detail}"
        if value is not None else
        f"自动评审未产生有效结论，按存疑结项（非证据结论）：{detail}"
    )
    kind = "min_level_floor" if score_type == "subjective" and value else (
        "provisional_zero" if value is not None else "provisional_conclusion"
    )
    fallback = {
        "kind": kind,
        "value": str(value) if value is not None else None,
        "basis": "machine_fallback",
        "reason": detail,
        "score_type": score_type,
    }
    row.final_score = value
    row.machine_score = value if score_type != "subjective" else None
    row.ai_suggested_score = value if score_type == "subjective" else None
    row.machine_status = "concern"
    row.review_status = "auto_confirmed"
    row.evidence_status = "none"
    row.ai_reasoning = reason
    row.evaluator_actor = "auto"
    row.confirm_time = utcnow()
    row.ai_raw_json = {
        "attempt_history": getattr(error, "attempt_history", []),
        "validation_history": list(getattr(error, "validation_history", [])),
        "auto_review": {
            "state": "completed",
            "basis": "fallback",
            "reasoning": reason,
            "fallback": fallback,
            "coverage_complete": False,
            "calculation": None,
            "error_code": getattr(error, "code", type(error).__name__),
            "error": detail,
        },
    }
    row.warnings_json = [{
        "message": reason,
        "fallback_score": fallback,
    }]
    s.add(ReviewRecord(
        subject_type="score", subject_id=str(row.id), action="confirm", actor="auto",
        reason=reason,
        new_value_json={"verdict": "concern", "score": str(value) if value is not None else None,
                        "auto_review": row.ai_raw_json["auto_review"]},
    ))


def _persist_cell(run_id: int, bidder_id: int, criterion: dict, outcome: CellOutcome | None,
                  error: BaseException | None = None) -> None:
    from app.evidence.verifier import verify_automatic_citation
    from app.services.automation_service import is_cell_completed

    if is_cell_completed(run_id, bidder_id, criterion["id"]):
        return
    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        row = s.execute(select(ScoreRecord).where(
            ScoreRecord.run_id == run_id, ScoreRecord.bidder_id == bidder_id,
            ScoreRecord.criterion_id == criterion["id"],
        )).scalar_one_or_none()
        if row is None:
            row = ScoreRecord(run_id=run_id, bidder_id=bidder_id, criterion_id=criterion["id"])
            s.add(row)
            s.flush()
        row.rule_version = run.package_version
        row.package_content_hash = run.package_content_hash
        row.max_score = criterion.get("max_score")
        row.score_type = criterion["score_type"]
        row.veto_flag = bool(criterion.get("is_veto"))
        s.execute(delete(EvidenceCitation).where(EvidenceCitation.score_record_id == row.id,
                                                EvidenceCitation.created_by != "human"))
        if error is not None:
            if config.score_closure_enabled():
                _write_closure_fallback(s, row, criterion, error)
            else:
                row.final_score = None
                row.machine_score = None
                row.machine_status = "execution_failed"
                row.review_status = "pending"
                row.evidence_status = "none"
                row.ai_raw_json = {"attempt_history": getattr(error, "attempt_history", []),
                    "validation_history": list(getattr(error, "validation_history", [])), "auto_review": {
                    "state": "failed", "error_code": getattr(error, "code", type(error).__name__),
                    "error": str(error)[:1500],
                }}
                row.warnings_json = [{"message": f"自动评审执行失败：{error}", "execution_failed": True}]
        else:
            page_map = {(p["document_id"], p["page_no"]): p for p in outcome.pages}
            visual_evidence = []
            verified_count = 0
            for item in outcome.evidence:
                key = (int(item["document_id"]), int(item["page_no"]))
                page = page_map.get(key)
                if page is None:
                    raise LLMError("证据引用了本次送审范围以外的页")
                if item.get("kind") == "visual":
                    if not page.get("image_url") or not item.get("observation"):
                        raise LLMError("视觉证据没有对应的原页图像或观察记录")
                    visual_evidence.append({
                        "document_id": key[0], "page_no": key[1],
                        "sha256": page.get("sha256"), "image_sha256": page.get("image_sha256"),
                        "observation": item["observation"],
                    })
                    continue
                cit = EvidenceCitation(
                    score_record_id=row.id, criterion_id=criterion["id"],
                    document_id=key[0], page_no=key[1], quote=str(item.get("quote") or ""),
                    citation_role="veto" if criterion["score_type"] == "veto" else "score",
                    created_by="llm", page_size_json=[page.get("width", 0), page.get("height", 0)],
                )
                if not verify_automatic_citation(cit, page["text"]):
                    if outcome.basis == "model":
                        # Direct subjective scoring keeps the numeric judgment
                        # and drops an unverifiable citation instead of failing
                        # the whole cell.
                        continue
                    raise LLMError("自动评审引文未通过逐页原文核验")
                s.add(cit)
                verified_count += 1
            metadata = {
                "state": "completed", "attempts": outcome.attempts, "basis": outcome.basis,
                "reasoning": outcome.reasoning, "external_requirement": outcome.basis == "external",
                "calculation": (outcome.raw.get("calculation") or outcome.raw.get("sub_score_calculation")
                                or outcome.raw.get("formula")),
                "visual_evidence": visual_evidence,
                "supplemented_pages": [{
                    "document_id": p["document_id"], "page_no": p["page_no"],
                    "source": p.get("source"), "text": p.get("text"),
                } for p in outcome.pages if p.get("supplemented")],
                "confirmed_absent": bool(outcome.raw.get("confirmed_absent")),
                "coverage_complete": bool(outcome.raw.get("coverage_complete")),
                "input_signature": hashlib.sha256(json.dumps(
                    [(p["document_id"], p["page_no"], p.get("sha256")) for p in outcome.pages],
                    sort_keys=True, default=str,
                ).encode()).hexdigest(),
                "lightweight": (outcome.raw.get("qualification_light")
                                if outcome.basis == "lightweight" else None),
                "fallback": (outcome.raw.get("fallback")
                             if outcome.basis == "fallback" else None),
            }
            row.ai_raw_json = {**outcome.raw, "auto_review": metadata}
            row.final_score = outcome.score
            row.machine_score = outcome.score if criterion["score_type"] != "subjective" else None
            row.ai_suggested_score = outcome.score if criterion["score_type"] == "subjective" else None
            row.ai_reasoning = outcome.reasoning
            row.machine_status = outcome.verdict
            row.review_status = "auto_confirmed"
            row.evidence_status = "verified" if verified_count or visual_evidence else "none"
            row.evaluator_actor = "auto"
            row.confirm_time = utcnow()
            row.warnings_json = outcome.warnings
            s.add(ReviewRecord(
                subject_type="score", subject_id=str(row.id), action="confirm", actor="auto",
                reason=outcome.reasoning,
                new_value_json={"verdict": outcome.verdict,
                                "score": str(outcome.score) if outcome.score is not None else None,
                                "auto_review": metadata},
            ))


def _resolve_prices(run_id: int, manifest: dict, pages_by_bidder: dict, gateway_factory) -> dict[int, Decimal]:
    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        resolved = dict((run.config_json or {}).get("resolved_prices") or {})
        provenance = dict((run.config_json or {}).get("resolved_price_evidence") or {})
    prices: dict[int, Decimal] = {}
    for bid in manifest["bidders"]:
        bid_id = int(bid["id"])
        if str(bid_id) in resolved and str(bid_id) in provenance:
            prices[bid_id] = _decimal(resolved[str(bid_id)], "已核验报价")
            continue
        original = bid.get("bid_price")
        if original is not None and bid.get("price_status") == "confirmed" and _decimal(original, "报价") > 0:
            prices[bid_id] = _decimal(original, "冻结确认报价")
            source = {"basis": "frozen_confirmed", "reasoning": "采用运行启动时已确认并冻结的投标总价",
                      "evidence": [], "price": str(prices[bid_id])}
        else:
            pages, data_class = pages_by_bidder[bid_id]
            price_rule = {
                "name": "投标总报价抽取", "score_type": "match", "check_type": "human_only",
                "_extract_price": True,
                "description": (
                    "从开标一览表或正式报价函提取评标口径总价（人民币元），不得误取保证金、年度价、分项价。"
                    "facts.bid_price 必须有逐页证据。大小写金额或总价与分项冲突时，按所附冻结细则核验并说明"
                    "采用依据；没有明确口径时列出 missing_evidence，禁止任选其一。"
                ),
                "price_rules": [c for c in manifest["criteria"] if c.get("score_type") == "formula"
                                or "报价" in str(c.get("name") or "")],
            }
            result = _assess_cell(price_rule, pages, data_class, gateway_factory)
            quote = (result.raw.get("facts") or {}).get("bid_price")
            if quote is None:
                raise LLMError(f"投标人 {bid_id} 的总报价经自动补抽仍不可得，未删除该投标人")
            prices[bid_id] = _decimal(quote, "投标总报价")
            if prices[bid_id] <= 0:
                raise LLMError(f"投标人 {bid_id} 的投标总报价无效")
            source = {"basis": result.basis, "reasoning": result.reasoning,
                      "evidence": result.evidence, "facts": result.raw.get("facts"),
                      "attempt_history": result.raw.get("attempt_history"), "price": str(prices[bid_id]),
                      "supplemented_pages": [{k: p.get(k) for k in
                                              ("document_id", "page_no", "text", "source")}
                                             for p in result.pages if p.get("supplemented")]}
        resolved[str(bid_id)] = str(prices[bid_id])
        provenance[str(bid_id)] = source
        with session_scope() as s:
            run = s.get(EvaluationRun, run_id)
            run.config_json = {**(run.config_json or {}), "resolved_prices": dict(resolved),
                               "resolved_price_evidence": dict(provenance)}
            s.add(ReviewRecord(
                subject_type="price", subject_id=str(bid_id), action="confirm", actor="auto",
                reason=source["reasoning"],
                new_value_json={"run_id": run_id, "price": str(prices[bid_id]), "source": source},
            ))
    return prices


def _price_stage(run_id: int, manifest: dict, criteria: list[dict], prices: dict[int, Decimal],
                 completed: set[tuple[int, int]], *, only_criterion_id: int | None = None,
                 allow_partial_gate: bool = False) -> None:
    from app.engine.price import compute_price_scores

    for criterion in [c for c in criteria if c["score_type"] == "formula"
                      and (only_criterion_id is None or c["id"] == only_criterion_id)]:
        if all((int(b["id"]), criterion["id"]) in completed for b in manifest["bidders"]):
            continue
        formula = {**(criterion.get("formula") or {}), "max_score": criterion.get("max_score")}
        gate = (formula.get("params_json") or {}).get("benchmark_gate")
        bids = []
        gate_missing_by_bidder: dict[int, list[str]] = {}
        with session_scope() as s:
            for bid in manifest["bidders"]:
                bid_id = int(bid["id"])
                value = basis = None
                sample_pass = None
                if gate and (gate.get("min_valid_bidders") is None
                             or len(manifest["bidders"]) >= int(gate["min_valid_bidders"])):
                    on = gate.get("gate_on") or "tech"
                    categories = {"tech": {"technical"}, "tech_qualification": {"technical", "commercial", "qualification"},
                                  "biz_tech": {"technical", "commercial"},
                                  "sample_or_composite": {"technical", "commercial"}}.get(on)
                    if categories is None:
                        raise ValidationFailed(f"未支持的基准价门槛口径：{on}")
                    needed = [c for c in criteria if c.get("category") in categories
                              and c.get("max_score") is not None and c["score_type"] not in ("veto", "match")
                              and (c.get("params") or {}).get("in_total") is not False]
                    values = []
                    missing_gate: list[str] = []
                    for c in needed:
                        record = s.execute(select(ScoreRecord).where(
                            ScoreRecord.run_id == run_id, ScoreRecord.bidder_id == bid_id,
                            ScoreRecord.criterion_id == c["id"],
                        )).scalar_one_or_none()
                        if record is None or (bid_id, c["id"]) not in completed:
                            if allow_partial_gate:
                                missing_gate.append(str(c.get("code") or c["id"]))
                                continue
                            raise LLMError("价格分所依赖的上游评分尚未完成")
                        values.append(_decimal(record.final_score, "门槛得分"))
                    if needed:
                        value = sum(values, Decimal(0))
                        basis = sum((_decimal(c["max_score"], "门槛满分") for c in needed), Decimal(0))
                        if missing_gate:
                            # Unknown upstream scores must not exclude the bidder in a
                            # partial run; include it and mark the price provisional.
                            value = basis
                            gate_missing_by_bidder[bid_id] = missing_gate
                    if on == "sample_or_composite":
                        sample_criteria = [c for c in criteria if "样品" in str(c.get("name") or "")
                                           and c["score_type"] not in ("veto", "match")]
                        if not sample_criteria:
                            raise LLMError("冻结价格门槛要求样品结论，但没有可对应的样品评审条款")
                        sample_results = []
                        for c in sample_criteria:
                            record = s.execute(select(ScoreRecord).where(
                                ScoreRecord.run_id == run_id, ScoreRecord.bidder_id == bid_id,
                                ScoreRecord.criterion_id == c["id"],
                            )).scalar_one_or_none()
                            if record is None or (bid_id, c["id"]) not in completed:
                                if allow_partial_gate:
                                    gate_missing_by_bidder.setdefault(bid_id, []).append(
                                        str(c.get("code") or c["id"]))
                                    continue
                                raise LLMError("价格门槛所需的样品评审尚未完成")
                            sample_results.append(_decimal(record.final_score, "样品得分"))
                        sample_pass = (sum(sample_results, Decimal(0)) > 0
                                       if sample_results else True)
                bids.append(PriceBid(bid_id, str(bid.get("name") or bid_id), prices[bid_id],
                                     False, gate_value=value, gate_basis=basis, sample_pass=sample_pass))
            price_evidence = dict((s.get(EvaluationRun, run_id).config_json or {}).get("resolved_price_evidence") or {})
        strict_error: str | None = None
        try:
            try:
                computation = compute_price_scores(bids, formula, strict=True)
            except ValueError as exc:
                if not allow_partial_gate:
                    raise
                # A strict 70% gate can leave too few bidders to form the
                # trimmed benchmark (run 98: six bidders below 70%, one left,
                # trim step impossible).  Do not turn the whole price item into
                # zero: compute the pre-gate benchmark and mark it provisional.
                strict_error = str(exc)
                lenient_params = dict((formula.get("params_json") or {}))
                lenient_params.pop("benchmark_gate", None)
                computation = compute_price_scores(
                    bids, {**formula, "params_json": lenient_params}, strict=False)
            # Compute all bidders against the same complete frozen quote set before writes.
            for result in computation.results:
                source = price_evidence.get(str(result.bidder_id)) or {}
                pages, _ = _load_pages(result.bidder_id)
                for extra in source.get("supplemented_pages") or []:
                    for page in pages:
                        if (page["document_id"], page["page_no"]) == (extra["document_id"], extra["page_no"]):
                            page.update(extra)
                            page["supplemented"] = True
                visual_keys = {(e["document_id"], e["page_no"]) for e in source.get("evidence", [])
                               if e.get("kind") == "visual"}
                if visual_keys:
                    for page in pages:
                        if (page["document_id"], page["page_no"]) in visual_keys:
                            page["unreadable"] = True
                    pages = _supplement_pages(criterion, pages, visual=True)
                missing_gate = gate_missing_by_bidder.get(result.bidder_id) or []
                provisional = bool(missing_gate) or strict_error is not None
                reason = result.explanation
                if missing_gate:
                    reason += (f"；暂定价格：门槛所需评分尚未完成（{', '.join(missing_gate)}），"
                               "按未剔除处理")
                if strict_error:
                    reason += f"；严格门槛无法形成基准价（{strict_error}），按门槛前口径暂定计算"
                _persist_cell(run_id, result.bidder_id, criterion, CellOutcome(
                    "pass", reason,
                    result.price_score, source.get("evidence") or [], pages,
                    raw={"formula": formula, "benchmark": str(computation.benchmark),
                         "excluded_from_benchmark": result.excluded_from_benchmark,
                         "benchmark_inclusion_reason": result.explanation,
                         "price_provisional": provisional,
                         "price_strict_error": strict_error,
                         "gate_missing_criteria": missing_gate,
                         "price_source": source,
                         "benchmark_inputs": [{"bidder_id": b.bidder_id, "price": str(b.price),
                                               "gate_value": str(b.gate_value) if b.gate_value is not None else None,
                                               "gate_basis": str(b.gate_basis) if b.gate_basis is not None else None,
                                               "sample_pass": b.sample_pass} for b in bids]},
                    warnings=[{"formula_explanation": result.explanation,
                               "price_provisional": provisional,
                               "price_strict_error": strict_error,
                               "gate_missing_criteria": missing_gate}],
                ))
                completed.add((result.bidder_id, criterion["id"]))
        except Exception as exc:
            for bid in manifest["bidders"]:
                _persist_cell(run_id, int(bid["id"]), criterion, None, exc)
            raise


def execute_automatic_run(run_id: int, *, ctx=None) -> dict:
    """Execute a managed run. Completed cells survive model outages and restarts."""
    from app.services.run_execution_lock import exclusive_run
    from app.services.execution_version import bind_run_execution, run_executor_version
    from app.services.review_collections import recover_interrupted_batches

    with exclusive_run(run_id):
        with session_scope() as s:
            run = s.get(EvaluationRun, run_id)
            if run is None:
                raise ValidationFailed("评分运行不存在")
            manifest = (run.config_json or {}).get("automation_manifest")
            version = run_executor_version(run)
            from app.services.automation_service import failure_policy
            policy = failure_policy(run)
        if not manifest:
            raise ValidationFailed("运行缺少冻结输入清单，请新建评标运行")
        # This generation is allocated only after taking the exclusive OS lock.
        # No live executor can still own an interrupted attempt from an older one.
        generation = time.time_ns()
        recover_interrupted_batches(run_id, generation)
        with bind_run_execution(manifest, version, failure_policy=policy):
            return _execute_locked(run_id, ctx=ctx, generation=generation, executor_version=version)


def _run_summary(run_id: int, manifest: dict, automation: dict) -> dict:
    criteria = manifest["criteria"]
    count = len(manifest["bidders"])
    return {"run_id": run_id, "need_human": 0, "automation": automation,
            "match": sum(c["score_type"] == "match" for c in criteria) * count,
            "objective": sum(c["score_type"] in ("veto", "count", "match") for c in criteria) * count,
            "subjective": sum(c["score_type"] == "subjective" for c in criteria) * count,
            "price": sum(c["score_type"] == "formula" for c in criteria) * count}


def _execute_locked(run_id: int, *, ctx=None, generation=None, executor_version=1) -> dict:
    from app.services import automation_service
    from app.services.execution_version import bind_run_execution, execution_config_value
    from app.services.review_collections import review_runtime, run_activity_observer
    from app.llm.request_control import request_scope
    from app.engine.shared_review import SharedReviewCollector
    from app.parsing.pdf_runtime import OCRProcessingError

    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        manifest = (run.config_json or {}).get("automation_manifest")
        if not manifest:
            raise ValidationFailed("运行缺少冻结输入清单，请新建评标运行")
        policy = automation_service.failure_policy(run)
        continue_errors = policy == "continue"
        qual_mode = automation_service.qualification_mode(run)
        count_mode = automation_service.count_deterministic_mode(run)
        if run.status == "partial" and continue_errors:
            state = automation_service.progress(run_id, session=s)
            if state and state["pending"] == 0:
                return _run_summary(run_id, manifest, state)
            raise ValidationFailed("已结束运行的处理记录不完整，请新建评分运行")
        if run.status == "succeeded":
            # A delayed duplicate job must leave the published frozen result
            # untouched, including when newer project inputs already exist.
            state = automation_service.progress(run_id, session=s)
            if state and state["is_final"]:
                return _run_summary(run_id, manifest, state)
            raise ValidationFailed("成功运行的完成记录无效，请新建评分运行")
    automation_service.initialize_run(run_id, manifest["criteria"],
                                      provider=manifest.get("provider"), model=manifest.get("model"))
    with session_scope() as s:
        run = s.get(EvaluationRun, run_id)
        run.status = "running"
        run.started_at = run.started_at or utcnow()
        run.finished_at = None
    criteria = [dict(c) for c in manifest["criteria"]]
    bidder_ids = [int(b["id"]) for b in manifest["bidders"]]
    completed = automation_service.completed_keys(run_id)
    skipped = automation_service.failed_keys(run_id) if continue_errors else set()
    reported_items = len(completed) + len(skipped)
    if ctx:
        ctx.start(total=len(criteria) * len(bidder_ids) + 1, message="自动评审准备")
        if completed or skipped:
            ctx.advance(n=len(completed) + len(skipped),
                        message=f"已恢复 {len(completed)} 项完成记录，跳过 {len(skipped)} 项已记录故障")
    gateway = None
    gateway_lock = threading.Lock()
    shared_collector = SharedReviewCollector() if executor_version == 2 else None
    observe_requests = run_activity_observer(run_id, generation)

    def gateway_factory():
        nonlocal gateway
        with gateway_lock:
            if gateway is None:
                from app.llm.gateway import Gateway
                from app.llm.registry import get_provider

                gateway = Gateway(get_provider(manifest.get("provider"), manifest.get("model")))
            return gateway

    pages_by_bidder: dict[int, tuple[list[dict], str]] = {}
    preparation_errors: dict[int, BaseException] = {}
    for bid_id in bidder_ids:
        try:
            pages_by_bidder[bid_id] = _load_pages(bid_id)
        except Exception as exc:
            preparation_errors[bid_id] = exc
    inventory_by_bidder: dict[int, Any] = {}
    inventory_locks = {bid: threading.Lock() for bid in bidder_ids}

    def get_inventory(bid_id):
        from app.engine.material_inventory import build_inventory

        with inventory_locks[bid_id]:
            inventory = inventory_by_bidder.get(bid_id)
            if inventory is None:
                loaded = pages_by_bidder.get(bid_id)
                if loaded is None:
                    return None
                inventory = build_inventory(bid_id, loaded[0])
                inventory_by_bidder[bid_id] = inventory
            return inventory

    def fastpath_cell(bid_id, criterion):
        # V2 is the shared-collection executor; its integration contracts
        # require every cell to go through the collector.  The deterministic
        # fast path is a V1 execution optimization.
        if executor_version >= 2:
            return None
        if count_mode not in ("on", "shadow"):
            return None
        score_type = criterion.get("score_type")
        if score_type not in ("count", "subjective", "match"):
            return None
        pages = pages_by_bidder.get(bid_id, ([], ""))[0]
        if _is_duplicate_summary(criterion, criteria):
            reason = "汇总条目：该判断项的分值已在对应计分项（同章节子项）计算，不重复计分"
            return CellOutcome(
                "not_deeply_reviewed", reason, None, [], pages, "fallback", 1,
                {"fallback": {"kind": "duplicate_summary", "value": None,
                              "basis": "duplicate_summary", "reason": reason}},
                [{"message": reason, "verdict_source": "deterministic"}],
            )
        if score_type == "match":
            return None
        try:
            from app.engine.objective_fastpath import build_objective_plan, score_objective
            if build_objective_plan(criterion) is None:
                return None
            inventory = get_inventory(bid_id)
            if inventory is None:
                return None
            result = score_objective(criterion, inventory)
            if result is None:
                return None
            if count_mode == "shadow":
                log.info("客观快路径影子 bid=%s criterion=%s score=%s detail=%s",
                         bid_id, criterion.get("code"), result.score, result.detail[:200])
                return None
            # Once a deterministic plan exists, always settle from the
            # inventory.  Falling back to the full-document LLM count path here
            # re-reads every page in dozens of batches and was the dominant
            # wall-clock cost of runs 93/94.  Incomplete inventory becomes an
            # explicit 0/concern with a reason, never a blank cell.

            raw = {
                "calculation": result.detail,
                "verified_facts": result.facts,
                "coverage_complete": result.complete,
                "zero_reason": result.zero_reason,
                "deterministic_plan": True,
            }
            return CellOutcome(
                result.verdict, result.detail, result.score, [], pages, "deterministic", 1,
                raw, [{"message": result.detail, "verdict_source": "deterministic"}],
            )
        except JobCancelled:
            raise
        except Exception:  # noqa: BLE001 - a parse failure must fall back to LLM
            log.exception("客观快路径失败 bid=%s criterion=%s", bid_id, criterion.get("code"))
            return None
    stages = manifest.get("stages") or ["objective", "price", "subjective"]
    cells = [(bid_id, c) for c in criteria for bid_id in bidder_ids
             if c["score_type"] != "formula" and (bid_id, c["id"]) not in completed
             and (bid_id, c["id"]) not in skipped
             and ((c["score_type"] == "subjective" and "subjective" in stages)
                  or (c["score_type"] != "subjective" and "objective" in stages))]
    routed_groups: dict[int, int] = {}
    if qual_mode == "light":
        # Objective scoring first, then subjective, qualification last.  Existing
        # runs keep their frozen submission order.
        for c in criteria:
            try:
                routed = _route_criterion(c)
            except Exception:  # noqa: BLE001
                routed = c
            routed_groups[int(c["id"])] = (
                0 if (routed.get("score_type") == "count" or routed.get("_objective_route"))
                else 1 if routed.get("score_type") == "subjective" else 2
            )
        cells.sort(key=lambda pair: routed_groups.get(int(pair[1]["id"]), 3))
    stop = threading.Event()
    user_cancelled = threading.Event()
    activity = SimpleQueue()
    preparation_locks = {bid_id: threading.Lock() for bid_id in bidder_ids}
    prepared: set[int] = set()
    bidder_names = {int(b["id"]): b["name"] for b in manifest["bidders"]}
    frozen_bidders = {int(b["id"]): dict(b) for b in manifest["bidders"]}

    # Qualification-light: one deterministic precheck + one consolidated LLM
    # screen per bidder.  Never OCR, never create collection batches.
    light_criteria_by_bidder: dict[int, list[dict]] = {}
    light_reviews: dict[int, dict] = {}
    light_locks: dict[int, threading.Lock] = {}
    if qual_mode == "light":
        from app.engine.auto_review import is_cross_bidder_requirement as _is_cross

        for bid_id in bidder_ids:
            light_criteria_by_bidder[bid_id] = [
                c for c in criteria
                if c["score_type"] in ("veto", "match") and not _is_cross(c)
            ]
            light_locks[bid_id] = threading.Lock()

    def light_cell_outcome(criterion, result, pages):
        from app.engine.qualification_light import LightResult, cell_payload

        if result is None:
            result = LightResult(status="not_deeply_reviewed", reason="轻量预检未覆盖该条款")
        payload = cell_payload(result)
        return CellOutcome(
            payload["status"], payload["reason"], None, [], pages,
            "lightweight", 1, {"qualification_light": payload},
            [{"message": payload["reason"], "verdict_source": "lightweight"}],
        )

    def merge_prepared_pages(bid_id, updated_pages):
        """Persist OCR/vision page text back so the next cell does not re-read it."""
        if not updated_pages:
            return
        with preparation_locks[bid_id]:
            current, data_class = pages_by_bidder[bid_id]
            by_key = {
                (int(p["document_id"]), int(p["page_no"])): p
                for p in updated_pages
                if p.get("supplemented") or p.get("ocr_textless_issues")
            }
            if not by_key:
                return
            pages_by_bidder[bid_id] = (
                [by_key.get((int(p["document_id"]), int(p["page_no"])), p) for p in current],
                data_class,
            )

    def prepare_bidder(bid_id):
        from app.services.review_activity import report

        # Never hold two bidder locks: concurrent horizontal reviews share the
        # same OCR preparation without acquiring locks in opposite orders.
        with preparation_locks[bid_id]:
            if bid_id in preparation_errors:
                raise preparation_errors[bid_id]
            pages, data_class = pages_by_bidder[bid_id]
            if bid_id not in prepared:
                prep_rounds = 0 if continue_errors else int(execution_config_value("auto_review_rounds"))
                for attempt in range(prep_rounds + 1):
                    try:
                        if any(p.get("unreadable") or p.get("requires_vision") for p in pages):
                            report(f"读取 {bidder_names[bid_id]} 关联材料，OCR 尝试 {attempt + 1}")
                            pages = _supplement_pages({}, pages, visual=False)
                        break
                    except JobCancelled:
                        raise
                    except Exception as exc:
                        if isinstance(exc, (AppError, OCRProcessingError)) or (
                                isinstance(exc, LLMError) and exc.transient
                        ) or attempt == prep_rounds:
                            preparation_errors[bid_id] = exc
                            raise
                pages_by_bidder[bid_id] = pages, data_class
                prepared.add(bid_id)
            return pages, data_class

    def worker(bid_id, criterion):
        from app.engine.auto_review import is_cross_bidder_requirement, is_external_requirement
        from app.services.review_activity import reporting, report

        if stop.is_set():
            return None

        def notify(message):
            if user_cancelled.is_set():
                raise JobCancelled()
            label = {0: "[客观分] ", 1: "[主观分] ", 2: "[资格/符合性] "}.get(
                routed_groups.get(int(criterion["id"])))
            activity.put((label or "") + f"{bidder_names[bid_id]} · {criterion['name']} · {message}")

        # Regular executor threads do not inherit ContextVars. Bind the immutable
        # execution identity before preparation, OCR, gateways or collection work.
        with bind_run_execution(manifest, executor_version, failure_policy=policy), \
                review_runtime(run_id, bid_id, criterion["id"], generation, manifest), \
                request_scope(cancel_check=lambda: notify("检查自动评审执行状态"),
                              on_event=observe_requests,
                              metadata={"run_id": run_id, "bidder_id": bid_id,
                                        "criterion_id": criterion["id"]}), \
                reporting(notify):
            fastpath = fastpath_cell(bid_id, criterion)
            if fastpath is not None:
                notify("确定性计分完成")
                return fastpath
            if (executor_version == 1
                    and criterion["score_type"] in ("veto", "match")
                    and is_external_requirement(criterion)
                    and not is_cross_bidder_requirement(criterion)):
                # External facts (for example "other bidders' prices" or a cost
                # explanation) cannot be established from the bid file.  Settle
                # them on the external boundary without OCR/LLM; the committee
                # verifies them and the bid is not blocked.
                reason = (
                    "该条款依赖投标文件之外的事实，系统按外部事实边界结项；"
                    "由评标委员会核验，不影响参评。"
                )
                pages = pages_by_bidder.get(bid_id, ([], ""))[0]
                return CellOutcome(
                    "unverifiable", reason, None, [], pages, "external", 1,
                    {"external_requirement": True, "coverage_complete": True},
                    [{"verdict_source": "external", "message": reason}],
                )
            if (executor_version == 1 and config.subjective_direct_enabled()
                    and criterion["score_type"] == "subjective"
                    and not criterion.get("_objective_route")):
                # Direct subjective scoring uses the already-parsed text pages.
                # It must not enter prepare_bidder/OCR: large scanned bids were
                # spending the whole run in remote/local OCR fallback (run 96).
                if bid_id in preparation_errors:
                    raise preparation_errors[bid_id]
                loaded = pages_by_bidder.get(bid_id)
                if loaded is None:
                    raise LLMError("投标文件页尚未加载")
                pages, data_class = loaded
                review_rule = {**criterion, "_frozen_bidder": frozen_bidders[bid_id]}
                outcome = _assess_cell(review_rule, pages, data_class, gateway_factory)
                merge_prepared_pages(bid_id, outcome.pages)
                return outcome
            if (qual_mode == "light" and criterion["score_type"] in ("veto", "match")
                    and criterion.get("max_score") is None
                    and not criterion.get("_extract_price")
                    and not is_cross_bidder_requirement(criterion)):
                if bid_id in preparation_errors:
                    raise preparation_errors[bid_id]
                pages, data_class = pages_by_bidder[bid_id]
                from app.engine.qualification_light import review_bidder_qualification
                from app.engine.qualification_light import LightResult

                with light_locks[bid_id]:
                    cached = light_reviews.get(bid_id)
                    if cached is None:
                        report("资格轻量预检 + 单次快审")
                        try:
                            gateway_obj = gateway_factory()
                        except Exception:  # noqa: BLE001 - unconfigured LLM degrades, never fails
                            gateway_obj = None
                        try:
                            cached = review_bidder_qualification(
                                light_criteria_by_bidder.get(bid_id, []), pages, data_class, gateway_obj)
                        except Exception as exc:  # noqa: BLE001
                            cached = {str(criterion.get("code") or ""): LightResult(
                                status="not_deeply_reviewed",
                                reason=f"轻量预检异常（{type(exc).__name__}），未深评")}
                        light_reviews[bid_id] = cached
                return light_cell_outcome(criterion, cached.get(str(criterion.get("code") or "")), pages)
            if executor_version == 1 and is_external_requirement(criterion):
                # External facts do not depend on OCR; using the parsed page
                # set directly avoids scanning large bids for an unobtainable
                # in-document fact (run 103 c42 stall).
                loaded = pages_by_bidder.get(bid_id)
                if loaded is None:
                    raise preparation_errors.get(bid_id, LLMError("投标文件页尚未加载"))
                pages, data_class = loaded
            else:
                pages, data_class = prepare_bidder(bid_id)
            review_rule = {**criterion, "_frozen_bidder": frozen_bidders[bid_id]}
            if is_cross_bidder_requirement(criterion):
                report("汇集全部所选投标人的关联材料进行横向核验")
                pages = []
                external_cross = executor_version == 1 and is_external_requirement(criterion)
                for peer_id in bidder_ids:
                    if external_cross:
                        # External cross-bidder facts still need every bidder's
                        # pages for ownership/coverage, but must not trigger
                        # OCR on large scanned bids.
                        loaded = pages_by_bidder.get(peer_id)
                        if loaded is None:
                            raise preparation_errors.get(
                                peer_id, LLMError("投标文件页尚未加载"))
                        peer_pages, peer_class = loaded
                    else:
                        peer_pages, peer_class = prepare_bidder(peer_id)
                    pages.extend({**p, "bidder_id": peer_id, "bidder_name": bidder_names[peer_id]} for p in peer_pages)
                    if peer_class == "confidential" or (peer_class == "redacted_sample" and data_class != "confidential"):
                        data_class = peer_class
                review_rule.update(
                    _frozen_bidders=list(frozen_bidders.values()),
                    _comparison_documents=[{"document_id": d["id"], "bidder_id": d["bidder_id"], "page_count": d["page_count"]}
                                           for d in manifest["documents"] if d.get("bidder_id") in frozen_bidders],
                )
                if external_cross:
                    review_rule["_external_fast"] = True
            if shared_collector is not None and is_cross_bidder_requirement(criterion):
                outcome = _assess_cell(review_rule, pages, data_class, gateway_factory,
                                       reviewer=shared_collector.review_criterion)
            else:
                outcome = _assess_cell(review_rule, pages, data_class, gateway_factory)
            merge_prepared_pages(bid_id, outcome.pages)
            return outcome

    errors: list[BaseException] = []
    workers = min(8, len(cells) or 1, max(1, config.llm_max_concurrency()))
    cancelled = False
    cell_iter = iter(cells)
    last_report = 0.0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}

        def schedule():
            if stop.is_set():
                return
            cell = next(cell_iter, None)
            if cell is not None:
                futures[pool.submit(worker, *cell)] = cell

        for _ in range(workers):
            schedule()
        while futures:
            ready, _ = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
            if ctx and not cancelled:
                try:
                    ctx.check_cancelled()
                    message = None
                    while True:
                        try:
                            message = activity.get_nowait()
                        except Empty:
                            break
                    if message and time.monotonic() - last_report >= 1 and hasattr(ctx, "report"):
                        ctx.report(f"已完成 {len(completed)}/{len(criteria) * len(bidder_ids)} 项 · {message}")
                        last_report = time.monotonic()
                except JobCancelled:
                    cancelled = True
                    user_cancelled.set()
                    stop.set()
            for future in ready:
                bid_id, criterion = futures.pop(future)
                success = False
                try:
                    outcome = future.result()
                    if outcome is not None:
                        _persist_cell(run_id, bid_id, criterion, outcome)
                        completed.add((bid_id, criterion["id"]))
                        success = True
                except JobCancelled:
                    cancelled = True
                    user_cancelled.set()
                    stop.set()
                except Exception as exc:
                    _persist_cell(run_id, bid_id, criterion, None, exc)
                    errors.append(exc)
                    if continue_errors:
                        skipped.add((bid_id, criterion["id"]))
                    else:
                        stop.set()
                if ctx and not cancelled:
                    try:
                        step = int(success or (continue_errors and (bid_id, criterion["id"]) in skipped))
                        ctx.advance(n=step, message=(
                            f"自动评审已完成 {len(completed)}/{len(criteria) * len(bidder_ids)} 项"
                            + (f" · 故障已跳过 {len(skipped)} 项，继续后续评审" if continue_errors and skipped
                               else f" · 技术故障 {len(errors)} 项，正在保存已开始的评审" if errors else "")))
                        reported_items += step
                    except JobCancelled:
                        cancelled = True
                        user_cancelled.set()
                        stop.set()
                schedule()
    if cancelled:
        raise JobCancelled()
    # Closure pass: every expected cell must have a settled record before the
    # price gate reads upstream scores.  Cells that never produced an outcome
    # (worker death, skipped item, price-stage exception) get an explicit
    # provisional record instead of remaining NULL.
    if config.score_closure_enabled():
        settled = automation_service.completed_keys(run_id)
        pending_cells = [
            (bid_id, c) for c in criteria if c["score_type"] != "formula"
            for bid_id in bidder_ids if (bid_id, c["id"]) not in settled
        ]
        if pending_cells:
            for bid_id, c in pending_cells:
                try:
                    _persist_cell(run_id, bid_id, c, None,
                                  LLMError("评审未产生有效结论，按暂定分兜底"))
                except JobCancelled:
                    raise
                except Exception:  # noqa: BLE001 - one bad row must not break closure
                    log.exception("暂定兜底落库失败 bid=%s criterion=%s", bid_id, c.get("code"))
            completed = automation_service.completed_keys(run_id)
            if ctx and not cancelled:
                try:
                    ctx.advance(
                        n=len(pending_cells),
                        message=f"已按暂定分兜底 {len(pending_cells)} 项，继续价格评分",
                    )
                except JobCancelled:
                    raise
    if (continue_errors or not errors) and "price" in stages and any(
        c["score_type"] == "formula" and any((b, c["id"]) not in completed for b in bidder_ids)
        for c in criteria
    ):
        try:
            with request_scope(on_event=observe_requests,
                               cancel_check=(ctx.check_cancelled if ctx else None),
                               metadata={"run_id": run_id, "phase": "price"}):
                prices = _resolve_prices(run_id, manifest, pages_by_bidder, gateway_factory)
            for price_rule in [c for c in criteria if c["score_type"] == "formula"]:
                if continue_errors and all((b, price_rule["id"]) in completed | skipped for b in bidder_ids):
                    continue
                try:
                    _price_stage(run_id, manifest, criteria, prices, completed,
                                 only_criterion_id=price_rule["id"],
                                 allow_partial_gate=continue_errors)
                except JobCancelled:
                    raise
                except Exception as exc:
                    if not continue_errors:
                        raise
                    errors.append(exc)
                    for bid_id in bidder_ids:
                        _persist_cell(run_id, bid_id, price_rule, None, exc)
        except JobCancelled:
            raise
        except Exception as exc:
            errors.append(exc)
            for c in criteria:
                if c["score_type"] == "formula":
                    for bid_id in bidder_ids:
                        _persist_cell(run_id, bid_id, c, None, exc)
    if errors and not continue_errors:
        with session_scope() as s:
            run = s.get(EvaluationRun, run_id)
            run.status = "failed"
            run.finished_at = utcnow()
            run.config_json = {**(run.config_json or {}), "error": str(errors[0])[:1500]}
        raise LLMError(f"自动评审未完成，已保留 {len(completed)} 项结果：{errors[0]}") from errors[0]
    automation = automation_service.finish_run(run_id)
    if ctx:
        ctx.advance(n=automation["processed"] - reported_items + 1,
                    message=(f"评审已结束：已评分 {automation['completed']} 项，跳过 {automation['skipped']} 项；结果可导出"
                             if automation["skipped"] else "全部应评项已完成并通过完整性校验"))
    with session_scope() as s:
        for bid in s.execute(select(Bidder).where(Bidder.id.in_(bidder_ids))).scalars():
            if automation["is_final"]:
                bid.status = "scored"
    return _run_summary(run_id, manifest, automation)
