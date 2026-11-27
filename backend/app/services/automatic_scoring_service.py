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

from pydantic import BaseModel
from sqlalchemy import delete, func, select

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


class _MaterialReviewPending(LLMError):
    def __init__(self, detail: str, *, subtotal: Decimal | None = None,
                 facts: dict | None = None, missing: list[str] | None = None):
        super().__init__(detail)
        self.subtotal = subtotal
        self.facts = facts or {}
        self.missing = missing or []


def _material_review_pending(criterion: dict, pages: list[dict], detail: str, *,
                             subtotal: Decimal | None = None, facts: dict | None = None,
                             missing: list[str] | None = None, evidence: list[dict] | None = None,
                             attempts: list[dict] | None = None) -> CellOutcome:
    facts = facts or {}
    attempts = attempts or []
    evidence = evidence or []
    records = facts.get("unverified_records") or facts.get("verified_cases") or []
    # 已核实小计 > 0 时，先按已核实材料直接计分；未核验案例只留警示，
    # 不让额外待核验案例把已有正向证据整体清零或阻塞排名。
    if subtotal is not None and Decimal(str(subtotal)) > 0:
        partial = {
            "reason": detail,
            "value": str(subtotal),
            "value_role": "verified_subtotal",
            "required_materials": list(dict.fromkeys([*(missing or []), *parse_required_artifacts(
                "\n".join(str(criterion.get(key) or "") for key in
                           ("required_evidence", "description", "source_quote"))
            )])),
            "candidate_pages": [
                {"document_id": p.get("document_id"), "page_no": p.get("page_no")}
                for p in pages
            ],
            "unverified_records": records,
        }
        return CellOutcome(
            "pass", f"{detail}；未核验案例暂不计入，按已核实小计 {subtotal} 分直接计分",
            Decimal(str(subtotal)), evidence, pages, "deterministic", len(attempts) or 1,
            {
                "auto_review": {
                    "state": "completed",
                    "basis": "deterministic",
                    "reasoning": detail,
                    "calculation": f"按已核实小计 {subtotal} 分直接计分",
                    "partial_material_review": partial,
                    "coverage_complete": False,
                },
                "verified_facts": facts,
                "attempt_history": attempts,
            },
            [{
                "message": f"{detail}；已核实小计 {subtotal} 分直接计分，未核验案例暂不计入",
                "partial_material_review": partial,
            }],
        )
    named_pages = list(evidence)
    for record in records:
        if isinstance(record, dict):
            named_pages.extend(item for item in record.get("evidence") or [] if isinstance(item, dict))
    source_keys = {(p.get("document_id"), p.get("page_no")) for p in pages}
    keys = dict.fromkeys((ref.get("document_id"), ref.get("page_no")) for ref in named_pages
                         if (ref.get("document_id"), ref.get("page_no")) in source_keys)
    rule = "\n".join(str(criterion.get(key) or "")
                     for key in ("required_evidence", "description", "source_quote"))
    fallback = {
        "kind": "material_review_pending", "basis": "machine_fallback",
        "value": str(subtotal) if subtotal is not None else None,
        "reason": detail, "score_type": criterion.get("score_type"),
        "required_materials": list(dict.fromkeys([*(missing or []), *parse_required_artifacts(rule)])),
        "candidate_pages": [{"document_id": doc, "page_no": page} for doc, page in keys],
        "unverified_records": records,
    }
    return CellOutcome(
        "concern", detail, None, evidence, pages, "fallback", len(attempts) or 1,
        {"fallback": fallback, "coverage_complete": False, "attempt_history": attempts,
         "verified_facts": facts},
        [{"message": detail, "fallback_score": fallback}],
    )


def _presence_outcome(result, criterion: dict, pages: list[dict]) -> CellOutcome:
    """材料存在性结论 → CellOutcome（present/absent 确定性；unverified 暂定待复核）。"""
    detail = str(getattr(result, "detail", "") or "")
    presence_raw = {
        "state": getattr(result, "state", "unverified"),
        "windows": getattr(result, "windows", []) or [],
        "material_pages": getattr(result, "material_pages", []) or [],
        "years": getattr(result, "years", {}) or {},
        "content_unverified": True,
    }
    raw = {
        "calculation": detail,
        "presence": presence_raw,
        "coverage_complete": getattr(result, "state", "") != "unverified",
        "confirmed_absent": getattr(result, "state", "") == "absent",
        "zero_reason": "confirmed_absent" if getattr(result, "state", "") == "absent" else None,
    }
    if getattr(result, "state", "") == "unverified":
        reason = "材料存在性尚未完成原页核验，未生成最终分"
        fallback = {
            "kind": "material_review_pending",
            "reason_code": "presence_unverified",
            "value": None,
            "value_role": "verified_subtotal",
            "basis": "machine_fallback",
            "reason": reason,
            "windows": presence_raw["windows"],
            "missing_years": [year for year, state in presence_raw["years"].items()
                              if state != "present"],
            "score_type": criterion.get("score_type"),
            "technical_error": True,
        }
        raw["fallback"] = fallback
        return CellOutcome(
            "concern", reason, None, [], pages, "fallback", 1, raw,
            [{
                "message": reason,
                "verdict_source": "deterministic",
                "fallback_score": fallback,
                "unresolved": "presence_unverified",
            }],
        )
    score = getattr(result, "score", None)
    verdict = "pass" if score is not None and score > 0 else "fail"
    return CellOutcome(
        verdict, detail, score, [], pages, "deterministic", 1, raw,
        [{"message": detail + "（需人工抽核）", "verdict_source": "deterministic"}],
    )


def _presence_unverified_outcome(
    criterion: dict, pages: list[dict], detail: str,
) -> CellOutcome:
    """absence 防线转来的“未核验”（不按 failure_policy=stop 冒泡）。"""
    from app.engine.presence import PresenceResult

    return _presence_outcome(
        PresenceResult("unverified", None, detail, reason_code="absence_scan_guard"),
        criterion, pages,
    )


def _value_extraction_unresolved_outcome(
    criterion: dict, pages: list[dict], detail: str,
) -> CellOutcome:
    """承诺值抽取未果时保留人工复核，不生成暂定数值分。"""
    reason = "承诺值抽取未形成可核验终态，未生成最终分"
    fallback = {
        "kind": "material_review_pending",
        "reason_code": "value_extraction_unresolved",
        "value": None,
        "value_role": "verified_subtotal",
        "basis": "machine_fallback",
        "reason": reason,
        "score_type": criterion.get("score_type"),
        "technical_error": True,
    }
    raw = {
        "calculation": reason,
        "coverage_complete": False,
        "zero_reason": "value_extraction_unresolved",
        "fallback": fallback,
        "technical_detail": str(detail or "").strip()[:500],
    }
    return CellOutcome(
        "concern", reason, None, [], pages, "fallback", 1, raw,
        [{
            "message": reason,
            "verdict_source": "deterministic",
            "fallback_score": fallback,
            "unresolved": "no_facts",
        }],
    )


class _ValueExtractOut(BaseModel):
    """承诺值残差抽取的 LLM 输出契约（包A 第 2 步）：抽值 + 逐字引文。"""

    value: str = ""
    quote: str = ""


def _value_extraction_residual(
    criterion: dict, pages: list[dict], inventory, gateway_factory,
) -> CellOutcome | None:
    """残差轮（包A 第 2 步）：锚词命中但正则解析不出 → 一次纯文本 LLM 抽值。

    三道校验全过才出分：① 逐字引文必须在该家原文中可定位（空白归一）；
    ② 数值必须能从引文复算（auto_review._numeric_values，与模型事实证据闸门
    同一通道）；③ 按冻结档位/线性参数复算计分。任一不过、开关
    BID_EVAL_VALUE_EXTRACT_LLM=0、LLM 未配置/故障 → None（调用方落暂定兜底）。
    只送文本片段，绝不触发 OCR/视觉/读页图。
    """
    if not config.value_extract_llm_enabled():
        return None
    params = dict(criterion.get("params") or {})
    field = str(params.get("field") or "")
    if field not in ("response_hours", "warranty_months"):
        return None
    from app.engine.auto_review import _numeric_values
    from app.engine.objective import value_anchor_snippets
    from app.parsing.normalize import normalize_no_ws
    from app.rules.prompts import VALUE_EXTRACT_SYSTEM_PROMPT, VALUE_EXTRACT_USER_TEMPLATE

    corpus = str(getattr(inventory, "full_text", "") or "")
    snippets = value_anchor_snippets(field, corpus)
    if not snippets:
        return None
    rule = "\n".join(str(criterion.get(key) or "") for key in
                     ("name", "description", "source_quote"))
    user = VALUE_EXTRACT_USER_TEMPLATE.format(
        rule=rule.strip(), field=field, snippets="\n---\n".join(snippets))
    try:
        gateway = gateway_factory()
        out, _result = gateway.chat_json(
            VALUE_EXTRACT_SYSTEM_PROMPT, user, _ValueExtractOut)
    except JobCancelled:
        raise
    except Exception:  # noqa: BLE001 - LLM 未配置/配额/契约失败 → 暂定兜底
        log.info("承诺值残差抽取不可用 criterion=%s", criterion.get("code"), exc_info=True)
        return None
    quote = str(out.quote or "").strip()
    raw_value = str(out.value or "").strip()
    if not quote or not raw_value:
        return None
    norm_quote = normalize_no_ws(quote)
    if not norm_quote or norm_quote not in normalize_no_ws(corpus):
        return None  # 引文不是原文逐字片段 → 拒绝
    try:
        value = Decimal(raw_value)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite() or value < 0:
        return None
    if value not in _numeric_values(quote, field):
        return None  # 数值无法从引文复算 → 拒绝（与事实证据闸门同口径）
    scorer = score_value_tier if str(params.get("engine")) == "value_tier" else score_value_linear
    try:
        scored = scorer(
            {"max_score": criterion.get("max_score"),
             "verified_facts": {field: str(value)}},
            params, BidFacts(text=corpus),
        )
    except Exception:  # noqa: BLE001 - 档位参数异常 → 暂定兜底
        return None
    if scored.unresolved:
        return None
    try:
        score = _score_in_range(scored.score, criterion)
    except (LLMError, InvalidOperation, TypeError, ValueError):
        return None
    evidence: list[dict] = []
    if len(norm_quote) >= config.MIN_QUOTE_CHARS:
        for page in pages:
            # 落页证前按 verify_automatic_citation 同口径自检（引文必须逐字落
            # 在单页原文内），否则 _persist_cell 的逐字核验会掀翻整格。
            if (page.get("document_id") is None or page.get("page_no") is None):
                continue
            if norm_quote in normalize_no_ws(str(page.get("text") or "")):
                evidence = [{
                    "document_id": page.get("document_id"),
                    "page_no": page.get("page_no"),
                    "quote": quote, "kind": "text",
                }]
                break
    detail = (
        f"残差抽取（LLM 纯文本）：{field}={value}，引文复算通过"
        f"（{quote[:60]}）→ {scored.detail}"
    )
    raw = {
        "calculation": detail,
        "verified_facts": {field: str(value), f"{field}_quote": quote},
        "coverage_complete": True,
        "zero_reason": None,
        "deterministic_plan": True,
    }
    return CellOutcome(
        "pass" if score > 0 else "fail", detail, score, evidence, pages,
        "deterministic", 1, raw,
        [{"message": detail, "verdict_source": "llm"}],
    )


def _unverified_scan_evidence(decision, pages: list[dict]) -> list[dict]:
    """复核页中的未 OCR 整页扫描件（absence 0 分防线用）。"""
    from app.engine.presence import is_unverified_full_page_scan

    reviewed = {
        (int(page.get("document_id") or 0), int(page.get("page_no") or 0))
        for page in (getattr(decision, "reviewed_pages", None) or [])
    }
    if not reviewed:
        return []
    return [
        {"document_id": page.get("document_id"), "page_no": page.get("page_no")}
        for page in pages
        if (int(page.get("document_id") or 0), int(page.get("page_no") or 0)) in reviewed
        and is_unverified_full_page_scan(page)
    ]


def _decimal(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LLMError(f"{label}不是有效数值") from exc
    if not number.is_finite():
        raise LLMError(f"{label}不是有限数值")
    return number


def _confirmed_zero_allowed(criterion: dict) -> bool:
    """Only an explicit missing-material sanction may publish a confirmed zero."""
    rule = "\n".join(str(criterion.get(key) or "") for key in (
        "description", "source_quote", "required_evidence", "required_artifacts",
        "required_artifacts_raw",
    ))
    return bool(re.search(r"不(?:予)?得分|不计分|(?:计|得|为|扣至)\s*0\s*分", rule))


def _score_in_range(value: Any, criterion: dict) -> Decimal:
    score = _decimal(value, "得分")
    maximum = _decimal(criterion.get("max_score"), "满分")
    if score < 0 or score > maximum:
        raise LLMError(f"得分 {score} 超出细则范围 0–{maximum}")
    return score.quantize(Decimal("0.01"))


def _load_pages(bidder_id: int) -> tuple[list[dict], str, list[dict]]:
    """读取一家投标人的全部送审页面。

    **逐文档降级**（2026-11-17 run 125 实证）：单个附件不可读（解析失败 / DOCX
    嵌入图片无法映射页码 / 页数不完整）只把**该文档**记入 unreadable_docs，
    其余可读文档照常送审——旧实现一家有一个坏附件就整家抬走，天兴（.xls
    解析失败）、青云（.docx 嵌入图片）400 页主标书明明可读，技术/商务分却
    全部被兜底清零。只有**全部**文档不可读时才抛错，且按读取故障（
    reading_incomplete，未定分待补读）定性，不按材料缺失计 0。
    """
    from app.services.automatic_document_service import annotate_page_readability

    with session_scope() as s:
        docs = s.execute(select(Document).where(Document.bidder_id == bidder_id)
                         .order_by(Document.id)).scalars().all()
        if not docs:
            raise LLMError("未找到投标文件，无法执行自动评审")
        pages: list[dict] = []
        unreadable_docs: list[dict] = []
        data_class = "synthetic"

        def _drop(doc, reason: str) -> None:
            unreadable_docs.append({"document_id": doc.id,
                                    "filename": doc.original_filename,
                                    "reason": reason})

        for doc in docs:
            if doc.parse_status not in ("parsed", "degraded", "ocr_required"):
                _drop(doc, f"文件 {doc.id} 尚未成功解析")
                continue
            if doc.data_class == "confidential":
                data_class = "confidential"
            elif doc.data_class == "redacted_sample" and data_class != "confidential":
                data_class = "redacted_sample"
            rows = s.execute(select(DocumentPage).where(DocumentPage.document_id == doc.id)
                             .order_by(DocumentPage.page_no)).scalars().all()
            if not rows:
                _drop(doc, f"文件 {doc.id} 没有可评审页面")
                continue
            if doc.page_count and len(rows) != doc.page_count:
                _drop(doc, f"文件 {doc.id} 的解析页数不完整")
                continue
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
                unverified_scan = (
                    not page.has_text_layer and page.source not in ("ocr", "ocr_img")
                )
                if not config.scoring_ocr_enabled():
                    # 用户指令（2026-11-19）：扫描件不做内容 OCR、只打标签。
                    # 因此扫描页不能作为阻断性"读取故障"——否则所有涉及扫描页的
                    # 格都会兜底 0 分（run138 实证）。保留 scan_unverified 标签
                    # 供报告展示"扫描件未核验"，评审按现有文本进行。
                    unreadable = False
                document_pages.append({
                    "document_id": doc.id, "page_no": page.page_no,
                    "text": page.text or "", "source": page.source or "text",
                    "has_text_layer": page.has_text_layer,
                    "stored_path": doc.stored_path, "sha256": doc.sha256,
                    "page_count": doc.page_count,
                    "width": page.width, "height": page.height,
                    "known_blank": known_blank, "unreadable": unreadable,
                    "requires_vision": unreadable,
                    "scan_unverified": unverified_scan,
                })
            try:
                pages.extend(annotate_page_readability(document_pages, doc.stored_path, doc.warnings_json))
            except LLMError as exc:
                # DOCX 嵌入图像/图像可读性核验拒绝等：只降级该文档，不抬走整家
                _drop(doc, str(exc)[:200])
        if not pages:
            detail = "；".join(f"{d['filename']}（{d['reason']}）" for d in unreadable_docs)
            err = LLMError(f"全部投标文件均未能读取，无法执行自动评审：{detail}")
            err.reading_incomplete = True
            raise err
        # 页级 material_tags（解析期已分类）附到页 dict——评分读页阶段据此跳过
        # 结构性噪声页（2026-11-18 用户指令：解析期标签应被评分复用，无用页不重读）
        from app.db.models import MaterialTag

        tag_rows = s.execute(
            select(MaterialTag.document_id, MaterialTag.page_no, MaterialTag.tags_json)
            .where(MaterialTag.document_id.in_([d.id for d in docs]),
                   MaterialTag.seg_kind == "page")
        ).all()
        tags_by_page = {
            (int(r[0]), int(r[1])): [str(x) for x in (r[2] or [])] for r in tag_rows
        }
        for p in pages:
            p["material_tags"] = tags_by_page.get(
                (int(p["document_id"]), int(p["page_no"])), [])
        return pages, data_class, unreadable_docs


def _page_text(pages: list[dict]) -> str:
    return "\n".join(str(p.get("text") or "") for p in pages)


# 结构性噪声页标签（永远不可能是文本证据）：目录/空白/分隔页。只在标签
# **明确命中**时跳过——未打标/拿不准的照读（正向判定，不猜）。
# 注意：封面类不入列（可能带公章/投标人标识，签章与身份核验要用）。
_NOISE_PAGE_TAGS = {"toc", "目录", "空白", "空白页", "分隔页", "隔页"}


def _is_noise_page(page: dict) -> bool:
    tags = [str(t).strip().lower() for t in (page.get("material_tags") or [])]
    return any(t in _NOISE_PAGE_TAGS for t in tags)


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


_VISUAL_HINTS = ("签字", "签章", "盖章", "公章", "印章", "签名", "密封", "授权")
_SUPPLEMENT_HINTS = ("合同", "发票", "验收", "证书", "认证", "资质", "营业执照", "社保",
                     "授权", "业绩", "案例", "检测", "设备", "购置", "审计", "信用", "资信",
                     "体系", "简历", "职称", "售后", "承诺", "证明")


def _supplement_pages(criterion: dict, pages: list[dict], *, visual: bool, exhaustive: bool = False,
                      max_pages: int | None = None) -> list[dict]:
    """Supplement only relevant source pages; retain originals and file/page IDs.

    BID_EVAL_SCORING_OCR=0 disables OCR while original-page image rendering
    remains available for selected visual review.
    """
    if not visual and not config.scoring_ocr_enabled():
        return pages
    from app.engine.auto_review import select_candidate_pages

    copied = [dict(p) for p in pages]
    requested = set(criterion.get("_visual_page_keys" if visual else "_material_page_keys") or [])
    if visual and exhaustive:
        candidates = [p for p in copied if not p.get("known_blank")]
    elif requested:
        candidates = [p for p in copied if (int(p["document_id"]), int(p["page_no"])) in requested]
    else:
        candidates = select_candidate_pages(criterion, copied, max_pages=8)
    # Exhaustive counts/absence require every unreadable page, including scans
    # whose headings cannot yet be retrieved by a text query.
    if visual or not requested:
        candidates = candidates + [p for p in copied if (p.get("unreadable") or p.get("requires_vision")) and not p.get("known_blank")]
    # 预算上限（2026-11-19 run137 实证：大扫描标书补检被放大到数万次 OCR）。
    # 截断时材料/签章关键词页与带页级标签的页优先，其余按页序。
    if max_pages is None and visual:
        max_pages = config.visual_max_pages()
    if max_pages is not None and len(candidates) > max_pages:
        hints = _VISUAL_HINTS if visual else _SUPPLEMENT_HINTS

        def _rank(pg: dict) -> tuple[int, int]:
            tags = pg.get("material_tags") or []
            text = str(pg.get("text") or "")[:300]
            hinted = any(k in text for k in hints)
            return (0 if tags else 1 if hinted else 2, int(pg["page_no"]))

        candidates = sorted(candidates, key=_rank)[:max_pages]
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
        # 2026-11-19：只对"几乎没有文字"的页补 OCR。旧判据（source!=text /
        # 无文字层 / 字符数<800）把已识别页和内容页全部重复送 OCR——run137
        # 2.8 万次请求、10 小时未出分的主因。
        if (page["document_id"], page["page_no"]) in targets and not _is_noise_page(page) and (
            page.get("unreadable") or page.get("requires_vision")
            or len(str(page.get("text") or "").strip()) < config.ocr_supplement_min_chars()
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
    if (decision.facts.get("_fact_provenance") or {}).get("cert_section_present"):
        # 已由确定性事实供给注入（ADR 0015）：同源检测结果，不重算不覆盖 provenance
        return
    from app.engine.facts import extract_bid_facts
    from app.engine.objective import cert_evidence_present, classify_cert_backing

    facts = extract_bid_facts(_page_text(pages))
    facts.certificates = list(certificates)
    page_pairs = [(str(p.get("source") or "text"), str(p.get("text") or "")) for p in pages]
    section = cert_evidence_present(page_pairs)
    backing = classify_cert_backing(facts, page_pairs, section)
    if backing:
        # 只升不降（ADR 0015 同款）：确定性 True 可升模型的 None/False，但不得把
        # 模型已核验的 True 用"材料看不清"降级——降级曾把整格打成暂定 0。
        existing = decision.facts.get("cert_backing")
        merged = {normalize_cert(str(k)): v for k, v in
                  (existing.items() if isinstance(existing, dict) else ())}
        for key, state in backing.items():
            norm = normalize_cert(str(key))
            if state is True or norm not in merged:
                merged[norm] = state
        decision.facts["cert_backing"] = merged
        if "cert_section_present" not in decision.facts:
            decision.facts["cert_section_present"] = bool(section[0])


def _verified_count(criterion: dict, decision, pages: list[dict]) -> tuple[Decimal, str, dict]:
    """AI supplies evidence-backed facts; existing rule engines supply arithmetic."""
    values = dict(decision.facts or {})
    if not decision.coverage_complete:
        raise _MaterialReviewPending("客观计分仍需核验相关原页", facts=values)
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
    # 确定性 overlay 移植入自动路（ADR 0015，2026-11-18）：SC 深评路既有语义
    # （scoring_service._overlay_* 同源：只升不降/严口径）——补齐"AS 不经 overlay"
    # 缺口，与确定性事实供给互为表里。run48/49 社保来回摆、run64 经验证明误杀
    # 等实证口径不变。
    cases = values.get("verified_cases")
    if isinstance(cases, list) and cases and params.get("person_scope"):
        from app.services import scoring_service as _ss

        # SC 深评路 overlay 契约是 (page_no, text, source) 元组；AS 这里是页 dict，
        # 直接传会把页 dict 当序列解包 → "too many values to unpack (expected 3)"
        # （run130 c29 143 实证）。
        overlay_pages = [(int(p.get("page_no") or 0), str(p.get("text") or ""),
                          str(p.get("source") or "")) for p in pages]
        _ss._overlay_social_security(cases, overlay_pages)
        if str(params.get("person_scope")) == "member":
            _ss._overlay_work_proof(cases, overlay_pages)
        else:
            _ss._overlay_experience_proof(cases, overlay_pages)
            _ss._overlay_leader_table_proof(cases, overlay_pages)
    for name in ("response_hours", "warranty_months"):
        if name in values:
            number = _decimal(values[name], name)
            if number < 0:
                raise LLMError(f"{name}不能小于零")
            values[name] = str(number)
    prepared = {**criterion, "verified_facts": values}
    if params.get("field") == "cmmi" and (params.get("cert_min_level") or params.get("cert_require_current")):
        from app.engine.material_inventory import maturity_certificate_details

        details = maturity_certificate_details(pages)
        prepared["certificate_details"] = details
        values["certificate_details"] = details
    # 承诺值字段的全文抽取语料（包A）：该家 source=='text' 页文本层拼接，
    # 供 score_value_* 在模型未供值时做确定性抽取的第三级兜底（零 OCR）。
    prepared["fulltext_corpus"] = "\n".join(
        str(p.get("text") or "") for p in pages
        if str(p.get("source") or "text") == "text"
    )
    backing = values.get("cert_backing") or {}
    if certificates:
        if not isinstance(backing, dict):
            raise LLMError("证书背书核验结果无效")
        backing = {normalize_cert(str(k)): v for k, v in backing.items()}
        if any(v is not None and not isinstance(v, bool) for v in backing.values()):
            raise LLMError("证书背书核验结果无效")
        # 背书三态（bool/None）：None = 复印件不可读/未绑上，由 score_count 计入
        # 并标疑问（用友防线）；缺省同 None。防伪由 _validate_fact_sources 的证据
        # 闸门负责——旧"逐本必须 bool"会把材料看不清的整格打成暂定 0（run128）。
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
    from app.engine.material_inventory import platform_query_families, requires_platform_certificate_query

    if params.get("field") == "certificates" and requires_platform_certificate_query(criterion):
        needs_seal = bool(re.search(r"公章|盖章|签章", material_rule))
        queried = {family for page in pages for family in
                   platform_query_families(str(page.get("text") or ""), require_seal=needs_seal)}
        missing_certs = [cert for cert in certificates if normalize_cert(cert) not in queried
                         and backing.get(normalize_cert(cert)) is not False]
        if missing_certs:
            raise _MaterialReviewPending("仍需核验对应证书的认证平台查询结果截图", facts=values,
                                         missing=[f"{cert} 认证平台查询结果截图" for cert in missing_certs])
        values["cert_section_present"] = bool(queried)
    if certificates and "截图" in material_rule:
        if not isinstance(values.get("cert_section_present"), bool):
            raise _MaterialReviewPending("仍需核验本项认证证书及对应的查询截图", facts=values,
                                         missing=["查询截图"])
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
                raise _MaterialReviewPending("案例仍有引文或材料须补评", facts=values)
            identity = (re.sub(r"\s", "", case["name"]), str(case.get("person") or ""))
            if identity in identities:
                raise LLMError("案例清单存在重复，须合并同一案例的跨页证明材料后重算")
            identities.add(identity)
            # Missing material flags (None) are handled by score_count's
            # three-state gate (counted + marked unverified).  Refusing here
            # skipped every performance cell on real scans (run 88/89).
            if params.get("person_scope"):
                role = _normalize_role(case)
                # 模型未标注归属 → 交给 score_count 的"按全部案例计 + 标疑问"
                # 分支（与 SC 深评路同口径）；旧实现整格暂定 0（run134 142 c29）。
                if role is not None:
                    case["role"] = role
            if params.get("min_amount_yuan") or amount_tiers:
                if case.get("amount_yuan") is None or _decimal(case["amount_yuan"], "案例金额") < 0:
                    if config.case_unverified_count_enabled():
                        # 已提供了原页/案例，全自动口径不再把金额绑定失败当运行未完成；
                        # score_count 会按最高金额档直接计分并保留 amount_unverified。
                        case["unverified"] = True
                        case["unverified_reason"] = "amount_unverified"
                        case["amount_fallback"] = "highest_tier_from_visible_case"
                    else:
                        raise _MaterialReviewPending("仍需核验案例合同金额", facts=values,
                                                     missing=["合同金额"])
            if params.get("person_scope") == "leader" and "运维" in (
                str(criterion.get("name") or "") + str(criterion.get("description") or "")
            ) and not isinstance(case.get("type_eligible"), bool):
                    raise _MaterialReviewPending("仍需核验类似运维案例的项目类型", facts=values,
                                                 missing=["类似运维案例的项目类型"])
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
    if (params.get("field") == "cmmi" and result.unresolved == "certificate_unverified"
            and not prepared.get("certificate_details") and decision.coverage_complete
            and (facts.cert_claims or facts.certificates)):
        return (Decimal(0),
                "完整检索仅见认证标题或声称，未提供可核验的等级、有效期证书，按细则要求计 0 分",
                values)
    if result.unresolved:
        if decision.confirmed_absent and decision.coverage_complete and result.unresolved == "no_facts":
            rule = requirements + str(criterion.get("description") or "") + str(criterion.get("source_quote") or "")
            if engine in (None, "count", "count_tier", "attribute_tier"):
                scans = _unverified_scan_evidence(decision, pages)
                if scans:
                    # 2026-11-19 run144 实证：纯文本模式下整页扫描件只剩页眉，
                    # 模型会把“没读到”判成“未提供”，确定性计数再把这个
                    # confirmed_absent 折成 0 分。只要复核页里有未 OCR 整页
                    # 扫描件，就不能按材料缺失计 0——转人工/暂定。
                    err = LLMError(
                        "材料缺失结论涉及未 OCR 整页扫描件，不能确认未提供；"
                        "已转材料存在性复核")
                    err.presence_unverified = True
                    err.unverified_scan_pages = scans[:50]
                    raise err
                score = Decimal(0)
                if engine != "attribute_tier" and params.get("tiers") and not amount_tiers:
                    from app.engine.objective import _tier_score
                    score = _tier_score(params["tiers"], Decimal(0)) or Decimal(0)
                return _score_in_range(score, criterion), f"完整检索确认符合计分条件的数量为 0，按冻结计数规则计 {score} 分", values
            if re.search(r"不(?:予)?得分|不计分|(?:计|得|为|扣至)\s*0\s*分", rule):
                return Decimal(0), "完整材料检索确认无所需证明，按细则明确规定计 0 分", values
        if (result.unresolved == "cases_unverified" or params.get("field") == "cmmi"
                or (result.unresolved == "no_facts"
                and engine not in ("value_tier", "value_linear", "deviation_deduct_table"))):
            raise _MaterialReviewPending(result.detail, facts=values)
        raise LLMError(f"客观项事实或评分口径仍未解决：{result.unresolved}")
    if result.unverified_counted:
        verified_only = result.score_verified_only
        if verified_only is None and params.get("field") == "certificates" and certificates:
            checked_facts = BidFacts(text="", certificates=[cert for cert in certificates
                                    if backing.get(normalize_cert(cert)) is True])
            verified_only = scorer(prepared, params, checked_facts).score
        if result.score > 0 and config.case_unverified_count_enabled():
            # 全自动模式：已提供原页/案例且已有正向可计分时直接定分，
            # 未核验案例只留审计警示，不再阻塞整格或排名。
            return (_score_in_range(result.score, criterion),
                    result.detail + "；未核验案例暂不计入，按已核实分直接计分",
                    values)
        raise _MaterialReviewPending(
            result.detail, facts=values,
            subtotal=_score_in_range(verified_only, criterion) if verified_only is not None else None,
        )
    if result.zero_reason in ("unreadable", "cases_unverified", "gate_named_missing",
                              "material_unverified"):
        raise _MaterialReviewPending(result.detail, facts=values)
    # Heuristic conflict alone is discharged by the evidence-backed second pass;
    # missing factual inputs above remain execution failures.
    return _score_in_range(result.score, criterion), result.detail, values


def _verified_price_limit(criterion: dict, decision) -> tuple[str, str, dict]:
    """Never infer annual compliance from a confirmed total or a model verdict."""
    from app.engine.price_limit import resolve_limit_keys

    params = criterion.get("params") or {}
    frozen = criterion.get("_frozen_bidder") or {}
    values = {}
    missing = []
    comparisons = []
    exceeded = False
    # R46-A 键归一：别名 max_yuan/max 与规范键走同一严核验路径
    total, yearly, _incl, _rate, _kind = resolve_limit_keys(params)
    checks = [("bid_price", total)] if total is not None else []
    checks += [(f"yearly_bid_price_{i}", cap) for i, cap in enumerate(yearly or [], 1)]
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
    from app.engine.auto_review import (EvidenceBindingError, is_external_requirement, is_price_limit_requirement,
                                        resolve_price_limit_requirement, review_criterion)
    from app.services.review_activity import report

    c = _route_criterion(criterion)
    # 2026-11-19 材料存在性快路径（run144 c16/c17/c23/c29 实证）：目录/索引
    # 定位 + 页级图像信号判定“有没有材料”，零 LLM/零 OCR，先于任何补读/模型
    # 调用。present/absent 直接定格；unverified 落暂定待人工复核。新路径自身
    # 异常只记日志并回落既有评审，绝不把格子打死。
    try:
        from app.engine import presence as _presence

        if _presence.is_presence_criterion(c):
            return _presence_outcome(_presence.score_material_presence(c, pages), c, pages)
        if _presence.is_sample_criterion(c):
            sample = _presence.score_sample_materials(c, pages)
            if sample is not None:
                return _presence_outcome(sample, c, pages)
    except Exception:  # noqa: BLE001
        log.exception("材料存在性快路径失败 criterion=%s", c.get("code"))
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
    material_review = objective_score and (c.get("params") or {}).get("engine") not in (
        "value_tier", "value_linear", "deviation_deduct_table")
    if material_review:
        c = {**c, "_allow_material_recovery": True}
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
        decision = None
        try:
            report(f"{'初评' if round_no == 0 else f'补评 {round_no}/{rounds}'}")
            visual = False
            requested_visual, requested_pages = _requested_visual_pages(previous, current_pages)
            pending_pages = set((previous or {}).get("material_page_keys") or [])
            supplement_rule = {**c, "_visual_page_keys": sorted(requested_pages | pending_pages),
                               "_material_page_keys": sorted(pending_pages)}
            if round_no == 1:
                report("补做 OCR")
                current_pages = _supplement_pages(supplement_rule if pending_pages else c, current_pages, visual=False,
                                                  **({"max_pages": config.supplement_ocr_max_pages()}
                                                     if pending_pages else {}))
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
                # Objective counts are recomputed from the immutable material
                # inventory.  A model-requested attachment cannot veto that
                # deterministic result; only an unresolved deterministic result
                # remains pending after the scorer has run.
                deterministic_count = (
                    objective_score and criterion.get("score_type") == "count"
                )
                if not deterministic_count:
                    if material_review:
                        raise _MaterialReviewPending(
                            "仍需核验：" + "；".join(decision.missing_evidence),
                            facts=decision.facts, missing=decision.missing_evidence,
                        )
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
                    if material_review:
                        raise _MaterialReviewPending("仍需定位计分所依据的原文或原页", facts=decision.facts)
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
                decision.verdict = "pass" if score > 0 else "fail"
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
        except _MaterialReviewPending as exc:
            attempts.append({"round": round_no, "state": "failed", "error": str(exc)})
            pending = _material_review_pending(
                c, current_pages, str(exc), subtotal=exc.subtotal, facts=exc.facts,
                missing=exc.missing, evidence=decision.evidence if decision is not None else [],
                attempts=attempts,
            )
            if round_no >= min(1, rounds) or not config.scoring_ocr_enabled():
                return pending
            targets = _material_recovery_pages(c, current_pages, pending.raw["fallback"], None)
            previous = {"execution_attempt": execution_attempt, "error": str(exc), "round": round_no,
                        "material_page_keys": [(p["document_id"], p["page_no"]) for p in
                                               targets[:config.supplement_ocr_max_pages()]]}
        except (LLMNotConfigured, AppError) as exc:
            exc.attempt_history = attempts + [{"round": round_no, "state": "failed", "error": str(exc)}]
            raise
        except Exception as exc:
            from app.llm.request_sizing import ReviewContextExceeded
            from app.parsing.pdf_runtime import OCRProcessingError

            if material_review and (isinstance(exc, EvidenceBindingError)
                                    or isinstance(exc.__cause__, EvidenceBindingError)):
                attempts.append({"round": round_no, "state": "failed", "error": str(exc)})
                return _material_review_pending(c, current_pages, "补核证明材料原页及其归属", attempts=attempts)
            if getattr(exc, "presence_unverified", False):
                if material_review and getattr(exc, "unverified_scan_pages", None):
                    attempts.append({"round": round_no, "state": "failed", "error": str(exc)})
                    outcome = _material_review_pending(c, current_pages, "继续核对关联证明材料原页",
                                                       facts=decision.facts if decision is not None else None,
                                                       evidence=decision.evidence if decision is not None else [],
                                                       attempts=attempts)
                    # These refs came from a coverage guard over the whole file.
                    # Leave ranking to the criterion's case/section evidence.
                    outcome.raw["unverified_scan_pages"] = exc.unverified_scan_pages
                    return outcome
                # auto_review absence 防线：确认“未提供”的窗口里有未 OCR
                # 整页扫描件 → 不能按材料缺失结项，也不能按 stop 让整轮停下；
                # 定格为“未核验待人工复核”（不计入正式总分）。
                outcome = _presence_unverified_outcome(
                    c, current_pages,
                    str(exc) or "材料缺失结论涉及未 OCR 扫描页，需人工复核",
                )
                outcome.attempts = round_no + 1
                return outcome
            if isinstance(exc, (ReviewContextExceeded, OCRProcessingError)) or (isinstance(exc, LLMError) and (
                    exc.transient or exc.review_attempts_exhausted)):
                # Transport retries belong to the provider; an exhausted HTTP
                # or contract repair must not trigger a full OCR/visual reread.
                exc.attempt_history = attempts + [{"round": round_no, "state": "failed", "error": str(exc)}]
                raise
            # P0-3（测试10 run113）：读取故障不是语义失败——continue 快速失败捷径
            # 不适用，必须走完补读轮（round 1 补 OCR、round 2+ 视觉补读）再定论，
            # 否则"须补读原页图像并视觉核验"的错误在第 0 轮就被折成暂定 0 分。
            reading_gap = isinstance(exc, LLMError) and exc.reading_incomplete
            previous = {"execution_attempt": execution_attempt, "error": str(exc), "round": round_no}
            attempts.append({**previous, "state": "failed"})
            if continue_after_errors() and not preparation_step and not reading_gap:
                exc.attempt_history = attempts
                raise
            if round_no == rounds:
                error = LLMError(f"自动补评 {rounds} 轮后仍未完成：{exc}")
                error.attempt_history = attempts
                if reading_gap:
                    error.reading_incomplete = True
                    error.unreadable_pages = list(getattr(exc, "unreadable_pages", []) or [])
                raise error from exc
    raise LLMError("自动评审未产生有效结论")


def _fallback_score_for(criterion: dict) -> Decimal | None:
    """Technical failures have no legitimate numeric fallback."""
    return None


def _write_closure_fallback(s, row: ScoreRecord, criterion: dict, error: BaseException) -> None:
    """Close technical work as pending review, never as a numeric score."""
    from app.services.review_collections import (
        public_error_message, sanitize_rejected_evidence,
        sanitize_validation_history, error_reason_code,
    )

    reason_code = error_reason_code(error)
    reason = public_error_message(error)
    detail = str(error).strip()[:500] or type(error).__name__
    subtotal = getattr(error, "verified_subtotal", None)
    try:
        subtotal_value = Decimal(str(subtotal)) if subtotal is not None else None
        if subtotal_value is not None and not subtotal_value.is_finite():
            subtotal_value = None
    except (InvalidOperation, TypeError, ValueError):
        subtotal_value = None

    unreadable_pages = list(getattr(error, "unreadable_pages", []) or [])
    candidate_pages = list(getattr(error, "candidate_pages", []) or unreadable_pages)
    required_materials = {
        "reading_incomplete": "补读并核验原页",
        "evidence_binding": "复核原批次证据绑定",
        "model_output_incomplete": "人工复核模型输出与原页证据",
        "execution_incomplete": "自动核验已完成的评审工作",
    }[reason_code]
    rejected = sanitize_rejected_evidence(getattr(error, "rejected_evidence", None))
    fallback = {
        "kind": "material_review_pending",
        "reason_code": reason_code,
        "value": str(subtotal_value) if subtotal_value is not None else None,
        "value_role": "verified_subtotal",
        "basis": "machine_fallback",
        "reason": reason,
        "score_type": criterion.get("score_type"),
        "technical_error": True,
        "error_code": str(getattr(error, "code", type(error).__name__)),
        "required_materials": [required_materials],
        "candidate_pages": candidate_pages,
    }
    if unreadable_pages:
        fallback["unreadable_pages"] = unreadable_pages
    if rejected:
        fallback["rejected_evidence"] = rejected

    scoring = criterion.get("score_type") not in ("veto", "match")
    full_auto = config.full_auto_finalization_enabled()
    final_value = subtotal_value if subtotal_value is not None else (Decimal("0") if scoring else None)
    if full_auto:
        row.final_score = final_value
        row.machine_score = final_value if scoring and criterion.get("score_type") != "subjective" else None
        row.ai_suggested_score = final_value if criterion.get("score_type") == "subjective" else None
        row.machine_status = ("pass" if final_value is not None and final_value > 0 else "fail") if scoring else "concern"
    else:
        row.final_score = None
        row.machine_score = None
        row.ai_suggested_score = None
        row.machine_status = "concern"
    row.review_status = "auto_confirmed"
    row.evidence_status = "none"
    row.ai_reasoning = reason
    row.evaluator_actor = "auto"
    row.confirm_time = utcnow()
    row.ai_raw_json = {
        "attempt_history": sanitize_validation_history(getattr(error, "attempt_history", None)),
        "validation_history": sanitize_validation_history(getattr(error, "validation_history", None)),
        "verified_facts": getattr(error, "verified_facts", None),
        "rejected_evidence": rejected,
        "technical_detail": detail,
        "auto_review": {
            "state": "completed",
            "basis": "deterministic" if full_auto else "fallback",
            "reasoning": "自动核验完成，已按当前材料形成最终结果" if full_auto else reason,
            "fallback": fallback,
            "auto_finalized": full_auto,
            "coverage_complete": full_auto,
            "calculation": (f"自动核验完成，按当前材料计 {final_value if final_value is not None else 0} 分"
                            if full_auto else None),
            "error_code": fallback["error_code"],
        },
    }
    warnings = [{
        "message": "自动核验完成，已按当前材料形成最终结果" if full_auto else reason,
        "fallback_score": fallback,
        "technical_error": not full_auto,
        "auto_finalized": full_auto,
    }]
    vl_note = getattr(error, "vl_fallback_note", None)
    if isinstance(vl_note, dict):
        safe_vl_note = {
            "visual_fallback": str(vl_note.get("visual_fallback") or "failed")[:80],
            "reason": str(vl_note.get("reason") or "visual_review_incomplete")[:80],
            "reason_code": str(vl_note.get("reason") or "visual_review_incomplete")[:80],
            "reviewed_pages": vl_note.get("reviewed_pages") or [],
            "facts_count": len(vl_note.get("facts") or []),
        }
        row.ai_raw_json["auto_review"]["visual_fallback_note"] = safe_vl_note
        warnings.append({
            "verdict_source": "visual",
            "message": "视觉补评已自动收敛，按当前材料形成最终结果",
            "reasoning": str(vl_note.get("reasoning") or "")[:200],
            "fallback_score": fallback,
            "technical_error": True,
        })
    row.warnings_json = warnings
    s.add(ReviewRecord(
        subject_type="score", subject_id=str(row.id), action="confirm", actor="auto",
        reason=reason,
        new_value_json={"verdict": "concern", "score": None,
                        "auto_review": row.ai_raw_json["auto_review"]},
    ))


def _persist_light_reject_citations(run_id: int, bidder_id: int, criterion: dict, outcome) -> None:
    """Persist qualification-light LLM rejection quotes with verified page identity."""
    payload = (outcome.raw or {}).get("qualification_light") or {}
    if payload.get("reject_source") != "llm" or not payload.get("reject_quotes"):
        return
    from app.evidence.verdict_citations import persist_verdict_citations

    with session_scope() as s:
        row = s.execute(select(ScoreRecord).where(
            ScoreRecord.run_id == run_id, ScoreRecord.bidder_id == bidder_id,
            ScoreRecord.criterion_id == criterion["id"],
        )).scalar_one_or_none()
        if row is None:
            return
        s.expunge(row)
    persist_verdict_citations(
        row, {"evidence_items": payload["reject_quotes"]},
        main_doc_id=None,
        role="veto" if criterion["score_type"] == "veto" else "match",
        stats=None,
    )


def _vl_note(outcome: CellOutcome | None, note: dict,
             error: BaseException | None = None) -> CellOutcome | None:
    """Attach failed visual-recovery audit notes without publishing a score."""
    if outcome is not None:
        raw = dict(outcome.raw or {})
        raw["visual_fallback_note"] = note
        outcome.raw = raw
        outcome.warnings = [*list(outcome.warnings or []), {
            "verdict_source": "visual",
            "message": f"视觉AI兜底未定分：{note.get('reason')}",
        }]
    if error is not None:
        try:
            setattr(error, "vl_fallback_note", note)
        except Exception:  # noqa: BLE001
            pass
    return outcome


def _material_recovery_pages(criterion: dict, pages: list[dict], fallback: dict,
                             inventory) -> list[dict]:
    from itertools import zip_longest

    from app.engine.material_locator import locate_material_sections, locator_enabled
    from app.engine.objective import documented_project_type_state
    from app.parsing.normalize import normalize_no_ws

    by_key = {(p.get("document_id"), p.get("page_no")): p for p in pages if not p.get("known_blank")}
    by_number: dict[int, list[tuple]] = {}
    for key in by_key:
        by_number.setdefault(key[1], []).append(key)
    priority = {}

    def add(key):
        if key in by_key:
            priority.setdefault(key, by_key[key])

    def add_number(raw_no):
        keys = by_number.get(raw_no, [])
        if len(keys) == 1:
            add(keys[0])

    for ref in fallback.get("unreadable_pages") or []:
        if isinstance(ref, dict):
            add((ref.get("document_id"), ref.get("page_no")))

    rule = "\n".join(str(criterion.get(key) or "") for key in
                     ("name", "description", "quote", "source_quote", "required_evidence"))
    params = criterion.get("params") or {}
    person_scope = params.get("person_scope")
    material_field = params.get("field")
    kinds = []
    if params.get("person_scope") or any(word in rule for word in ("工作经验", "社保", "团队", "人员", "驻场")):
        kinds.append("personnel")
    if params.get("field") == "cmmi" or any(word in rule.upper() for word in ("CMMI", "CSMM", "能力成熟度")):
        kinds.append("cmmi")
    if "截图" in rule or "查询结果" in rule:
        kinds.append("query_screenshot")
    if params.get("field") == "certificates" or any(word in rule for word in ("证书", "认证", "资质")):
        kinds.append("cert")
    for word, kind in (("发票", "invoice"), ("验收", "acceptance"), ("合同", "contract"), ("业绩", "performance")):
        if word in rule:
            kinds.append(kind)

    if material_field in ("certificates", "cmmi"):
        from app.engine.material_locator import _is_map_page

        for key, page in sorted(by_key.items()):
            if _is_map_page(page):
                continue
            head = normalize_no_ws(str(page.get("text") or "")[:800])
            if material_field == "cmmi" and re.search(r"CMMI|CSMM|能力成熟度", head, re.I) and re.search(
                    r"成熟度|Maturity|Appraisal|证书|评估", head, re.I):
                add(key)
            elif material_field == "certificates" and (
                    re.search(r"认证认可信息公共服务平台|cx\.cnca\.cn", head, re.I)
                    or re.search(r"管理体系认证证书|ISO(?:/IEC)?(?:9001|27001|20000).{0,20}(?:认证|证书)", head, re.I)):
                add(key)
                add((key[0], key[1] + 1))

    if material_field == "performance":
        from app.engine.material_locator import _is_map_page

        headings: dict[str, list[tuple[int, int]]] = {}
        section = None
        person_role = None
        current_document = None
        nominated_names: set[str] = set()
        member_case_open = False
        for key, page in sorted(by_key.items()):
            if key[0] != current_document:
                section, person_role = None, None
                nominated_names.clear()
                member_case_open = False
                current_document = key[0]
            if _is_map_page(page):
                continue
            text = str(page.get("text") or "")
            lines = [normalize_no_ws(line) for line in text.splitlines()[:10]]
            for heading in lines:
                if len(heading) > 110:
                    continue
                if re.match(r"^\d+(?:[.\-、]\d+){1,2}[.、]?(?:拟派)?(?:投标人)?(?:类似项目业绩|项目团队情况|项目团队人员情况|项目负责人情况|拟派人员情况)", heading):
                    section = "company" if "业绩" in heading else "team"
                    if section == "team":
                        person_role = "leader" if "负责人" in heading else "member" if "团队人员" in heading else None
                if section == "team" and re.match(r"^\d+(?:[.\-、]\d+){2,}", heading):
                    if re.search(r"驻[场点]运维|项目团队人员|人员社保|驻场工程师", heading):
                        person_role = "member"
                    elif re.search(r"项目负责人|项目经理", heading):
                        person_role = "leader"
                if person_scope == "member" and re.match(r"^\d+(?:[.\-、]\d+)+", heading):
                    team_case = bool(re.search(r"驻[场点][^。；]{0,12}工程师.*类似项目.*经验证明", heading))
                    if team_case:
                        member_case_open = True
                    elif re.search(r"到位率承诺|技术服务方案|项目负责人(?:情况|证明)", heading):
                        member_case_open = False
                if person_scope and section == "team" and person_role == person_scope:
                    if re.search(r"社保(?:保证明|缴纳|参保|证明)|社会保险参保证明", heading):
                        headings.setdefault("social", []).append(key)
                    elif re.search(r"[五5]年.*工作经验|工作经验证明|项目经理经验证明", heading):
                        headings.setdefault("experience", []).append(key)
                    elif re.search(r"类似项目.*经验|类似系统.*运维|项目经验\d|业主证明|实施证明", heading):
                        headings.setdefault("case", []).append(key)
                    elif re.search(r"履历表|情况表", heading):
                        headings.setdefault("person", []).append(key)
                if not person_scope and section == "company":
                    if re.search(r"验收报告|验收证明|工程终验|业主证明", heading) or (
                            heading == "项目类型" and {"项目名称", "用户单位"}.issubset(lines)):
                        headings.setdefault("acceptance", []).append(key)
                    elif re.search(r"发票|开票日期", heading):
                        headings.setdefault("invoice", []).append(key)
                    elif re.search(r"合同复印件|合同(?:含税)?总?金额|合同签订", heading):
                        headings.setdefault("contract", []).append(key)
            if person_scope == "member" and member_case_open:
                normalized_text = normalize_no_ws(text)
                for match in re.finditer(r"(?:两名|2名)项目驻[场点]运维工程师([\u4e00-\u9fff]{2,4})[、和及与]"
                                         r"([\u4e00-\u9fff]{2,4})[，,。]", normalized_text):
                    nominated_names.update(match.groups())
                if (nominated_names and re.search(r"工作职能|项目(?:中任职|实施顾问|技术人员)|参与项目阶段|驻点运维人员", text)
                        and any(name in normalized_text for name in nominated_names)):
                    headings.setdefault("annex", []).append(key)
                elif re.search(r"甲方.{0,30}(?:盖章|签章).{0,80}乙方.{0,30}(?:盖章|签章)", normalized_text):
                    headings.setdefault("signature", []).append(key)
        order = (("social", "case", "annex", "signature", "person", "experience") if person_scope == "member"
                 else ("social", "experience", "case", "person") if person_scope
                 else ("acceptance", "invoice", "contract"))
        for group in zip_longest(*(dict.fromkeys(headings.get(kind, [])) for kind in order)):
            for key in group:
                if key is not None:
                    add(key)

    for ref in fallback.get("candidate_pages") or []:
        if isinstance(ref, dict):
            add((ref.get("document_id"), ref.get("page_no")))
    for number in fallback.get("material_pages") or []:
        add_number(number)

    records = [r for r in fallback.get("unverified_records") or [] if isinstance(r, dict)]
    names = [normalize_no_ws(str(r.get("name") or "")) for r in records]
    names = [name for name in names if len(name) >= 4]
    for record in records:
        for number in [*(record.get("pages") or []),
                       *(number for nos in (record.get("materials") or {}).values() for number in nos or [])]:
            if isinstance(number, int):
                add_number(number)

    # Take a relevant page from each material category before expanding any one section.
    windows = (locate_material_sections(list(by_key.values()), tuple(kinds))
               if kinds and (locator_enabled() == "on" or fallback.get("kind") == "material_review_pending")
               else {})
    candidates_by_kind = []
    for kind in kinds:
        keys = dict.fromkeys((window.document_id, n) for window in windows.get(kind, []) for n in window.page_nos)
        candidates_by_kind.append(iter(k for k in keys if k in by_key and not _is_noise_page(by_key[k])))
    for row in zip_longest(*candidates_by_kind):
        for key in row:
            add(key)

    anchors = [word for word in _SUPPLEMENT_HINTS if word in rule]

    def rank(page):
        tags = [str(t) for t in page.get("material_tags") or []]
        text = str(page.get("text") or "")
        hay = " ".join(tags) + " " + text[:500]
        normalized = normalize_no_ws(text) if names else ""
        return (_is_noise_page(page), -sum(name in normalized for name in names),
                -sum(word in hay for word in anchors), not bool(tags), page["page_no"], page["document_id"])

    # Inventory page numbers predate document-scoped bindings. Only use an
    # unambiguous number here; the locator above supplies document keys directly.
    if inventory is not None:
        if any(kind in kinds for kind in ("cert", "cmmi", "query_screenshot")):
            material_records = inventory.certs
        elif "personnel" in kinds:
            material_records = inventory.persons
        else:
            material_records = inventory.contracts
        for record in material_records:
            for number in getattr(record, "experience_pages", []) or getattr(record, "pages", []):
                add_number(number)
        if "personnel" in kinds:
            for record in inventory.contracts or []:
                if documented_project_type_state(
                        record.project_type, record.name, rule) is not True:
                    continue
                for number in record.pages or []:
                    add_number(number)
                for numbers in (record.materials or {}).values():
                    for number in numbers or []:
                        add_number(number)
    for page in sorted(by_key.values(), key=rank):
        add((page["document_id"], page["page_no"]))
    return list(priority.values())


def _maybe_vl_rescue(run_id: int, bid_id: int, criterion: dict,
                     outcome: CellOutcome | None, error: BaseException | None,
                     pages_by_bidder: dict, budget,
                     inventory_factory=None, checkpoint: dict | None = None,
                     ) -> tuple[CellOutcome | None, BaseException | None]:
    """材料审核视觉大模型兜底（2026-11-23 优化②，run151 后改一页一图）。

    触发集=技术待复核 fallback（reading_incomplete/value_extraction_unresolved/
    presence_unverified 等）与 closure 错误路径；材料页=该格送审页
    （fallback 点名的不可读页优先），逐页渲染成图、每请求≤vl_fallback_max_images
    张（run 151 实证：整本长图十几 MB 被拒收 400）。VL 成功（计分格定分）→
    basis=visual_fallback 的正常结论（页级视觉核验，不产逐字引文）；否决/资格/
    符合性格只收线索（VL 结论绝不成否决）；其余（预算耗尽/涉密拦截/契约失败/
    渲染失败）→ 原待复核语义不变。不调用 OCR；成本上限由 Budget 保证。
    """
    from app.engine import vl_fallback

    if not config.vl_fallback_enabled():
        return outcome, error
    if error is not None:
        if getattr(error, "reading_incomplete", False) is False and not config.score_closure_enabled():
            return outcome, error  # 执行故障是可恢复技术态，不是分值
    elif outcome is None or outcome.basis != "fallback":
        return outcome, error
    loaded = pages_by_bidder.get(bid_id)
    if loaded is None:
        return outcome, error
    all_pages, data_class = loaded
    page_map = {(p["document_id"], p["page_no"]): dict(p) for p in all_pages}
    if outcome is not None:
        page_map.update({(p["document_id"], p["page_no"]): dict(p) for p in outcome.pages})
    fallback = ((outcome.raw or {}).get("fallback") if outcome is not None else None) or {}
    if error is not None:
        fallback = {**fallback, "unreadable_pages": getattr(error, "unreadable_pages", [])}
    kind = fallback.get("kind")
    pending = kind == "material_review_pending"
    if kind and not vl_fallback.is_trigger_kind(kind):
        return outcome, error

    inventory = None
    if inventory_factory is not None and not vl_fallback.is_verdict_criterion(criterion):
        try:
            inventory = inventory_factory(bid_id)
        except JobCancelled:
            raise
        except Exception:  # noqa: BLE001
            log.exception("未能建立补评材料索引 bid=%s", bid_id)

    from app.evidence.renderer import render_page_png
    from app.engine.objective_fastpath import score_objective
    from app.engine.material_inventory import requires_platform_certificate_query
    from app.engine.vl_facts import merge_vl_facts
    from app.engine.vl_rescore import rescore_from_merged_facts

    image_limit = config.vl_fallback_max_images()
    cell_key = f"{run_id}:{bid_id}:{criterion.get('id')}"
    facts: list[dict] = []
    visual_evidence: list[dict] = []
    observations: set[tuple] = set()
    reviewed_pages: list[dict] = []
    rechecked_certificate_pages: set[tuple[int, int]] = set()
    results: list = []
    res = None
    halt_reason = "no_renderable_pages"
    base = Decimal(str(fallback["value"])) if fallback.get("value") is not None else None
    maximum = Decimal(str(criterion["max_score"])) if criterion.get("max_score") is not None else None
    request_rule = {**criterion, "_material_recovery": fallback} if pending else criterion

    def render_original(page):
        if not page.get("stored_path") or page.get("page_no") is None:
            return None
        try:
            png = render_page_png(str(page.get("sha256") or ""), str(page["stored_path"]), int(page["page_no"]))
            content = png.read_bytes()
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("补评原页渲染失败 document=%s page=%s: %s", page["document_id"], page["page_no"], exc)
            return None
        page["image_url"] = "data:image/png;base64," + base64.b64encode(content).decode("ascii")
        page["image_sha256"] = hashlib.sha256(content).hexdigest()
        return {"data_url": page["image_url"], "document_id": page["document_id"], "page_no": page["page_no"]}

    def rescore():
        if inventory is None or not facts:
            return None
        try:
            merged = merge_vl_facts(inventory, facts)
            if (requires_platform_certificate_query(criterion)
                    and not merged.platform_query_certificates
                    and any(re.search(r"(?:未见|未提供|未附|缺少).{0,30}(?:查询结果截图|查询截图|查询结果)",
                                      str(ref.get("observation") or ""))
                            for ref in visual_evidence)):
                # A reviewed proof section can explicitly establish that the
                # required platform screenshots are absent. Keep that negative
                # scoped to this merged recovery evidence set.
                merged.audit = {**merged.audit, "cert_section_state": False}
            if not requires_platform_certificate_query(criterion):
                objective = score_objective(criterion, merged)
                if objective is not None:
                    return objective
            return rescore_from_merged_facts(criterion, merged)
        except JobCancelled:
            raise
        except Exception:  # noqa: BLE001
            log.exception("视觉补评事实未能重算 criterion=%s", criterion.get("name"))
            return None

    def at_verified_maximum(result):
        return (result is not None and maximum is not None and result.score >= maximum
                and not (result.facts or {}).get("unverified_score_included"))

    def at_confirmed_zero(result):
        return (result is not None and result.score == 0 and result.verdict == "fail"
                and result.complete
                and not (result.facts or {}).get("unverified_score_included"))

    if pending and checkpoint:
        earlier = checkpoint.get("visual_fallback_note") or {}
        for ref in earlier.get("reviewed_pages") or []:
            key = (ref.get("document_id"), ref.get("page_no"))
            page = page_map.get(key)
            if page and ref.get("sha256") == page.get("sha256"):
                reviewed_pages.append(ref)
                if ref.get("certificate_rechecked"):
                    rechecked_certificate_pages.add(key)
        for ref in checkpoint.get("visual_evidence") or []:
            key = (ref.get("document_id"), ref.get("page_no"))
            page = page_map.get(key)
            if not page or page.get("sha256") != ref.get("sha256") or not ref.get("observation"):
                continue
            # The immutable source-page hash authenticates the prior observation.
            # Rendering can vary slightly across workers, so image hash mismatch
            # must not discard a source-validated fact on resume. Keep the page
            # renderable for the persisted visual citation.
            if not page.get("image_url") and render_original(page) is None:
                continue
            observations.add((*key, ref["observation"]))
            visual_evidence.append({"kind": "visual", "document_id": key[0], "page_no": key[1],
                                    "observation": ref["observation"],
                                    **({"sha256": ref["sha256"]}
                                       if ref.get("sha256") else {})})
        for fact in earlier.get("facts") or []:
            refs = fact.get("evidence") or []
            if refs and all((ref.get("document_id"), ref.get("page_no"), ref.get("observation")) in observations
                            for ref in refs):
                facts.append(fact)
        res = rescore()
        if at_verified_maximum(res) or at_confirmed_zero(res):
            halt_reason = "checkpoint_rescored"

    prior_pages = {(ref["document_id"], ref["page_no"]) for ref in reviewed_pages}
    ranked_pages = _material_recovery_pages(criterion, list(page_map.values()), fallback, inventory)
    retry_pages = set()
    if prior_pages and (criterion.get("params") or {}).get("field") in ("certificates", "cmmi"):
        fact_pages = {(ref["document_id"], ref["page_no"])
                      for fact in facts for ref in fact.get("evidence") or []}
        best_pages = {(page["document_id"], page["page_no"]) for page in ranked_pages[:image_limit]}
        # A batch may omit a relevant original from its facts even though the
        # image was sent. Recheck the strongest such candidates once, then advance.
        retry_pages = (best_pages & prior_pages) - fact_pages - rechecked_certificate_pages
    ordered = (page for page in ranked_pages
               if (page["document_id"], page["page_no"]) not in prior_pages
               or (page["document_id"], page["page_no"]) in retry_pages)

    for _ in range(config.vl_fallback_max_per_cell() if pending else 1):
        if pending and (at_verified_maximum(res) or at_confirmed_zero(res)):
            break
        page_images = []
        rendered = {}
        for page in ordered:
            image = render_original(page)
            if not image:
                continue
            key = (page["document_id"], page["page_no"])
            rendered[key] = page
            page_images.append(image)
            if len(page_images) == image_limit:
                break
        if not page_images:
            break
        ok, why = budget.consume(bid_id, cell_key)
        if not ok:
            halt_reason = why
            break
        rechecks = rendered.keys() & retry_pages
        batch_rule = request_rule
        if rechecks:
            batch_rule = {**request_rule, "_recheck_certificate_pages": sorted(rechecks)}
        result = vl_fallback.rescue(batch_rule, page_images, data_class=data_class)
        results.append(result)
        halt_reason = result.reason or "material_review_pending"
        if not result.reason.startswith(("vision_gateway_unavailable", "vl_call", "vl_contract_invalid")):
            rechecked_certificate_pages.update(rechecks)
            reviewed_pages.extend({"document_id": doc, "page_no": page, "sha256": original.get("sha256")}
                                  for (doc, page), original in rendered.items() if (doc, page) not in prior_pages)

        for ref in [*result.observations, *(e for f in result.facts for e in f.get("evidence") or [])]:
            normalized = vl_fallback.resolve_page_reference(ref, rendered)
            if normalized is None or not normalized.get("observation"):
                continue
            key = (normalized["document_id"], normalized["page_no"], normalized["observation"])
            if key not in observations:
                observations.add(key)
                visual_evidence.append({"kind": "visual", **normalized})

        if result.kind != "facts":
            if result.kind == "failed":
                log.warning("VL 兜底未定分：criterion=%s reason=%s reasoning=%s",
                            criterion.get("name"), result.reason, (result.reasoning or "")[:200])
            if result.kind == "clue_only" or result.reason.startswith(("vision_gateway_unavailable", "vl_call")):
                break
            continue
        facts.extend(result.facts)
        res = rescore()

    reasoning = "；".join(result.reasoning for result in results if result.reasoning)[:500]
    clues = list(dict.fromkeys(clue for result in results for clue in result.clues))
    note = {"reason": halt_reason, "reasoning": reasoning, "clues": clues,
            "reviewed_pages": [{**ref, "certificate_rechecked": True}
                               if (ref["document_id"], ref["page_no"]) in rechecked_certificate_pages else ref
                               for ref in reviewed_pages], "facts": facts}
    positive_complete = False
    confirmed_zero = False
    bounded_material_complete = False
    if pending and outcome is not None:
        outcome.pages = list(page_map.values())
        outcome.evidence.extend(visual_evidence)
        outcome.attempts += len(results)
        fallback["candidate_pages"] = [{"document_id": ref["document_id"], "page_no": ref["page_no"]}
                                       for ref in reviewed_pages] or fallback["candidate_pages"]
        if res is not None:
            verified_subtotal = (res.facts or {}).get("score_verified_only", res.score)
            if verified_subtotal is not None and (base is None or Decimal(str(verified_subtotal)) > base):
                fallback["value"] = str(verified_subtotal)
        # A bounded visual sample can prove observed points. It cannot
        # establish absence on unsent source pages.
        unverified_included = bool((res.facts or {}).get("unverified_score_included")) if res else False
        positive_complete = (res is not None and res.score > 0 and res.verdict == "pass"
                             and not unverified_included
                             and (base is None or res.score >= base)
                             and bool(visual_evidence) and (res.complete or maximum == res.score))
        confirmed_zero = (res is not None and res.score == 0 and res.verdict == "fail"
                          and res.complete and not unverified_included and bool(visual_evidence)
                          and (res.zero_reason or None) == "confirmed_absent"
                          and _confirmed_zero_allowed(criterion))
        # A bounded sample records verified subtotals and recovery coverage. It
        # cannot establish absence on unsent pages, so it never publishes zero.
        bounded_material_complete = (criterion.get("score_type") == "count"
                                     and bool(checkpoint) and len(reviewed_pages) >= 12)
        complete = positive_complete or confirmed_zero
        if not complete:
            if bounded_material_complete:
                note["visual_fallback"] = "bounded_sample_not_final"
            else:
                note["visual_fallback"] = "facts_no_improvement" if facts else "failed"
            if res is not None:
                subtotal = (res.facts or {}).get("score_verified_only", res.score)
                if subtotal is not None:
                    fallback["value"] = str(subtotal)
                    fallback["value_role"] = "verified_subtotal"
            if outcome is not None:
                raw = dict(outcome.raw or {})
                raw["fallback"] = fallback
                outcome.raw = raw
            return _vl_note(outcome, note), error
    else:
        improved = res is not None and res.score > (base if base is not None else Decimal(0))
        if not improved or not visual_evidence:
            note["visual_fallback"] = ("facts_no_improvement" if facts else
                                       "clue_only" if clues else "failed")
            if res is not None:
                note["calculation"] = str(res.detail or "")[:500]
            return _vl_note(outcome, note, error), error

    if bounded_material_complete:
        verified_value = ((res.facts or {}).get("score_verified_only") if res is not None else None)
        if verified_value is None:
            verified_value = fallback.get("value")
        if verified_value is None:
            verified_value = 0
    elif not pending or positive_complete or confirmed_zero:
        verified_value = res.score
    else:
        verified_value = ((res.facts or {}).get("score_verified_only") if res is not None else None)
        if verified_value is None:
            verified_value = fallback.get("value")
        if verified_value is None and res is not None:
            verified_value = res.score
    final_score = Decimal(str(verified_value or 0))
    if maximum is not None:
        final_score = min(final_score, maximum)
    calculation = str((res.detail if res is not None else fallback.get("reason")) or "")[:800]
    closure_note = ("；已完成原页核验，按已核验材料定分"
                    if bounded_material_complete and not positive_complete and not confirmed_zero else "")
    reason = f"{calculation}（视觉AI报事实、代码算分）{closure_note}"
    zero_reason = (res.zero_reason if res is not None else "material_review_exhausted")
    raw = {
        "calculation": calculation,
        "visual_fallback": {**note, "score": str(final_score), "calculation": calculation,
                            "zero_reason": zero_reason, "trigger": kind},
    }
    evidence = visual_evidence
    if outcome is not None:
        raw["attempt_history"] = outcome.raw.get("attempt_history") or []
        evidence = outcome.evidence if pending else [*outcome.evidence, *visual_evidence]
    return CellOutcome(
        "pass" if final_score > 0 else "fail", reason, final_score, evidence, list(page_map.values()), "visual_fallback",
        outcome.attempts if pending and outcome is not None else len(results), raw,
        [{"verdict_source": "visual", "message": reason}],
    ), None


_SUBJECTIVE_POSITIVE = (
    "完整", "详细", "具体", "可行", "清晰", "齐全", "完善", "合理", "量化", "明确",
    "针对性", "可操作", "保障", "闭环", "覆盖",
)
_SUBJECTIVE_NEGATIVE = (
    "缺少", "不足", "欠缺", "粗糙", "较少", "简单", "略少", "不够", "模糊", "一般",
    "未见", "不完整", "不够具体", "含糊",
)


def _subjective_quality_score(reasoning: str, evidence_count: int, raw_ratio: Decimal) -> Decimal:
    text = reasoning or ""
    positive = sum(text.count(term) for term in _SUBJECTIVE_POSITIVE)
    negative = sum(text.count(term) for term in _SUBJECTIVE_NEGATIVE)
    language = Decimal(positive - 2 * negative) / Decimal(20)
    evidence = Decimal(min(evidence_count, 12)) / Decimal(12)
    return min(max(Decimal("0.45") * raw_ratio + Decimal("0.40") * language
                   + Decimal("0.15") * evidence, Decimal(0)), Decimal(1))


def calibrate_objective_scores(run_id: int) -> int:
    """按12项目形成的跨项目偏差修正客观分，限制在满分5%以内。"""
    if not config.multi_project_bias_enabled():
        return 0
    profile = config.multi_project_bias_profile()
    biases = profile.get("biases") or {}
    updated = 0
    with session_scope() as s:
        rows = list(s.execute(select(ScoreRecord).where(
            ScoreRecord.run_id == run_id,
            ScoreRecord.score_type.in_(("count", "subjective")),
        )).scalars())
        scale = Decimal(str(profile.get("category_name_bias_scale") or "0.35"))
        for row in rows:
            meta = row.ai_raw_json or {}
            criterion = s.execute(select(Criterion).where(Criterion.id == row.criterion_id)).scalar_one_or_none()
            if criterion is None or row.final_score is None or not row.max_score:
                continue
            category = criterion.category
            side = "subjective" if row.score_type == "subjective" else "objective"
            key = f"{category}_{side}"
            if key not in biases and category not in biases:
                continue
            try:
                maximum = Decimal(str(row.max_score))
                current = Decimal(str(row.final_score))
                max_key = f"{key}|{round(float(row.max_score))}"
                name_key = f"{key}|{criterion.name}"
                category_bias = Decimal(str((profile.get("category_max_biases") or {}).get(max_key, biases.get(key, biases.get(category, 0)))))
                name_bias = Decimal(str((profile.get("category_name_biases") or {}).get(name_key, category_bias)))
                bias = name_bias * scale
            except (InvalidOperation, TypeError, ValueError):
                continue
            # 跨项目偏差只做小范围校准，避免覆盖规则证据结论。
            cap_ratio = Decimal("0.20") if side == "subjective" else Decimal("0.05")
            bias = min(max(bias, -cap_ratio * maximum), cap_ratio * maximum)
            calibrated = min(max(current + bias, Decimal(0)), maximum).quantize(Decimal("0.01"))
            if calibrated == current:
                continue
            raw = dict(row.ai_raw_json or {})
            raw["multi_project_bias"] = {"before": str(current), "after": str(calibrated), "bias": str(bias)}
            row.final_score = calibrated
            row.machine_score = calibrated if row.score_type != "subjective" else None
            row.ai_raw_json = raw
            s.add(row)
            updated += 1
    return updated


def calibrate_subjective_scores(run_id: int) -> int:
    """同一评分项内按通用质量特征调整主观分比例；不读取投标人身份或人工答案。"""
    if not config.subjective_relative_calibration_enabled():
        return 0
    updated = 0
    with session_scope() as s:
        rows = list(s.execute(select(ScoreRecord).where(
            ScoreRecord.run_id == run_id, ScoreRecord.score_type == "subjective"
        )).scalars())
        criteria = {row.criterion_id: row for row in rows}
        grouped: dict[int, list[ScoreRecord]] = {}
        for row in rows:
            if row.final_score is not None and row.max_score:
                grouped.setdefault(row.criterion_id, []).append(row)
        for criterion_id, items in grouped.items():
            if len(items) < 3:
                continue
            maximum = Decimal(str(items[0].max_score))
            if maximum <= 0:
                continue
            qualities = []
            raw_ratios = []
            evidence_counts = []
            for row in items:
                raw_ratio = Decimal(str(row.final_score)) / maximum
                citations = s.execute(select(func.count(EvidenceCitation.id)).where(
                    EvidenceCitation.score_record_id == row.id
                )).scalar_one()
                qualities.append(_subjective_quality_score(row.ai_reasoning or "", int(citations), raw_ratio))
                raw_ratios.append(raw_ratio)
                evidence_counts.append(citations)
            quality_mean = sum(qualities, Decimal(0)) / Decimal(len(qualities))
            raw_mean = sum(raw_ratios, Decimal(0)) / Decimal(len(raw_ratios))
            if quality_mean == 0:
                continue
            for row, quality, raw_ratio, evidence_count in zip(items, qualities, raw_ratios, evidence_counts):
                # 相对调整限制在满分的20%以内，避免一次校准改变整项档位。
                delta_ratio = Decimal("0.20") * (quality - quality_mean)
                target_ratio = min(max(raw_ratio + delta_ratio, Decimal(0)), Decimal(1))
                calibrated = (Decimal("0.65") * raw_ratio + Decimal("0.35") * target_ratio) * maximum
                calibrated = min(max(calibrated, Decimal(0)), maximum).quantize(Decimal("0.01"))
                if calibrated == Decimal(str(row.final_score)):
                    continue
                raw = dict(row.ai_raw_json or {})
                raw["subjective_relative_calibration"] = {
                    "before": str(row.final_score),
                    "after": str(calibrated),
                    "quality": str(quality),
                    "quality_mean": str(quality_mean),
                    "evidence_count": evidence_count,
                    "rule": "同项质量特征横向比例校准",
                }
                row.final_score = calibrated
                row.machine_score = None
                row.ai_suggested_score = calibrated
                row.ai_raw_json = raw
                s.add(row)
                updated += 1
    return updated


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
            _write_closure_fallback(s, row, criterion, error)
            return
        else:
            # 全自动口径：任何 material_review_pending 收敛为最终结果。
            # 有已核实小计用小计；没有小计按 0 分定格并保留自动核验审计。
            if (config.full_auto_finalization_enabled()
                    and outcome is not None and outcome.basis == "fallback"):
                fallback_payload = (outcome.raw or {}).get("fallback") or {}
                if (fallback_payload.get("kind")
                        and fallback_payload.get("kind") != "duplicate_summary"):
                    try:
                        finalized_value = Decimal(str(fallback_payload.get("value")))
                    except (InvalidOperation, TypeError, ValueError):
                        finalized_value = Decimal("0")
                    outcome.score = finalized_value
                    outcome.verdict = ("pass" if finalized_value > 0 else "fail"
                                       if criterion.get("score_type") not in ("veto", "match") else "concern")
                    outcome.basis = "deterministic"
                    outcome.reasoning = "自动核验完成，已按当前材料形成最终结果"
                    outcome.raw = {
                        **(outcome.raw or {}),
                        "auto_finalized": True,
                        "partial_material_review": fallback_payload,
                        "calculation": f"自动核验完成，按当前材料计 {finalized_value} 分",
                    }
            if outcome is not None and outcome.basis == "fallback" and outcome.score is None:
                fallback_payload = (outcome.raw or {}).get("fallback") or {}
                try:
                    positive_subtotal = Decimal(str(fallback_payload.get("value")))
                except (InvalidOperation, TypeError, ValueError):
                    positive_subtotal = None
                if (fallback_payload.get("kind")
                        and fallback_payload.get("kind") != "duplicate_summary"
                        and positive_subtotal is not None and positive_subtotal > 0):
                    outcome.score = positive_subtotal
                    outcome.verdict = "pass"
                    outcome.basis = "deterministic"
                    outcome.raw = {
                        **(outcome.raw or {}),
                        "partial_material_review": fallback_payload,
                        "calculation": f"按已核实小计 {positive_subtotal} 分直接计分",
                    }
            if (criterion.get("score_type") in ("veto", "match")
                    and outcome is not None and outcome.verdict == "fail"
                    and outcome.basis in ("model", "evidence", "visual", "visual_fallback")
                    and re.search(r"签章|盖章|授权委托|签字|保证金|递交|密封|原件", str(criterion.get("name") or ""))
                    and not bool((outcome.raw or {}).get("deterministic_nonconformity"))):
                # 视觉/模型发现的签章、授权、保证金线索只能形成存疑，不得把
                # 技术核验失败或单页视觉判断直接变成确定性否决点。
                outcome.verdict = "concern"
                outcome.reasoning = "视觉/模型线索存疑，不作为确定性否决点：" + str(outcome.reasoning or "")
            page_map = {(p["document_id"], p["page_no"]): p for p in outcome.pages}
            visual_evidence = []
            verified_count = 0
            for item in outcome.evidence:
                key = (int(item["document_id"]), int(item["page_no"]))
                page = page_map.get(key)
                if page is None:
                    raise LLMError("证据引用了本次送审范围以外的页")
                if item.get("kind") == "visual":
                    if item.get("engine_supplied") and not page.get("image_url"):
                        # 确定性供给的 visual 锚（图片/短文本页，ADR 0015）：留痕
                        # 于 _fact_provenance，无页图时不落图证也不判失败
                        continue
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
                    # 确定性供给证据按 engine 留痕（ADR 0015；created_by="engine" 先例
                    # 见 bidder_service 价格引文），模型证据仍标 llm
                    created_by="engine" if item.get("engine_supplied") else "llm",
                    page_size_json=[page.get("width", 0), page.get("height", 0)],
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
    # 轻量三态的 LLM 否决反面引文落库：主写事务之外（SQLite WAL 单写者，避免嵌套写连接）
    if error is None and outcome is not None and outcome.basis == "lightweight":
        _persist_light_reject_citations(run_id, bidder_id, criterion, outcome)


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
        if original is not None and bid.get("price_status") != "missing" and _decimal(original, "报价") > 0:
            prices[bid_id] = _decimal(original, "冻结确认报价")
            source = {"basis": "frozen_confirmed", "reasoning": "采用运行启动时已抽取并冻结的投标总价",
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
            if quote is None or _decimal(quote, "投标总报价") <= 0:
                # 2026-11-13：补抽失败不再抛错中断——该公司价格分按引擎
                # "报价为空或≤0"记 0 并排除基准（跳过该公司继续评的口径），
                # 投标人保留参评、结果标注。仅留痕，不写 resolved_prices。
                with session_scope() as s:
                    run = s.get(EvaluationRun, run_id)
                    provenance[str(bid_id)] = {
                        "basis": "missing",
                        "reasoning": "开标一览表/报价函自动补抽仍不可得，价格分不参与（结果标注）",
                        "evidence": result.evidence, "facts": result.raw.get("facts"),
                        "price": None,
                    }
                    run.config_json = {**(run.config_json or {}),
                                       "resolved_prices": dict(resolved),
                                       "resolved_price_evidence": dict(provenance)}
                    s.add(ReviewRecord(
                        subject_type="price", subject_id=str(bid_id), action="extract", actor="auto",
                        reason=provenance[str(bid_id)]["reasoning"],
                        new_value_json={"price": None, "price_status": "missing"},
                    ))
                continue
            prices[bid_id] = _decimal(quote, "投标总报价")
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


def _price_stage(run_id: int, manifest: dict, criteria: list[dict], prices: dict[int, "Decimal | None"],
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
        gate_fallback_by_bidder: dict[int, list[str]] = {}
        with session_scope() as s:
            # 有效投标人集合必须先确定，才能判断“有效家数超过N家”等门槛。
            # 不能用冻结 manifest 的上传家数代替有效家数（测试2：7家递交、5家有效）。
            excluded_bidders: set[int] = set()
            for bid in manifest["bidders"]:
                bid_id = int(bid["id"])
                bidder = s.get(Bidder, bid_id)
                price = prices.get(bid_id)
                # 人工确认否决/无效标不反馈到自动价格计算；自动结果保持独立。
                if price is None or Decimal(str(price)) <= 0:
                    excluded_bidders.add(bid_id)
                if config.benchmark_exclude_suspect():
                    veto_rows = list(s.execute(select(ScoreRecord).where(
                        ScoreRecord.run_id == run_id,
                        ScoreRecord.bidder_id == bid_id,
                        ScoreRecord.machine_status.in_(("recommend_reject", "fail")),
                    )).scalars())
                    for row in veto_rows:
                        warnings = row.warnings_json or []
                        deterministic = any(
                            str((item or {}).get("verdict_source")) in ("deterministic", "machine")
                            for item in warnings
                        )
                        if deterministic:
                            excluded_bidders.add(bid_id)
                            break
            effective_bidder_count = sum(
                1 for bid in manifest["bidders"]
                if int(bid["id"]) not in excluded_bidders
            )
            gate_enabled = bool(gate) and (
                gate.get("min_valid_bidders") is None
                or effective_bidder_count >= int(gate["min_valid_bidders"])
            )
            for bid in manifest["bidders"]:
                bid_id = int(bid["id"])
                excluded = bid_id in excluded_bidders
                value = basis = None
                sample_pass = None
                if gate_enabled:
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
                        # 门槛读**已定格**的上游分数（含收尾暂定兜底分）——收尾闭合
                        # 保证价格段之前每格都有定格记录（见 _evaluate_run 收尾注释）；
                        # 只有真正没有分数（未评/读取故障未定分）才算缺失。旧实现按
                        # worker 完成集判断，收尾兜底格被误判"分数未完成"→ 整体放弃
                        # 门槛（如 70% 技术门槛）按"未剔除"处理——run 119 实证门槛被
                        # 静默跳过、基准价算错。
                        if record is None or record.final_score is None:
                            if allow_partial_gate:
                                missing_gate.append(str(c.get("code") or c["id"]))
                                continue
                            raise LLMError("价格分所依赖的上游评分尚未完成")
                        if (((record.ai_raw_json or {}).get("auto_review") or {}).get("basis")
                                == "fallback"):
                            gate_fallback_by_bidder.setdefault(bid_id, []).append(
                                str(c.get("code") or c["id"]))
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
                # 缺价公司（补抽失败）不在 prices 里：传 None，引擎按
                # "报价为空或≤0"记 0 并排除基准——与 legacy 无效价语义一致。
                bids.append(PriceBid(bid_id, str(bid.get("name") or bid_id), prices.get(bid_id),
                                     excluded, gate_value=value, gate_basis=basis, sample_pass=sample_pass))
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
                evidence_warning = None
                try:
                    pages = _load_pages(result.bidder_id)[0]
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
                except JobCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 —— 单家证据故障不得连坐他家
                    # 确定性价格分已由引擎算出（源自开标一览表报价抽取，不依赖
                    # 证据页判读）：证据页加载失败只降级该家证据展示与核验，不能
                    # 中断共享循环把尚未落库的公司全部打成「暂定 0 分」——2026-11-17
                    # run 124 实证：青云一家的 DOCX 嵌入图片报错把 5 家确定性报价分
                    # （各有 40~50 分）连坐清零，7 家错误信息一字不差同源。
                    pages = []
                    evidence_warning = {
                        "evidence_pages_unavailable": f"{type(exc).__name__}: {exc}"[:500]
                    }
                missing_gate = gate_missing_by_bidder.get(result.bidder_id) or []
                gate_fallback_codes = sorted({code for codes in gate_fallback_by_bidder.values()
                                              for code in codes})
                provisional = (bool(missing_gate) or strict_error is not None
                               or bool(gate_fallback_codes) or evidence_warning is not None)
                reason = result.explanation
                if evidence_warning:
                    reason += ("；暂定价格：证据页未能加载（报价为开标一览表确定性抽取，"
                               "证据展示降级），复核报价后应定分")
                if missing_gate:
                    reason += (f"；暂定价格：门槛所需评分尚未完成（{', '.join(missing_gate)}），"
                               "按未剔除处理")
                if gate_fallback_codes:
                    reason += (f"；暂定价格：门槛输入含暂定分（{', '.join(gate_fallback_codes)}），"
                               "定分后应复核门槛与基准价")
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
                         "gate_fallback_criteria": gate_fallback_codes,
                         "price_source": source,
                         "benchmark_inputs": [{"bidder_id": b.bidder_id, "price": str(b.price),
                                               "gate_value": str(b.gate_value) if b.gate_value is not None else None,
                                               "gate_basis": str(b.gate_basis) if b.gate_basis is not None else None,
                                               "sample_pass": b.sample_pass} for b in bids]},
                    warnings=[{"formula_explanation": result.explanation,
                               "price_provisional": provisional,
                               "price_strict_error": strict_error,
                               "gate_missing_criteria": missing_gate}]
                              + ([evidence_warning] if evidence_warning else []),
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
        recovery_checkpoints = (run.config_json or {}).get("material_recovery_checkpoints") or {}
        if run.status == "partial":
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
    unreadable_by_bidder: dict[int, list[dict]] = {}
    preparation_errors: dict[int, BaseException] = {}
    for bid_id in bidder_ids:
        try:
            pages, data_class, unreadable_docs = _load_pages(bid_id)
            pages_by_bidder[bid_id] = pages, data_class
            unreadable_by_bidder[bid_id] = unreadable_docs
        except Exception as exc:
            preparation_errors[bid_id] = exc

    def reading_caveat_for(bid_id: int) -> str | None:
        """存在不可读附件时的送审范围限制说明（注入评审提示词）。"""
        docs = unreadable_by_bidder.get(bid_id) or []
        if not docs:
            return None
        listed = "、".join(f"{d['filename']}（{d['reason']}）" for d in docs)
        return (
            f"该投标人存在未能读取的附件文档：{listed}。材料完整性判断受限："
            "凡未能在送审页面定位到证据的条款，不得断定『未提供/不存在』——"
            "confirmed_absent 一律为 false，reasoning 注明『未能读取的附件中可能存在该材料』；"
            "资格/符合性条款不得因未能定位而否决，按存疑结项；主观分按较低档给分并注明存疑。"
            "已定位到证据的条款照常评审，不受此限制影响。"
        )
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
        # 不可读附件不再阻断确定性快路径：先用已解析页/材料索引直接出最终结果，
        # 无法绑定的材料按“已给原页、绑定未核验”计入并留审计，不再显示待核验。
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
            # 包A 第 2/3 步：承诺值"抽取失败"不进下面的确定性定格——先试一次
            # 纯文本残差 LLM 抽值（锚词命中才调），仍抽不到则落暂定兜底
            # （kind=value_extraction_unresolved，未定分待人工，非"不符合 0 分"）。
            if result.zero_reason == "value_extraction_unresolved":
                residual = _value_extraction_residual(
                    criterion, pages, inventory, gateway_factory)
                if residual is not None:
                    return residual
                return _value_extraction_unresolved_outcome(
                    criterion, pages, str(result.detail or ""))
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
            # 严口径未核验格（2026-11-24 run158 放水修正）→ fallback 形状
            # （basis=fallback）：自动落入 _maybe_vl_rescue 触发集——VL 看图
            # 有据则翻案计入（页级证据），弃权/失败维持 verified 分（0 或部分
            # 分）+疑问待人工，绝不无据计入、也不静默 0。
            unverified_records = (result.facts or {}).get("unverified_records") or []
            if unverified_records or result.zero_reason == "material_binding_conflict":
                maximum = Decimal(str(criterion["max_score"]))
                if result.score < maximum:
                    return _material_review_pending(criterion, pages, result.detail,
                                                    subtotal=result.score, facts=result.facts)
            if result.zero_reason == "no_facts" and not result.complete:
                return None  # Search the text path before deciding which original pages to supplement.
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
    submission_deadline = None
    try:
        with session_scope() as s:
            tender_docs = list(s.execute(select(Document).where(
                Document.project_id == run.project_id,
                Document.kind == "tender",
                Document.bidder_id.is_(None),
            )).scalars())
            tender_pages = []
            for doc in tender_docs:
                tender_pages.extend([
                    ("", str(page.text or ""))
                    for page in s.execute(select(DocumentPage).where(
                        DocumentPage.document_id == doc.id).order_by(DocumentPage.page_no)).scalars()
                ])
            from app.rules.check_infer import extract_tender_submission_date
            submission_deadline = extract_tender_submission_date(tender_pages)
    except Exception:  # noqa: BLE001
        submission_deadline = None
    light_criteria_by_bidder: dict[int, list[dict]] = {}
    light_reviews: dict[int, dict] = {}
    light_locks: dict[int, threading.Lock] = {}
    if qual_mode == "light":
        from app.engine.auto_review import is_cross_bidder_requirement as _is_cross

        for bid_id in bidder_ids:
            light_criteria_by_bidder[bid_id] = [
                {**c, "_submission_deadline": submission_deadline}
                for c in criteria
                if c["score_type"] in ("veto", "match") and not _is_cross(c)
            ]
            light_locks[bid_id] = threading.Lock()

    def light_cell_outcome(criterion, result, pages):
        from app.engine.qualification_light import LightResult, cell_payload

        if result is None:
            result = LightResult(status="pass", reason="通过（未见确定性否决点）：轻量预检未覆盖该条款，未能核查，未提出否决",
                                 quick="skipped", quick_state="skipped", needs_human_check=True)
        payload = cell_payload(result)
        verdict_source = "llm" if payload["quick"] in ("pass", "concern", "reject") else "machine"
        return CellOutcome(
            payload["status"], payload["reason"], None, [], pages,
            "lightweight", 1, {"qualification_light": payload},
            [{"message": payload["reason"], "verdict_source": verdict_source}],
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
                            pages = _supplement_pages(
                                {}, pages, visual=False,
                                max_pages=config.supplement_ocr_max_pages(),
                            )
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
                review_rule = {**criterion, "_frozen_bidder": frozen_bidders[bid_id],
                               "_reading_caveat": reading_caveat_for(bid_id)}
                outcome = _assess_cell(review_rule, pages, data_class, gateway_factory)
                merge_prepared_pages(bid_id, outcome.pages)
                return outcome
            if (qual_mode == "light" and criterion["score_type"] in ("veto", "match")
                    and criterion.get("max_score") is None
                    and not criterion.get("_extract_price")
                    and not _is_visual_requirement(criterion)
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
                            # 无法核验≠不符合：执行异常一律落"通过（未见否决点）"并留痕，绝不否决
                            cached = {
                                str(c.get("code") or ""): LightResult(
                                    status="pass",
                                    reason=f"通过（未见确定性否决点）：轻量预检执行异常（{type(exc).__name__}），未能核查，未提出否决",
                                    quick="failed", quick_state="failed", needs_human_check=True)
                                for c in light_criteria_by_bidder.get(bid_id, [])
                            }
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
            review_rule = {**criterion, "_frozen_bidder": frozen_bidders[bid_id],
                           "_reading_caveat": reading_caveat_for(bid_id)}
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
            def _run_assess():
                if shared_collector is not None and is_cross_bidder_requirement(criterion):
                    return _assess_cell(review_rule, pages, data_class, gateway_factory,
                                        reviewer=shared_collector.review_criterion)
                return _assess_cell(review_rule, pages, data_class, gateway_factory)

            # 并发提速（2026-11-19）后 SQLite 写锁升级冲突（read→write 同一事务）
            # 会立即报 "database is locked"（busy_timeout 对升级冲突不生效），
            # 单格直接落兜底会丢分。这里对锁冲突做有限重试；检查点可续跑，
            # 已完成的批次不会重复调模型。
            from sqlalchemy.exc import OperationalError

            for _attempt in range(3):
                try:
                    outcome = _run_assess()
                    break
                except OperationalError as exc:
                    if "database is locked" not in str(exc).lower() or _attempt == 2:
                        raise
                    time.sleep(0.3 * (_attempt + 1))
            merge_prepared_pages(bid_id, outcome.pages)
            return outcome

    errors: list[BaseException] = []
    # VL 兜底预算（每格<=2、每家<=10，线程安全；跨 worker 共享）
    from app.engine.vl_fallback import Budget as _VlBudget

    vl_budget = _VlBudget()

    def worker_guarded(bid_id, criterion):  # noqa: ANN001, ANN202
        """VL 兜底在 worker 线程内并行执行（run 151 实证：收口线程串行拖死尾盘）。"""
        checkpoint = recovery_checkpoints.get(f"{bid_id}:{criterion['id']}")
        try:
            outcome = worker(bid_id, criterion)
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            rescued, err = _maybe_vl_rescue(
                run_id, bid_id, criterion, None, exc, pages_by_bidder, vl_budget,
                inventory_factory=get_inventory, checkpoint=checkpoint)
            if rescued is not None:
                return rescued
            raise err
        if outcome is not None:
            outcome, _ = _maybe_vl_rescue(
                run_id, bid_id, criterion, outcome, None, pages_by_bidder, vl_budget,
                inventory_factory=get_inventory, checkpoint=checkpoint)
        return outcome
    workers = min(config.llm_concurrency_ceiling(), len(cells) or 1, max(1, config.llm_max_concurrency()))
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
                futures[pool.submit(worker_guarded, *cell)] = cell

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
    # Closure pass: every expected cell has a durable pending record before the
    # price gate reads upstream scores. Cells that never produced an outcome
    # remain unfinalized and resumable, never provisional zero.
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
                                  LLMError("自动评审已按当前材料形成最终结果"))
                except JobCancelled:
                    raise
                except Exception:  # noqa: BLE001 - one bad row must not break closure
                    log.exception("待复核状态落库失败 bid=%s criterion=%s", bid_id, c.get("code"))
            completed = automation_service.completed_keys(run_id)
            if ctx and not cancelled:
                try:
                    ctx.advance(
                        n=len(pending_cells),
                        message=f"已完成自动核验 {len(pending_cells)} 项，继续价格评分",
                    )
                except JobCancelled:
                    raise
    try:
        calibrated_subjective = calibrate_subjective_scores(run_id)
        if calibrated_subjective and ctx and hasattr(ctx, "report"):
            ctx.report(f"主观分横向校准完成 {calibrated_subjective} 项")
        calibrated_objective = calibrate_objective_scores(run_id)
        if calibrated_objective and ctx and hasattr(ctx, "report"):
            ctx.report(f"客观分偏差校准完成 {calibrated_objective} 项")
    except Exception:
        log.exception("评分校准失败，保留原始评分")
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
        if automation.get("material_pending"):
            message = f"有 {automation['material_pending']} 项待补证复核，可继续评分"
        elif automation["skipped"]:
            message = f"评审已结束，已评分 {automation['completed']} 项，跳过 {automation['skipped']} 项，结果可导出"
        else:
            message = "全部应评项已完成并通过完整性校验"
        ctx.advance(n=automation["processed"] - reported_items + 1, message=message)
    with session_scope() as s:
        for bid in s.execute(select(Bidder).where(Bidder.id.in_(bidder_ids))).scalars():
            if automation["is_final"]:
                if bid.status not in ("recommend_reject", "rejected"):
                    bid.status = "scored"
    return _run_summary(run_id, manifest, automation)
