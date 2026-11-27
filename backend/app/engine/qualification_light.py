"""Lightweight qualification review: deterministic precheck + one screen per bidder.

Qualification/eligibility is deliberately *not* a deep review here:
- deterministic primitives (run_check / extract_bid_facts) run first, no LLM;
- unresolved clauses are sent to exactly one consolidated LLM screen per bidder;
- no OCR, no images, no per-criterion review loop;
- conclusions are advisory only and never block scoring, ranking or price.

**Binary reject-screen since schema v3** (2026-11-23, 决策 0017): every clause
settles as ``pass`` / ``fail`` — displayed 通过 / 存疑（建议否决，需人工确认）.
``pass`` means "no definite reject point found" (未见确定性否决点), *not*
"conformity confirmed"; ``fail`` means "a definite reject point was found"
and is only a rejection *suggestion* (AI 不裁决否决，不阻断计分/排名/价格).

Reject points come from affirmative counter-facts only (误否决绝对不可接受):
- deterministic fact contradictions — extracted value/date/count/math vs the
  frozen rule text — → ``fail`` (reject_source=deterministic);
- an LLM screen may suggest reject **only** with verbatim counter-evidence
  re-verified locally against the reviewed pages (reject_source=llm).

Everything else falls to ``pass``: 材料/短语未见（缺证据≠不符合）, scanned
pages, visual/external items (签章、密封、递交时间…), quick-screen failure,
unverified reject quotes — none of these may suggest rejection. Legacy v1/v2
rows (pass/fail/concern/not_deeply_reviewed) keep their display unchanged.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
import re

from pydantic import BaseModel, ConfigDict

from app import config
from app.engine.facts import BidFacts, extract_bid_facts
from app.engine.objective import run_check

SCHEMA_VERSION = 3
PROMPT_VERSION = "2026-11-23.qual-light.3"
RULE_TEXT_LIMIT = 300
MAX_REJECT_QUOTES = 5

SYSTEM_PROMPT = """你是招标评审的资格/符合性轻量预检助手。这不是完整资格审查，只做一次合并快审，目标是筛查"确定性否决点"。
你的结论中只有 reject 会被采纳为"建议否决（存疑，需人工确认）"；pass/concern 仅作人工参考线索，不构成资格结论。
输入包含：投标人结构化事实、已解析的投标文件页面文本（可能是节选）、待审条款列表。
对每个条款 code 输出且只输出一个 status：
- pass：材料/事实初步支持符合，或未见反面事实；
- concern：存在疑点但拿不出逐字反面原文、信息不足、材料缺失、无法读取或需要人工核对；
- reject：材料中存在明确反面事实、可逐字引用原文证明不符合（如资质等级低于要求、承诺明确不满足条款原文）。
reject 标准必须极度克制、宁漏勿错杀（误否决绝对不可接受）：只有反面事实清晰无歧义、且能给出逐字反面原文时才可 reject；
材料缺失、抽取不到、扫描件不可读、仅有推断或无法确定，一律不得 reject（缺证据≠不符合）。
reject 必须附 evidence 数组：反面原文逐字摘录（≥10 字，不得改写、不得编造、不得省略号截断关键断言），
每条含 quote、document_id、page_no（照抄输入 pages 中的值）；引文必须能在输入页面文本中逐字找到。
禁止编造原文、材料或页码；目录、模板、投标人自述不能当作证明材料。
若 input_complete=false（输入为节选），任何“材料缺失”结论只能给 concern，不得给 pass；
但已有逐字反面原文的 reject 不受此限（引文本身即证明）。
签章、盖章、签字、照片、现场密封、递交时间、平台记录等需要原件/视觉/外部记录的事项，一律给 concern。
严格输出 JSON：{"items":[{"code":"条款code","status":"pass|concern|reject","reason":"简短理由",
"evidence":[{"quote":"逐字原文","document_id":1,"page_no":3}]}]}
每个待审条款都必须出现一次；没有把握就写 concern。"""


class _QuickEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    quote: str = ""
    document_id: int = 0
    page_no: int = 0


class _QuickItem(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    code: str = ""
    status: str = ""
    reason: str = ""
    evidence: list[_QuickEvidence] = []


class _QuickReview(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[_QuickItem] = []


@dataclass
class LightResult:
    status: str                    # pass|fail（二值：pass=未见确定性否决点；fail=存疑/建议否决，需人工确认）
    reason: str
    deterministic: str = "unknown"  # pass|fail|concern|unknown
    quick: str = "skipped"          # pass|concern|reject|unknown|skipped|failed
    quick_state: str = "skipped"    # completed|skipped|failed
    input_complete: bool = True
    reject_source: str = ""         # ""|deterministic|llm
    evidence_items: list = field(default_factory=list)  # llm 否决的已逐字核验反面引文
    needs_human_check: bool = False  # pass 格里"AI 未真正查验"的（签章/未覆盖/快审失败/引文未核验）


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
            for page in select_candidate_pages(criterion, ordered, max_pages=6):
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


def _verified_counter_quotes(evidence, pages: list[dict]) -> list[dict]:
    """LLM reject 反面引文的本地逐字核验闸门。

    不信任 LLM 给的页码（只作 hint）：引文必须在送审页文本中逐字命中
    （归一化空白后子串），命中处的 document_id/page_no 才是落库位置；
    找不到即视为编造/改写，整条作废。返回 ≤MAX_REJECT_QUOTES 条
    ``{quote, document_id, page_no}``。
    """
    from app.parsing.normalize import normalize_no_ws

    page_index = [
        (int(p.get("document_id") or 0), int(p.get("page_no") or 0),
         normalize_no_ws(str(p.get("text") or "")))
        for p in pages
    ]
    verified: list[dict] = []
    seen: set[str] = set()
    for ev in evidence or []:
        if len(verified) >= MAX_REJECT_QUOTES:
            break
        quote = str((ev or {}).get("quote") or "").strip()
        if len(quote) < 8:
            continue
        needle = normalize_no_ws(quote)
        if not needle or needle in seen:
            continue
        claimed = (int((ev or {}).get("document_id") or 0), int((ev or {}).get("page_no") or 0))
        hit = None
        for doc, page, text in page_index:
            if needle in text and (doc, page) == claimed:
                hit = (doc, page)
                break
        if hit is None:
            for doc, page, text in page_index:
                if needle in text:
                    hit = (doc, page)
                    break
        if hit is None:
            continue
        seen.add(needle)
        verified.append({"quote": quote, "document_id": hit[0], "page_no": hit[1]})
    return verified


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
            params = dict(criterion.get("params") or {})
            name = str(criterion.get("name") or "")
            text = _page_text(pages)
            if "分项报价表" in name:
                table_hits = re.findall(r"(?:投标分项报价表|分项报价表)[\s\S]{0,1200}", text)
                has_content = any(re.search(r"合计|总计|投标总价|单价|数量|金额", hit) for hit in table_hits)
                primitive = (run_check("document_present", {**params, "document": "投标分项报价表", "require_content": True},
                                        facts, criterion=criterion) if has_content else None)
                if primitive is None:
                    from app.engine.models import PrimitiveResult
                    primitive = PrimitiveResult("fail", "未按要求提供投标分项报价表")
                det = "fail" if primitive.status == "fail" else "pass"
            elif ("授权委托书" in name and
                  (criterion.get("_submission_deadline") or params.get("not_after"))):
                deadline = criterion.get("_submission_deadline") or params.get("not_after")
                primitive = run_check("date_valid", {
                    **params, "document": "授权委托书", "not_after": deadline,
                }, facts, criterion=criterion)
                det = _deterministic_state(primitive)
            else:
                primitive = run_check(
                    check_type, params, facts, criterion=criterion
                )
                det = _deterministic_state(_apply_scan_hint(primitive, check_type, scan_hint)) if primitive else "unknown"
        except Exception:  # noqa: BLE001 - a broken primitive must not fail the run
            primitive = None
            det = "unknown"
        visual = _is_visual_requirement(criterion)
        external = is_external_requirement(criterion)
        results[code] = LightResult(
            status="pass",  # 占位；第二循环归并必覆盖（二值：pass|fail）
            reason="",
            deterministic=det,
            quick="skipped",
            quick_state="skipped",
        )
        # R45-B：pass+need_human = 范围内检查因缺输入没跑（如限价未解析到）——
        # 第二循环据此标 needs_human_check（"残留在通过里必须可见"），不进 pending
        if primitive is not None and primitive.status == "pass" and primitive.need_human:
            results[code].needs_human_check = True
        if det == "unknown" and not visual and not external:
            pending.append(criterion)
        results[code].reason = ("确定性预检：" + (primitive.message if primitive else "预检异常"))[:500]

    quick_state = "skipped"
    input_complete = True
    quick_map: dict[str, tuple[str, str, list]] = {}
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
                raw = str(item.status or "").strip().lower()
                if raw == "pass":
                    status = "pass"
                elif raw == "reject":
                    status = "reject"
                else:
                    # unknown / 乱码 / 旧词一律降为 concern 线索（归并阶段只作参考，不作为否决点）
                    status = "concern"
                evidence = [
                    {"quote": e.quote, "document_id": e.document_id, "page_no": e.page_no}
                    for e in (item.evidence or [])
                ]
                quick_map[str(item.code or "")] = (status, item.reason or "", evidence)

    for criterion in criteria:
        code = str(criterion.get("code") or "")
        result = results[code]
        quick_status, quick_reason, quick_evidence = quick_map.get(code, ("", "", []))
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
        # 二值化（决策 0017）：fail=发现确定性否决点（显示"存疑/建议否决"，需人工确认）；
        # 其余一律 pass=未见确定性否决点。缺证据、看不见、核验不了都不作为否决依据。
        if result.deterministic in ("fail", "concern"):
            # 事实与规则原文矛盾（数值门槛/有效期越窗/计数不足/报价算术矛盾）→
            # 确定性否决点。math 冲突（concern）经 R45-B 范围过滤后只可能来自
            # 条款原文覆盖的检查，不再有跨条款归因的假矛盾；前缀与 ADR0017 第3条
            # 措辞一致（原"数学关系矛盾——"对超限价场景语义不准）。
            result.status = "fail"
            result.reject_source = "deterministic"
            result.reason = "建议否决（仅建议，需人工确认，不阻断计分/排名）：事实与规则原文矛盾——" + result.reason
        elif visual or external:
            # 签章/原件/视觉/外部记录事项 AI 不查验 → 未见否决点，需人工核对
            result.status = "pass"
            result.needs_human_check = True
            result.reason = "通过（未见确定性否决点）：签章/原件/视觉/外部记录事项 AI 不查验，需人工核对"
        elif result.deterministic == "pass":
            result.status = "pass"
            if result.needs_human_check:
                # R45-B：范围检查缺输入未执行（限价未解析/大写未抽到）→
                # 如实注记预检文案，不冒充"已核对符合"
                result.reason = (
                    "通过（未见确定性否决点）："
                    + result.reason.removeprefix("确定性预检：")
                    + "（轻量预检，不构成完整资格审查）"
                )
            else:
                result.reason = "通过（确定性预检符合，轻量预检，不构成完整资格审查）"
        elif quick_status == "reject":
            verified = _verified_counter_quotes(quick_evidence, pages)
            if verified:
                result.status = "fail"
                result.reject_source = "llm"
                result.evidence_items = verified
                result.reason = ("建议否决（仅建议，需人工确认，不阻断计分/排名）：快审发现明确反面事实——"
                                 + quick_reason[:400])
            else:
                # 反面引文缺失/无法逐字核验（可能编造）→ 不作为否决点，线索保留
                result.status = "pass"
                result.needs_human_check = True
                result.reason = ("通过（未见确定性否决点）：快审拟判不符合但反面引文未能逐字核验，"
                                 "线索保留——" + quick_reason[:400])
        elif quick_status == "pass":
            result.status = "pass"
            # 节选 pass 的覆盖有限：结论仍是 pass，但归入待人工核对（误放须可见）
            result.needs_human_check = not input_complete
            cover = "" if input_complete else "（材料为节选，快审覆盖有限）"
            result.reason = "通过（未见确定性否决点）：轻量快审未发现反面事实" + cover + "——" + quick_reason[:300]
        elif quick_status == "concern":
            result.status = "pass"
            # 存疑线索保留 → 待人工核对（prompt 本身就要求"需要人工核对"落 concern）
            result.needs_human_check = True
            result.reason = "通过（未见确定性否决点）：快审存疑线索但无确凿反面事实，线索保留——" + quick_reason[:300]
        else:
            # 兜底：快审失败/未给出结论/未覆盖 → 未见否决点，不提出否决
            result.status = "pass"
            result.needs_human_check = True
            if quick_state == "failed":
                detail = "快审调用失败，未能核查"
            elif quick_state == "skipped":
                detail = "快审未执行"
            elif not input_complete:
                detail = "材料为节选，快审覆盖有限"
            else:
                detail = "快审未给出结论"
            result.reason = f"通过（未见确定性否决点）：{detail}，未提出否决"

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
        "reject_source": result.reject_source or None,
        "reject_quotes": result.evidence_items,
        "needs_human_check": result.needs_human_check,
    }
