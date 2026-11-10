"""Lightweight qualification review: deterministic precheck + one screen per bidder.

Qualification/eligibility is deliberately *not* a deep review here:
- deterministic primitives (run_check / extract_bid_facts) run first, no LLM;
- unresolved clauses are sent to exactly one consolidated LLM screen per bidder;
- no OCR, no images, no per-criterion review loop, no evidence citations;
- conclusions are advisory only and never block scoring, ranking or price.

Statuses are intentionally distinct from the scoring vocabulary:
``pass`` / ``fail`` / ``concern`` / ``not_deeply_reviewed``.  A clause that was
not actually reviewed is never reported as a pass.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from app import config
from app.engine.facts import BidFacts, extract_bid_facts
from app.engine.objective import run_check

SCHEMA_VERSION = 1
PROMPT_VERSION = "2026-11-10.qual-light.1"
RULE_TEXT_LIMIT = 300

SYSTEM_PROMPT = """你是招标评审的资格/符合性轻量预检助手。这不是完整资格审查，只做一次合并快审。
输入包含：投标人结构化事实、已解析的投标文件页面文本（可能是节选）、待审条款列表。
对每个条款 code 输出且只输出一个 status：
- pass：材料/事实初步支持符合；
- concern：存在疑点、信息不足或需要人工核对；
- unknown：无法判断。
禁止输出 fail。禁止编造原文、材料或页码；目录、模板、投标人自述不能当作证明材料。
若 input_complete=false（输入为节选），任何“材料缺失”结论只能给 unknown 或 concern，不得给 pass。
签章、盖章、签字、照片、现场密封、递交时间、平台记录等需要原件/视觉/外部记录的事项，一律给 unknown。
严格输出 JSON：{"items":[{"code":"条款code","status":"pass|concern|unknown","reason":"简短理由"}]}
每个待审条款都必须出现一次；没有把握就写 unknown。"""


class _QuickItem(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    code: str = ""
    status: str = ""
    reason: str = ""


class _QuickReview(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[_QuickItem] = []


@dataclass
class LightResult:
    status: str                    # pass|fail|concern|not_deeply_reviewed
    reason: str
    deterministic: str = "unknown"  # pass|fail|concern|unknown
    quick: str = "skipped"          # pass|concern|unknown|skipped|failed
    quick_state: str = "skipped"    # completed|skipped|failed
    input_complete: bool = True


_DETERMINISTIC_CHECKS = ("document_present", "contains_phrase", "value_in_range",
                         "count_at_least", "date_valid", "math_relation", "human_only")


def _scan_hint(pages: list[dict]) -> bool:
    return any(
        p.get("unreadable") or p.get("requires_vision")
        or str(p.get("source") or "text").lower() != "text"
        for p in pages
    )


def _apply_scan_hint(result, check_type: str, scan_hint: bool):
    """Scan-only pages cannot prove a textual clause is missing (legacy parity)."""
    from app.engine.models import PrimitiveResult

    if (scan_hint and result.status == "fail" and not result.need_human
            and check_type in ("document_present", "contains_phrase", "human_only")):
        return PrimitiveResult(
            status="unknown",
            message=(result.message or "") + "；标书含扫描件/OCR 页，文本层不可见可能导致误判，不作为资格结论",
            evidence_quote=result.evidence_quote,
            evidence_items=result.evidence_items,
            need_human=True,
        )
    return result


def _deterministic_state(result) -> str:
    if result.status == "pass":
        return "pass"
    if result.status == "fail":
        return "concern" if result.need_human else "fail"
    return "unknown"


def _page_text(pages: list[dict]) -> str:
    return "\n".join(str(p.get("text") or "") for p in pages)


def _rule_payload(criterion: dict) -> dict:
    def cut(value) -> str:
        text = str(value or "")
        return text[:RULE_TEXT_LIMIT]

    return {
        "code": criterion.get("code"),
        "name": cut(criterion.get("name")),
        "description": cut(criterion.get("description")),
        "source_quote": cut(criterion.get("source_quote")),
        "check_type": criterion.get("check_type") or "human_only",
        "params": criterion.get("params") or {},
    }


def _facts_summary(facts: BidFacts) -> dict:
    from app.engine.auto_judge import _facts_summary as summary

    return summary(facts)


def _build_pages_digest(criteria: list[dict], pages: list[dict], budget: int) -> tuple[list[dict], bool]:
    """Return bounded page digests; never OCR, never send images."""
    ordered = [p for p in pages if not p.get("known_blank")]
    total = sum(len(str(p.get("text") or "")) for p in ordered)
    complete = total <= budget
    if complete:
        chosen = ordered
    else:
        from app.engine.auto_review import select_candidate_pages

        seen: set[tuple[int, int]] = set()
        chosen = []
        for criterion in criteria:
            for page in select_candidate_pages(criterion, ordered, max_pages=4):
                key = (int(page["document_id"]), int(page["page_no"]))
                if key not in seen:
                    seen.add(key)
                    chosen.append(page)
        chosen.sort(key=lambda p: (int(p["document_id"]), int(p["page_no"])))
    digest: list[dict] = []
    used = 0
    for page in chosen:
        text = str(page.get("text") or "")
        remaining = budget - used
        if remaining <= 0:
            complete = False
            break
        if len(text) > remaining:
            text = text[:remaining]
            complete = False
        digest.append({
            "document_id": int(page["document_id"]),
            "page_no": int(page["page_no"]),
            "text": text,
        })
        used += len(text)
    return digest, complete


def _call_quick_review(gateway, payload: dict, data_class: str) -> _QuickReview | None:
    from app.llm.request_sizing import ReviewContextExceeded, assert_request_fits

    user = json.dumps(payload, ensure_ascii=False, default=str)
    try:
        assert_request_fits(SYSTEM_PROMPT, user)
    except ReviewContextExceeded:
        # Retry once with half the page material; never truncate silently.
        pages = payload.get("pages") or []
        half = max(1, len(pages) // 2)
        payload = {**payload, "pages": pages[:half], "input_complete": False}
        user = json.dumps(payload, ensure_ascii=False, default=str)
        try:
            assert_request_fits(SYSTEM_PROMPT, user)
        except ReviewContextExceeded:
            return None
    review, _ = gateway.chat_json(
        SYSTEM_PROMPT, user, _QuickReview,
        data_class=data_class, prompt_version=PROMPT_VERSION,
    )
    return review


def review_bidder_qualification(criteria: list[dict], pages: list[dict], data_class: str,
                                gateway=None) -> dict[str, LightResult]:
    """One deterministic precheck + one consolidated LLM screen for this bidder."""
    from app.engine.auto_review import is_external_requirement
    from app.services.automatic_scoring_service import _is_visual_requirement

    facts = extract_bid_facts(_page_text(pages))
    scan_hint = _scan_hint(pages)
    results: dict[str, LightResult] = {}
    pending: list[dict] = []

    for criterion in criteria:
        code = str(criterion.get("code") or "")
        check_type = str(criterion.get("check_type") or "human_only")
        if check_type not in _DETERMINISTIC_CHECKS:
            check_type = "human_only"
        try:
            primitive = run_check(check_type, criterion.get("params") or {}, facts)
        except Exception:  # noqa: BLE001 - a broken primitive must not fail the run
            primitive = None
        det = _deterministic_state(_apply_scan_hint(primitive, check_type, scan_hint)) if primitive else "unknown"
        visual = _is_visual_requirement(criterion)
        external = is_external_requirement(criterion)
        results[code] = LightResult(
            status="not_deeply_reviewed",
            reason="",
            deterministic=det,
            quick="skipped",
            quick_state="skipped",
        )
        if det == "unknown" and not visual and not external:
            pending.append(criterion)
        # Visual/external clauses never receive an LLM "pass" here; they remain advisory.
        results[code].reason = ("确定性预检：" + (primitive.message if primitive else "预检异常"))[:500]

    quick_state = "skipped"
    input_complete = True
    quick_map: dict[str, tuple[str, str]] = {}
    if pending and gateway is not None:
        budget = config.qualification_quick_chars()
        digest, input_complete = _build_pages_digest(pending, pages, budget)
        payload = {
            "bidder": None,
            "input_complete": input_complete,
            "facts": _facts_summary(facts),
            # Only unresolved clauses are sent: deterministic pass/fail/concern
            # and visual/external clauses are already settled without the LLM.
            "rules": [_rule_payload(c) for c in pending],
            "pages": digest,
        }
        try:
            review = _call_quick_review(gateway, payload, data_class)
        except Exception:  # noqa: BLE001 - infrastructure failure is not a verdict
            quick_state = "failed"
            review = None
        else:
            quick_state = "completed" if review is not None else "skipped"
        if review is not None:
            for item in review.items:
                status = item.status if item.status in ("pass", "concern", "unknown") else "unknown"
                quick_map[str(item.code or "")] = (status, item.reason or "")

    for criterion in criteria:
        code = str(criterion.get("code") or "")
        result = results[code]
        quick_status, quick_reason = quick_map.get(code, ("unknown", ""))
        result.quick_state = quick_state
        result.input_complete = input_complete
        if quick_state == "failed":
            result.quick = "failed"
        elif code in quick_map:
            result.quick = quick_status
        else:
            result.quick = "skipped" if not pending else "unknown"

        visual = _is_visual_requirement(criterion)
        external = is_external_requirement(criterion)
        if result.deterministic == "fail":
            result.status = "fail"
            result.reason = "确定性预检不符合（仅报告，不阻断计分/排名）：" + result.reason
        elif result.deterministic == "concern":
            result.status = "concern"
            result.reason = "确定性预检存在冲突，建议人工核对：" + result.reason
        elif visual or external:
            result.status = "not_deeply_reviewed"
            result.reason = ("未深评（需原件/视觉/外部记录）：" + result.reason)[:500]
        elif result.deterministic == "pass":
            if quick_status == "concern":
                result.status = "concern"
                result.reason = "预检通过但快审存疑：" + quick_reason[:400]
            else:
                result.status = "pass"
                result.reason = "确定性预检通过（轻量预检，未做完整资格审查）"
        elif quick_status == "pass":
            if input_complete:
                result.status = "pass"
                result.reason = "轻量快审通过（仅基于已解析材料，不构成完整审查）：" + quick_reason[:400]
            else:
                result.status = "concern"
                result.reason = "仅节选材料快审通过，未深评：" + quick_reason[:400]
        elif quick_status == "concern":
            result.status = "concern"
            result.reason = "轻量快审存疑：" + quick_reason[:400]
        else:
            result.status = "not_deeply_reviewed"
            if quick_state == "failed":
                detail = "快审调用失败"
            elif not input_complete:
                detail = "材料为节选"
            else:
                detail = "快审未给出结论"
            result.reason = f"未深评（{detail}）：" + result.reason

    return results


def cell_payload(result: LightResult) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": result.status,
        "reason": result.reason[:1000],
        "deterministic": result.deterministic,
        "quick": result.quick,
        "quick_state": result.quick_state,
        "input_complete": result.input_complete,
        "method": "deterministic+quick",
    }
