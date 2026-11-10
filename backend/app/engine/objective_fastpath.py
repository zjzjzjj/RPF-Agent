# -*- coding: utf-8 -*-
"""Deterministic objective scoring from a page-bound material inventory.

These plans are derived from the frozen rule text (name/description/quote and
params), never from a project-specific criterion code.  The LLM remains the
fallback for rules that cannot be mapped safely; a mapped plan always yields a
numeric score (0 with a reason when nothing qualifying was found).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.engine.material_inventory import (
    CertRecord, ContractRecord, MaterialInventory, PersonRecord,
)
from app.engine.objective import normalize_cert
from app.rules.check_infer import infer_count_params, infer_objective_route


@dataclass
class FastPathResult:
    score: Decimal
    detail: str
    verdict: str
    facts: dict[str, Any]
    pages: list[int]
    zero_reason: str | None = None
    confidence: str = "high"
    complete: bool = True
    warnings: list[dict] = field(default_factory=list)


@dataclass
class ObjectivePlan:
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    labels: list[str] = field(default_factory=list)


_RE_AMOUNT_TIER = re.compile(
    r"合同(?:金额|价款)?\s*(\d+(?:\.\d+)?)\s*万\s*(?:元)?\s*(?:及)?以上"
    r"[^。；]{0,40}?(?:每个|每份|每项|每)\s*得\s*(\d+(?:\.\d+)?)\s*分"
)
_RE_AMOUNT_TIER_GT = re.compile(
    r"合同(?:金额|价款)?\s*超\s*(?:过)?\s*(\d+(?:\.\d+)?)\s*万"
    r"[^。；]{0,40}?(?:每个|每份|每项|每)\s*得\s*(\d+(?:\.\d+)?)\s*分"
)
_RE_CAP_SCORE = re.compile(r"最高(?:不超过|得|为)?\s*(\d+(?:\.\d+)?)\s*分")
_RE_PER_PERSON = re.compile(r"每(?:人|位|名)\s*得\s*(\d+(?:\.\d+)?)\s*分")
_RE_YEARS_MIN = re.compile(r"(\d+)\s*年(?:以上|及以上|或以上)")
_RE_SENIOR_SCORE = re.compile(r"高级工程师[^。；]{0,20}?得\s*(\d+(?:\.\d+)?)\s*分")
_RE_MID_SCORE = re.compile(r"(?:中级工程师|工程师中级|工程师)[^。；]{0,20}?得\s*(\d+(?:\.\d+)?)\s*分")
_RE_TWO_THREE_TIER = re.compile(
    r"([一二两三四五六七八九十\d]+)\s*项[^。；]{0,12}?得\s*(\d+(?:\.\d+)?)\s*分"
)

_EQUIPMENT_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("大屏", ("大屏", "LED")),
    ("中控", ("中控",)),
    ("投影", ("投影",)),
    ("调音台", ("调音台", "调音")),
    ("音箱", ("音箱",)),
    ("功放", ("功放",)),
    ("数字音频", ("数字音频",)),
    ("灯光", ("灯光",)),
)
_SPECIAL_CERTS = ("电工证", "特种作业操作证", "高处作业", "登高证")


def _rule_text(criterion: dict) -> str:
    return "\n".join(str(criterion.get(key) or "") for key in (
        "name", "description", "source_quote", "required_evidence",
        "required_artifacts", "required_artifacts_raw",
    ))


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _cap(score: Decimal, criterion: dict) -> Decimal:
    maximum = _decimal(criterion.get("max_score"))
    if maximum is not None and score > maximum:
        return maximum
    return score.quantize(Decimal("0.01"))


def _max(criterion: dict) -> Decimal | None:
    return _decimal(criterion.get("max_score"))


def _confidence_from(inventory: MaterialInventory, section_found: bool) -> str:
    if section_found:
        return "high"
    return "low"


def build_objective_plan(criterion: dict) -> ObjectivePlan | None:
    """Map a frozen rule to a deterministic plan, or None for the LLM path."""
    score_type = criterion.get("score_type")
    if score_type not in ("count", "subjective", "match"):
        return None
    if score_type == "match":
        # match with points is a compile-time anomaly; do not invent a second
        # scoring semantic here (c38 is handled as a duplicate summary).
        return None
    text = _rule_text(criterion)
    name = str(criterion.get("name") or "")
    params = dict(criterion.get("params") or {})
    expanded = f"{name}\n{text}"

    if "增值服务" in expanded:
        return ObjectivePlan("value_items", params)
    if "行业认证维修工程师" in expanded:
        return ObjectivePlan("equipment_certs", params)
    if "特种作业操作证" in expanded or ("电工" in expanded and ("登高" in expanded or "高处" in expanded)):
        return ObjectivePlan("special_certs", params)
    if "职称" in expanded:
        return ObjectivePlan("title_tier", params)
    if "驻场人员" in expanded and "经验" in expanded:
        return ObjectivePlan("person_experience", params)
    if "电子与智能化" in expanded or "音视频集成" in expanded:
        return ObjectivePlan("cert_level_sum", params)
    if ("ISO" in expanded or "体系认证" in expanded or "信息技术服务认证" in expanded) and re.search(
            r"[一二两三四五六七八九十\d]+\s*项[^。；]{0,12}?得\s*\d", expanded):
        return ObjectivePlan("cert_count_tier", params)
    if "业绩" in expanded or "合同金额" in expanded or "类似项目" in expanded:
        return ObjectivePlan("contracts", params)

    if score_type == "subjective":
        route = infer_objective_route(criterion)
        if not route:
            return None
        engine, route_params = route
        merged = {**params, **route_params}
        if engine != "count":
            merged["engine"] = engine
        if engine == "count_tier":
            return ObjectivePlan("cert_count_tier", merged)
        if engine == "attribute_tier":
            return ObjectivePlan("attribute_tier", merged)
        if engine in ("count", "value_tier", "value_linear", "deviation_deduct_table"):
            return ObjectivePlan("engine", merged)
        return None
    if params.get("engine") in (
            "count_tier", "attribute_tier", "value_tier", "value_linear",
            "deviation_deduct_table"):
        return ObjectivePlan("engine", params)
    return None


# ---------------------------------------------------------------------------
# Contract / performance
# ---------------------------------------------------------------------------
def _amount_tiers(text: str) -> list[dict[str, str]]:
    first = _RE_AMOUNT_TIER.search(text)
    second = _RE_AMOUNT_TIER_GT.search(text)
    if not first:
        return []
    low = Decimal(first.group(1)) * 10_000
    low_per = first.group(2)
    if second:
        high = Decimal(second.group(1)) * 10_000
        high_per = second.group(2)
        if high > low:
            return [
                {"min_amount_yuan": str(low), "max_amount_yuan": str(high), "per_item": low_per},
                {"min_amount_yuan": str(high), "per_item": high_per},
            ]
    return [{"min_amount_yuan": str(low), "per_item": low_per}]


def _contract_rate(tiers: list[dict[str, str]], amount: Decimal) -> Decimal:
    for tier in tiers:
        lo = Decimal(str(tier.get("min_amount_yuan", 0)))
        hi = _decimal(tier.get("max_amount_yuan"))
        if amount >= lo and (hi is None or amount < hi):
            return Decimal(str(tier.get("per_item", 0)))
    return Decimal(0)


def _contract_material_ok(record: ContractRecord, requires_material: bool) -> bool:
    if not requires_material:
        return True
    # A row in the bidder's own performance table is itself a structured claim
    # of the contract; the material chain is recorded separately and must not
    # turn an otherwise countable table row into zero.
    if record.source == "table":
        return True
    return bool(record.materials.get("invoice") or record.materials.get("acceptance")
                or record.materials.get("owner_proof") or record.materials.get("contract"))


def _score_contracts(criterion: dict, inventory: MaterialInventory) -> FastPathResult:
    text = _rule_text(criterion)
    maximum = _max(criterion) or Decimal(0)
    tiers = _amount_tiers(text)
    params = dict(criterion.get("params") or {})
    if not tiers:
        min_amount = _decimal(params.get("min_amount_yuan")) or Decimal(0)
        per_item = _decimal(params.get("per_item")) or Decimal(0)
        if per_item > 0:
            tiers = [{"min_amount_yuan": str(min_amount), "per_item": str(per_item)}]
    requires_material = any(word in text for word in ("发票", "验收", "业主证明", "用户证明"))
    qualifying: list[tuple[ContractRecord, Decimal]] = []
    rejected: list[str] = []
    for record in inventory.contracts:
        amount = record.amount_yuan
        if amount is None:
            rejected.append(f"{record.name}（无金额）")
            continue
        if tiers and amount < Decimal(str(tiers[0].get("min_amount_yuan", 0))):
            continue
        if not _contract_material_ok(record, requires_material):
            rejected.append(f"{record.name}（缺发票/验收）")
            continue
        rate = _contract_rate(tiers, amount) if tiers else Decimal(0)
        if rate <= 0:
            continue
        qualifying.append((record, rate))
    score = min(maximum, sum((rate for _record, rate in qualifying), Decimal(0)))
    if qualifying:
        detail = "；".join(
            f"{record.name} 金额 {record.amount_yuan} 元 → {rate} 分"
            f"（p{'/'.join(str(p) for p in record.pages[:3])}）"
            for record, rate in qualifying
        )
    elif rejected:
        detail = "未找到满足金额/材料要求的业绩：" + "；".join(rejected[:6])
    elif inventory.performance_section_found:
        detail = "已定位业绩章节，未检出满足金额要求的合同"
    else:
        detail = "未定位到业绩章节或合同金额"
    score = _cap(score, criterion)
    # A zero is a confirmed conclusion only when contracts were actually
    # located and every one carries a readable amount.  Otherwise let the LLM
    # path attempt the missing blocks; the closure pass is still the backstop.
    unknown_amounts = any(record.amount_yuan is None for record in inventory.contracts)
    missing_materials = any("缺发票/验收" in reason for reason in rejected)
    complete = (inventory.performance_section_found and bool(inventory.contracts)
                and not unknown_amounts and not missing_materials)
    zero_reason = None
    if score == 0:
        zero_reason = "confirmed_absent" if complete else "no_facts"
    return FastPathResult(
        score=score, detail=detail + f"；合计 {score} 分",
        verdict="pass" if score > 0 else ("fail" if complete else "concern"),
        facts={"contracts": [
            {"name": r.name, "amount_yuan": str(r.amount_yuan), "pages": r.pages,
             "materials": r.materials} for r in inventory.contracts
        ]},
        pages=sorted({p for r in inventory.contracts for p in r.pages}),
        zero_reason=zero_reason,
        confidence=_confidence_from(inventory, complete),
        complete=complete,
    )


# ---------------------------------------------------------------------------
# Personnel
# ---------------------------------------------------------------------------
def _person_experience_score(criterion: dict, inventory: MaterialInventory) -> FastPathResult:
    maximum = _max(criterion) or Decimal(0)
    minimum_years = None
    match = _RE_YEARS_MIN.search(_rule_text(criterion))
    if match:
        minimum_years = Decimal(match.group(1))
    per_person = _RE_PER_PERSON.search(_rule_text(criterion))
    per = _decimal(per_person.group(1)) if per_person else Decimal(1)
    eligible: list[PersonRecord] = []
    for person in inventory.persons:
        if not re.search(r"驻场|维保|成员|团队", person.role or ""):
            continue
        if person.years is not None and minimum_years is not None:
            if person.years >= minimum_years:
                eligible.append(person)
            continue
        if person.experience_pages or re.search(r"年|场", person.experience or ""):
            eligible.append(person)
    score = min(maximum, Decimal(len(eligible)) * (per or Decimal(0)))
    detail = (
        "驻场/团队人员经验：" + "、".join(
            f"{p.name}{('（' + str(p.years) + '年）') if p.years is not None else ''}"
            for p in eligible[:10]
        ) if eligible else "未从团队表检出满足年限的驻场/团队成员"
    )
    score = _cap(score, criterion)
    complete = inventory.personnel_table_found and inventory.personnel_detail_found
    if not eligible and any(
            "详见" in (p.experience or "") or "个人介绍" in (p.experience or "")
            for p in inventory.persons):
        # The table points at individual resumes instead of stating years;
        # let the LLM path read those pages rather than recording a hard zero.
        complete = False
    if not eligible and any(
            "负责" in (p.role or "") and p.years is not None
            and (minimum_years is None or p.years >= minimum_years)
            for p in inventory.persons):
        # Site-lead roles ("本部负责") may or may not be the clause's 驻场人员;
        # let the LLM decide instead of silently treating them as absent.
        complete = False
    zero_reason = None if score > 0 else ("confirmed_absent" if complete else "no_facts")
    return FastPathResult(
        score=score, detail=detail + f"；合计 {score} 分",
        verdict="pass" if score > 0 else ("fail" if complete else "concern"),
        facts={"persons": [{"name": p.name, "role": p.role, "years": str(p.years) if p.years else None}
                           for p in inventory.persons]},
        pages=sorted({page for p in eligible for page in p.pages}),
        zero_reason=zero_reason, complete=complete,
        confidence=_confidence_from(inventory, complete),
    )


def _person_special_certs(criterion: dict, inventory: MaterialInventory) -> FastPathResult:
    maximum = _max(criterion) or Decimal(0)
    per_person = _RE_PER_PERSON.search(_rule_text(criterion))
    per = _decimal(per_person.group(1)) if per_person else Decimal(1)
    eligible = [p for p in inventory.persons
                if any(cert in _SPECIAL_CERTS for cert in p.certs)]
    score = min(maximum, Decimal(len(eligible)) * (per or Decimal(0)))
    detail = (
        "特种作业证人员：" + "、".join(f"{p.name}（{'/'.join(p.certs)}）" for p in eligible[:10])
        if eligible else "未从团队表检出电工/登高/高处作业证"
    )
    score = _cap(score, criterion)
    zero_reason = None if score > 0 else ("confirmed_absent" if inventory.personnel_table_found else "no_facts")
    return FastPathResult(
        score=score, detail=detail + f"；合计 {score} 分",
        verdict="pass" if score > 0 else ("fail" if inventory.personnel_table_found else "concern"),
        facts={"persons": [{"name": p.name, "certs": p.certs} for p in inventory.persons]},
        pages=sorted({page for p in eligible for page in p.pages}),
        zero_reason=zero_reason,
        complete=inventory.personnel_table_found and any(p.certs for p in inventory.persons),
        confidence=_confidence_from(inventory, inventory.personnel_table_found),
    )


def _person_title_score(criterion: dict, inventory: MaterialInventory) -> FastPathResult:
    maximum = _max(criterion) or Decimal(0)
    text = _rule_text(criterion)
    senior_match = _RE_SENIOR_SCORE.search(text)
    mid_match = _RE_MID_SCORE.search(text)
    senior_value = _decimal(senior_match.group(1)) if senior_match else Decimal(1)
    mid_value = _decimal(mid_match.group(1)) if mid_match else Decimal("0.5")
    senior = [p for p in inventory.persons if p.title == "高级工程师"]
    mid = [p for p in inventory.persons if p.title == "工程师"]
    score = min(maximum, Decimal(len(senior)) * (senior_value or Decimal(0))
                + Decimal(len(mid)) * (mid_value or Decimal(0)))
    detail = (
        f"高级工程师 {len(senior)} 人、工程师 {len(mid)} 人："
        + "、".join(p.name for p in senior + mid)
        if senior or mid else "未从团队表检出高级/中级工程师职称"
    )
    score = _cap(score, criterion)
    zero_reason = None if score > 0 else ("confirmed_absent" if inventory.personnel_table_found else "no_facts")
    return FastPathResult(
        score=score, detail=detail + f"；合计 {score} 分",
        verdict="pass" if score > 0 else ("fail" if inventory.personnel_table_found else "concern"),
        facts={"persons": [{"name": p.name, "title": p.title} for p in inventory.persons]},
        pages=sorted({page for p in senior + mid for page in p.pages}),
        zero_reason=zero_reason,
        complete=inventory.personnel_table_found and any(p.title for p in inventory.persons),
        confidence=_confidence_from(inventory, inventory.personnel_table_found),
    )


def _person_equipment_score(criterion: dict, inventory: MaterialInventory) -> FastPathResult:
    maximum = _max(criterion) or Decimal(0)
    categories: dict[str, list[str]] = {}
    for person in inventory.persons:
        for cert in person.certs:
            for category, words in _EQUIPMENT_CATEGORIES:
                if any(word in cert for word in words):
                    categories.setdefault(category, []).append(f"{person.name}:{cert}")
    score = min(maximum, Decimal(len(categories)))
    detail = (
        "行业认证维修工程师设备类别：" + "；".join(
            f"{category}（{'、'.join(names[:3])}）" for category, names in categories.items()
        ) if categories else "未从团队表检出设备行业认证维修工程师"
    )
    score = _cap(score, criterion)
    zero_reason = None if score > 0 else ("confirmed_absent" if inventory.personnel_table_found else "no_facts")
    return FastPathResult(
        score=score, detail=detail + f"；合计 {score} 分",
        verdict="pass" if score > 0 else ("fail" if inventory.personnel_table_found else "concern"),
        facts={"categories": {k: v for k, v in categories.items()}},
        pages=sorted({page for p in inventory.persons for page in p.pages}),
        zero_reason=zero_reason,
        complete=inventory.personnel_table_found and any(p.certs for p in inventory.persons),
        confidence=_confidence_from(inventory, inventory.personnel_table_found),
    )


# ---------------------------------------------------------------------------
# Value-added services
# ---------------------------------------------------------------------------
def _value_items_score(criterion: dict, inventory: MaterialInventory) -> FastPathResult:
    maximum = _max(criterion) or Decimal(0)
    per_person = _RE_PER_PERSON.search(_rule_text(criterion))
    per = _decimal(per_person.group(1)) if per_person else Decimal(1)
    score = min(maximum, Decimal(len(inventory.value_items)) * (per or Decimal(0)))
    detail = (
        "增值服务清单：" + "、".join(item.name for item in inventory.value_items[:10])
        if inventory.value_items else "未在增值服务章节检出清单条目"
    )
    score = _cap(score, criterion)
    complete = inventory.value_section_found and bool(inventory.value_items)
    zero_reason = None if score > 0 else ("confirmed_absent" if complete else "no_facts")
    return FastPathResult(
        score=score, detail=detail + f"；合计 {score} 分",
        verdict="pass" if score > 0 else ("fail" if complete else "concern"),
        facts={"items": [{"name": item.name, "page": item.page} for item in inventory.value_items]},
        pages=[item.page for item in inventory.value_items],
        zero_reason=zero_reason, complete=complete,
        confidence=_confidence_from(inventory, complete),
    )


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------
def _cert_count_tier_score(criterion: dict, inventory: MaterialInventory) -> FastPathResult:
    maximum = _max(criterion) or Decimal(0)
    families: list[str] = []
    for cert in inventory.certs:
        key = cert.normalized or normalize_cert(cert.name)
        if key.startswith("ISO") and key not in families:
            families.append(key)
    # Tiers from the rule text: "两项得1分，三项得2分".
    tiers: dict[int, Decimal] = {}
    for raw_n, raw_score in _RE_TWO_THREE_TIER.findall(_rule_text(criterion)):
        n = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}.get(raw_n)
        if n is None:
            try:
                n = int(raw_n)
            except ValueError:
                continue
        value = _decimal(raw_score)
        if value is not None:
            tiers[n] = value
    score = Decimal(0)
    for minimum in sorted(tiers):
        if len(families) >= minimum:
            score = max(score, tiers[minimum])
    score = _cap(score, criterion)
    detail = (
        f"有效体系认证 {len(families)} 项：{'、'.join(families)}"
        if families else "未检出 ISO/信息技术服务管理体系认证证书"
    )
    complete = bool(inventory.certs) or inventory.cert_section_present
    zero_reason = None if score > 0 else ("confirmed_absent" if complete else "no_facts")
    return FastPathResult(
        score=score, detail=detail + f"；合计 {score} 分",
        verdict="pass" if score > 0 else ("fail" if complete else "concern"),
        facts={"certificates": families},
        pages=sorted({page for cert in inventory.certs for page in cert.pages}),
        zero_reason=zero_reason, complete=complete,
        confidence=_confidence_from(inventory, complete),
    )


def _cert_level_sum_score(criterion: dict, inventory: MaterialInventory) -> FastPathResult:
    maximum = _max(criterion) or Decimal(0)
    scores: dict[str, Decimal] = {}
    for cert in inventory.certs:
        name = cert.name or ""
        level = cert.level or ""
        value = None
        if "电子与智能化" in name:
            if level == "一级":
                value = Decimal(1)
            elif level == "二级":
                value = Decimal("0.5")
        elif "音视频集成" in name:
            if level == "一级":
                value = Decimal(1)
            elif level == "二级":
                value = Decimal("0.5")
        if value is not None:
            scores[name] = max(scores.get(name, Decimal(0)), value)
    score = min(maximum, sum(scores.values(), Decimal(0)))
    score = _cap(score, criterion)
    detail = (
        "资质等级：" + "、".join(f"{name}={value}分" for name, value in scores.items())
        if scores else "未检出电子与智能化/音视频集成资质证书等级"
    )
    complete = bool(inventory.certs) or inventory.cert_section_present
    zero_reason = None if score > 0 else ("confirmed_absent" if complete else "no_facts")
    return FastPathResult(
        score=score, detail=detail + f"；合计 {score} 分",
        verdict="pass" if score > 0 else ("fail" if complete else "concern"),
        facts={"certificates": [
            {"name": c.name, "level": c.level, "pages": c.pages} for c in inventory.certs
        ]},
        pages=sorted({page for cert in inventory.certs for page in cert.pages}),
        zero_reason=zero_reason, complete=complete,
        confidence=_confidence_from(inventory, complete),
    )


def _generic_engine_score(criterion: dict, inventory: MaterialInventory,
                         params: dict[str, Any]) -> FastPathResult | None:
    from app.services.automatic_scoring_service import _ENGINES
    from app.engine.objective import score_count

    facts = inventory.to_bid_facts()
    engine = params.get("engine")
    scoring_criterion = {
        **criterion,
        "verified_facts": {"inventory": inventory.audit},
        "cert_backing": inventory.cert_backing,
        "cert_section_present": inventory.cert_section_present,
    }
    try:
        if engine in _ENGINES:
            result = _ENGINES[engine](scoring_criterion, params, facts)
        else:
            result = score_count(scoring_criterion, params, facts)
    except Exception:  # noqa: BLE001 - unsupported shape falls back to LLM
        return None
    score = _cap(Decimal(str(result.score or 0)), criterion)
    zero_reason = getattr(result, "zero_reason", None)
    return FastPathResult(
        score=score, detail=str(result.detail or "") + f"；合计 {score} 分",
        verdict="pass" if score > 0 else "fail",
        facts={"detail": result.detail},
        pages=[],
        zero_reason=zero_reason,
        confidence="medium",
        complete=not bool(getattr(result, "unresolved", None)),
    )


def score_objective(criterion: dict, inventory: MaterialInventory) -> FastPathResult | None:
    plan = build_objective_plan(criterion)
    if plan is None:
        return None
    if plan.kind == "contracts":
        return _score_contracts(criterion, inventory)
    if plan.kind == "person_experience":
        return _person_experience_score(criterion, inventory)
    if plan.kind == "special_certs":
        return _person_special_certs(criterion, inventory)
    if plan.kind == "title_tier":
        return _person_title_score(criterion, inventory)
    if plan.kind == "equipment_certs":
        return _person_equipment_score(criterion, inventory)
    if plan.kind == "value_items":
        return _value_items_score(criterion, inventory)
    if plan.kind == "cert_count_tier":
        return _cert_count_tier_score(criterion, inventory)
    if plan.kind == "cert_level_sum":
        return _cert_level_sum_score(criterion, inventory)
    if plan.kind == "attribute_tier":
        return _generic_engine_score(criterion, inventory, plan.params)
    if plan.kind == "engine":
        return _generic_engine_score(criterion, inventory, plan.params)
    return None
