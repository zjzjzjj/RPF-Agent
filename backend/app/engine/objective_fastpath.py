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
    platform_query_details, requires_platform_certificate_query,
)
from app.engine.objective import (
    date_gate_state, date_window_from_rule, documented_project_type_state, normalize_cert,
)
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
    ("音箱", ("音箱", "音响")),
    ("功放", ("功放",)),
    ("数字音频", ("数字音频",)),
    ("灯光", ("灯光",)),
)
_SPECIAL_CERTS = ("电工证", "特种作业操作证", "高处作业", "登高证")
# P1-4（测试2 run166 c53）：细则「作业类别：电工作业和登高作业证，具备齐全的
# 每人得1分」——须同时具备电工作业族 + 登高/高处作业族两证才算齐全。旧 any()
# 数「持任一证的人」导致科通（3人各持单证）计 2、安保（3人只电工）计 2，人工
# 只认两证齐全者（科通 1、安保 0）。
_SPECIAL_CERT_ELECTRIC = ("电工", "电工作业")
_SPECIAL_CERT_CLIMBING = ("登高", "高处")

# P0-4（测试2 run166 c51）：人员主体词族——与 check_infer._LEADER_KW/_MEMBER_KW
# 同源。命中人员主体的「类似项目/业绩/合同金额」是**人员级经验档**（按项目经理
# 年限 5/4/3 年给分），不得被公司级合同金额桶（contracts）替代，否则无金额档
# →rate=0→结构性 0 分。run166 c51 五家全 0 即此根因（objective_fastpath.py:145
# 的业绩关键词分支前无此守卫，person_scope 因 c51 params=null 永不生效）。
_PERSON_SUBJECT_KW = re.compile(
    r"项目负责人|项目经理|负责人|拟派负责人|项目总监|技术负责人"
    r"|团队成员|驻场人员|人员名单|拟派人员|技术人员|维护保养服务团队"
)


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

    # 人员级证书（拟派负责人/工程师的 ITIL、PMP 等）：从人员情况表的上岗证书
    # 列确定性计数，按规则文本点名的证书族匹配；不再依赖模型逐案例产 facts
    # （run128/133 c27/c28 恒 no_facts）。
    if (score_type == "count" and params.get("person_scope")
            and params.get("field") == "certificates"):
        if not _person_cert_targets(text):
            return None
        return ObjectivePlan("person_certs", params)

    if ("应急响应" in expanded and "响应" in expanded
            and re.search(r"得\s*\d+(?:\.\d+)?\s*分", expanded)):
        return ObjectivePlan("response_commitment", params)
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
        # P0-4：人员主体命中时按人员经验年限档计分；只有无法形成人员事实时
        # 才回退到逐案例 LLM，避免公司合同金额桶吞掉项目经理经验项。
        if (params.get("person_scope") == "leader"
                or re.search(r"项目负责人|项目经理|项目总监|技术负责人|总负责人", name)):
            return ObjectivePlan("leader_experience", params)
        if params.get("person_scope") or _PERSON_SUBJECT_KW.search(name):
            return None
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
    # 测试2 run166 c39：表格行豁免不得无条件放行——materials_state 可读确认缺件
    # （contract/invoice/acceptance 全 False 且案例块完整）时按缺件处理（run158
    # 严口径），否则材料全缺的表行仍被计分（集服 6 分虚高）。
    if record.source == "table":
        ms = record.materials_state or {}
        if ms.get("_case_block_complete") and all(
                ms.get(k) is False for k in ("contract", "invoice", "acceptance")):
            return False
        return True
    return bool(record.materials.get("invoice") or record.materials.get("acceptance")
                or record.materials.get("owner_proof") or record.materials.get("contract"))


# P0-5（测试10 run113）：三件套材料链——细则点名材料 + 硬制裁句（"以上资料
# 未提供的不得分"）时逐案例 AND 核验；无制裁句不设闸（不猜）。
# 测试2 run166 c39：制裁句写作"否则不予认可"（非"不得分"）→ 旧正则漏判整段
# AND 核验不执行 → 集服材料全缺仍计 6 分。补制裁句同义族（认可/计分/得分）。
_RE_HARD_SANCTION = re.compile(
    r"(?:未提供|不提供|未按要求提供|未提交)[^。；]{0,30}不得分"
    r"|不提供或提供[^。；]{0,15}不全的不得分"
    r"|(?:否则|则|一律|均)?\s*(?:不予认可|不予计分|不予得分|不得分)"
)
_MATERIAL_NAME2KIND = {
    "合同": "contract", "中标通知书": "contract",
    "发票": "invoice", "验收": "acceptance",
    "业主证明": "owner_proof", "用户证明": "owner_proof", "使用证明": "owner_proof",
}
_RE_OR_MATERIAL = re.compile(
    r"(发票|验收|业主证明|用户证明|使用证明|中标通知书|合同)[^。；，,]{0,4}"
    r"或[^。；，,]{0,4}(发票|验收|业主证明|用户证明|使用证明|中标通知书|合同)"
)


def _hard_material_requirements(text: str) -> tuple[frozenset, frozenset]:
    """细则点名的必备材料（AND 口径，"A或B"对除外）；无硬制裁句 → 空集。

    规则文本驱动：制裁句（"…未提供的不得分"）+ 材料名单（【证明材料】段优先）
    双条件才启用硬闸门；细则未点名的材料类型一律不查（红线：不猜）。
    返回 (必备材料 kinds, OR 组 kinds——组内任一即可)。
    """
    if not _RE_HARD_SANCTION.search(text or ""):
        return frozenset(), frozenset()
    seg = text or ""
    marker = re.search(r"【?\s*证明材料\s*】?", seg)
    if marker:
        seg = seg[marker.start():]
    req: set = set()
    for name, kind in _MATERIAL_NAME2KIND.items():
        if name in seg:
            req.add(kind)
    or_group: set = set()
    m_or = _RE_OR_MATERIAL.search(seg)
    if m_or:
        or_group = {_MATERIAL_NAME2KIND[m_or.group(1)], _MATERIAL_NAME2KIND[m_or.group(2)]}
    return frozenset(req), frozenset(or_group & req)


def _missing_required_materials(
    record: ContractRecord, required: frozenset, or_group: frozenset
) -> tuple[list, list]:
    """逐案例必备材料三态核验（细则"未提供的不得分"制裁句的执行）。

    合同件：正文合同块（source=text）本身就是投标文件提供的合同复印件；
    业绩表行（source=table）只是主张，必须另有关联合同页才认。
    三态（2026-11-24 run155 实证）：True=材料在（或合同豁免）；False=可读
    确认缺（判 0，红线不放宽）；None=不可读/未绑上 → 未核实（run158 放水
    修正：严口径不计入，交 VL 翻案/人工；BID_EVAL_CASE_UNVERIFIED_COUNT=1
    回退旧"计入+疑问"）。返回 (确认缺失, 未核实)。
    """
    def state(kind: str) -> bool | None:
        if kind == "contract":
            if record.source == "text" or record.materials.get("contract"):
                return True
        st = (record.materials_state or {}).get(kind)
        if st is not None:
            return bool(st)
        return True if record.materials.get(kind) else None

    labels = {"contract": "合同", "invoice": "发票",
              "acceptance": "验收证明", "owner_proof": "业主证明"}
    satisfied_or = bool(or_group) and any(state(k) is True for k in or_group)
    missing: list = []
    unverified: list = []
    for kind in sorted(required):
        if kind in or_group and satisfied_or:
            continue
        st = state(kind)
        if st is False:
            missing.append(labels[kind])
        elif st is None:
            unverified.append(labels[kind])
    return missing, unverified


def _guard_on(inventory: MaterialInventory) -> bool:
    """PR4 护栏开关：BID_EVAL_MATERIAL_GUARD=1 且 locator 生效（on）才升级。"""
    try:
        from app import config
        from app.engine.material_locator import locator_enabled

        return config.material_guard_enabled() and locator_enabled() == "on"
    except Exception:  # noqa: BLE001
        return False


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
    # P0-5：细则有硬制裁句（"以上资料未提供的不得分"）+ 点名材料 → 逐案例
    # AND 核验（"至少一张发票"、业主证明不单独替代验收证明）；表格行不豁免。
    required_materials, or_group = _hard_material_requirements(text)
    # 三闸（2026-11-24 run159 对账）：时间窗/类型按细则明示条件剔除（剔除=不计分，
    # 非否决）；无法确认→保留+疑问。盖章：文本路无法核验≠未盖章，只记疑问。
    date_window = date_window_from_rule(text)
    seal_required = any(w in text for w in ("盖章", "公章", "签章"))
    qualifying: list[tuple[ContractRecord, Decimal]] = []
    rejected: list[str] = []
    unverified_notes: list[str] = []
    unverified_records: list[dict] = []
    guard_escalated = False
    date_dropped = type_dropped = 0
    date_unsure = type_unsure = seal_unverified = 0
    # 严口径（2026-11-24 run158 放水修正）：未核实（不可读/未绑上）不再计入
    # ——只按 verified 案例计分（部分分保留），未核验案例留 VL 翻案/人工复核。
    # BID_EVAL_CASE_UNVERIFIED_COUNT=1 回退旧"未核实计入+疑问"（回滚通道）。
    from app import config as _config
    strict_unverified = not _config.case_unverified_count_enabled()
    for record in inventory.contracts:
        if record.source == "person_proof":
            continue
        if (record.materials_state or {}).get("_case_block_complete"):
            for kind in ("contract", "invoice", "acceptance"):
                if (record.materials_state or {}).get(kind) is None:
                    record.materials_state[kind] = bool(
                        (record.materials or {}).get(kind)
                    )
        amount = record.amount_yuan
        if amount is None:
            rejected.append(f"{record.name}（无金额）")
            continue
        if tiers and amount < Decimal(str(tiers[0].get("min_amount_yuan", 0))):
            continue
        d_state = date_gate_state(record.signed_date, date_window)
        if d_state is False:
            rejected.append(
                f"{record.name}（签订时间 {record.signed_date} 不在细则时间窗内）")
            date_dropped += 1
            continue
        t_state = documented_project_type_state(record.project_type, record.name, text)
        if t_state is False:
            rejected.append(f"{record.name}（明确非细则要求类型，按类型门槛剔除）")
            type_dropped += 1
            continue
        if t_state is None and "运维" in text:
            type_unsure += 1
        if d_state is None and (date_window[0] or date_window[1]):
            date_unsure += 1
        record_unverified: list[str] = []
        if required_materials:
            missing, unverified = _missing_required_materials(
                record, required_materials, or_group)
            # PR4 护栏（2026-11-24 用户口径"不允许漏评分"）：目录/索引列有该
            # 材料页但绑定未闭合 → **禁止按缺件判 0**，转"未核实"（护栏
            # 报警）；目录也没有 → 真缺件判 0（红线保留）。翻转方向只升不降。
            if (missing and _guard_on(inventory)
                    and not (record.materials_state or {}).get("_case_block_complete")):
                _kind_of_label = {"合同": "contract", "发票": "invoice",
                                  "验收证明": "acceptance", "业主证明": "acceptance"}
                escalated = [m for m in missing
                             if (inventory.locator_index or {}).get(
                                 _kind_of_label.get(m, ""))]
                if escalated:
                    record_unverified.append(
                        f"{'/'.join(escalated)}目录列有材料页但绑定未闭合")
                    guard_escalated = True
                    missing = [m for m in missing if m not in escalated]
            if missing:
                rejected.append(f"{record.name}（缺{'/'.join(missing)}，细则：未提供的不得分）")
                continue
            if unverified:
                record_unverified.append(f"{'/'.join(unverified)}不可读/未绑上")
        elif not _contract_material_ok(record, requires_material):
            rejected.append(f"{record.name}（缺发票/验收）")
            continue
        if record_unverified and strict_unverified:
            unverified_notes.append(
                f"{record.name}（{'；'.join(record_unverified)}，严口径不计入，待视觉核验/人工）")
            unverified_records.append({
                "name": record.name,
                "reason": "；".join(record_unverified),
                "pages": list(record.pages or []),
                "materials": {k: list(v or [])
                              for k, v in (record.materials or {}).items()},
            })
            continue
        if record_unverified:
            unverified_notes.append(
                f"{record.name}（{'；'.join(record_unverified)}，计入并标疑问）")
        if seal_required and (record.materials_state or {}).get("seal") is not True:
            # 盖章三态：VL has_seal=true→True；其余→None。文本路无法核验盖章
            # ≠未盖章——不阻计，记疑问（确认缺判 0 仅对可读确认缺）
            seal_unverified += 1
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
        detail = "未找到满足金额/材料/时间/类型要求的业绩：" + "；".join(rejected[:6])
    elif inventory.performance_section_found:
        detail = "已定位业绩章节，未检出满足金额要求的合同"
    else:
        detail = "未定位到业绩章节或合同金额"
    if unverified_notes:
        detail += (("；未核实不计入（严口径，待视觉核验/人工）："
                    if strict_unverified and unverified_records else "；未核实计入：")
                   + "；".join(unverified_notes[:6]))
    if type_dropped:
        detail += f"（按'类似运维项目'类型门槛剔除 {type_dropped} 个明确非运维类案例）"
    if date_dropped:
        detail += f"（按细则时间窗剔除 {date_dropped} 个签订时间窗外案例）"
    if type_unsure:
        detail += f"（{type_unsure} 个案例类型无法机器确认，保留计分并标疑问）"
    if date_unsure:
        detail += f"（{date_unsure} 个案例签订时间无法确认，保留计分并标疑问）"
    if seal_unverified:
        detail += f"（{seal_unverified} 个案例盖章未核验，计入并标疑问）"
    score = _cap(score, criterion)
    # A zero is a confirmed conclusion only when contracts were actually
    # located and every one carries a readable amount.  Otherwise let the LLM
    # path attempt the missing blocks; the closure pass is still the backstop.
    company_contracts = [
        record for record in inventory.contracts
        if record.source != "person_proof"
    ]
    unknown_amounts = any(record.amount_yuan is None for record in company_contracts)
    complete = (inventory.performance_section_found and bool(company_contracts)
                and not unknown_amounts and not unverified_notes)
    zero_reason = None
    if score == 0:
        if unverified_notes:
            # 0 分不是评审结论：未核实案例剔除所致——待 VL 翻案/人工复核
            if strict_unverified and unverified_records:
                zero_reason = ("material_binding_conflict" if guard_escalated
                               else "material_unverified")
            else:
                zero_reason = "material_binding_conflict"
        elif type_dropped:
            # 类型门槛剔空：0 分是类型判断结论而非"未提供材料"
            zero_reason = "type_gate"
        else:
            zero_reason = "confirmed_absent" if complete else "no_facts"
    warnings = []
    if unverified_notes:
        warnings.append({"code": "material_unverified",
                         "message": "；".join(unverified_notes[:6])})
    gate_concern = bool(type_unsure or date_unsure or seal_unverified)
    if gate_concern:
        _gate_msgs = []
        if type_unsure:
            _gate_msgs.append(f"{type_unsure} 个案例类型无法机器确认")
        if date_unsure:
            _gate_msgs.append(f"{date_unsure} 个案例签订时间无法确认")
        if seal_unverified:
            _gate_msgs.append(f"{seal_unverified} 个案例盖章未核验")
        warnings.append({"code": "gate_unverified",
                         "message": "、".join(_gate_msgs) + "（保留计分并标疑问）"})
    return FastPathResult(
        score=score, detail=detail + f"；合计 {score} 分",
        # 三闸"无法确认"（类型/日期/盖章）只落 warnings+detail 注记（机器局限
        # ≠材料问题，不翻 verdict 免淹没复核队列）；材料未核实才 concern
        verdict=("concern" if unverified_notes else "pass") if score > 0
                else ("fail" if complete else "concern"),
        warnings=warnings,
        facts={"contracts": [
            {"name": r.name, "amount_yuan": str(r.amount_yuan), "pages": r.pages,
             "materials": r.materials, "materials_state": r.materials_state}
            for r in inventory.contracts
        ], "unverified_records": unverified_records},
        pages=sorted({p for r in inventory.contracts for p in r.pages}),
        zero_reason=zero_reason,
        confidence=_confidence_from(inventory, complete),
        complete=complete,
    )


# ---------------------------------------------------------------------------
# Personnel
# ---------------------------------------------------------------------------
_RE_EXPERIENCE_RANGE = re.compile(
    r"(20\d{2})\s*年?\s*(?:\d{1,2}\s*月)?\s*(?:至|—|~|－|\-|到)\s*"
    r"(20\d{2})\s*年?\s*(?:\d{1,2}\s*月)?"
)
_RE_EXPERIENCE_YEARS = re.compile(r"(\d{1,2})\s*年(?:以上|及以上|余|多)?")


def _experience_years(text: str) -> Decimal | None:
    values: list[Decimal] = []
    for match in _RE_EXPERIENCE_RANGE.finditer(text or ""):
        values.append(Decimal(int(match.group(2)) - int(match.group(1)) + 1))
    for match in _RE_EXPERIENCE_YEARS.finditer(text or ""):
        value = Decimal(match.group(1))
        if 0 < value <= 55:
            values.append(value)
    return max(values) if values else None


def _leader_experience_score(criterion: dict, inventory: MaterialInventory) -> FastPathResult:
    maximum = _max(criterion) or Decimal(0)
    text = _rule_text(criterion)
    tiers: list[tuple[Decimal, Decimal]] = []
    cn = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    for years, score in re.findall(
            r"([一二两三四五六七八九十\d]+)\s*年(?:以上|及以上|或以上)?[^。；]{0,25}?得\s*(\d+(?:\.\d+)?)\s*分", text):
        years_value = cn.get(years, years)
        tiers.append((Decimal(str(years_value)), Decimal(score)))
    tiers.sort(reverse=True)
    leader_re = re.compile(r"项目负责人|项目经理|项目总监|技术负责人|总负责人")
    related_re = re.compile(r"会议|多媒体|音视频|显示|扩声|大屏|音控")
    candidates = []
    for person in inventory.persons:
        scope_ok = bool(leader_re.search(person.role or "")) or not any(
            leader_re.search(p.role or "") for p in inventory.persons
        )
        if not scope_ok:
            continue
        name_text = person.experience or ""
        for page in inventory.pages:
            if person.name and person.name in str(page.get("text") or ""):
                name_text += "\n" + str(page.get("text") or "")
        years = person.years or _experience_years(name_text)
        related = bool(related_re.search((person.experience or "") + (person.role or "")))
        if related:
            candidates.append((years, person, name_text, related))
    if not candidates:
        return FastPathResult(
            Decimal(0), "未从项目负责人资料中核定满足年限的类似项目经验", "concern",
            {"persons": [{"name": p.name, "role": p.role, "years": str(p.years) if p.years else None}
                         for p in inventory.persons]},
            sorted({page for p in inventory.persons for page in p.pages}),
            zero_reason="no_facts", complete=False, confidence="low",
        )
    years, person, _name_text, _related = max(candidates, key=lambda item: item[0] or Decimal(0))
    score = Decimal(0)
    if years is None and _related:
        # 人员表只证明相关经历但未写明确年限时，按最低档计入，避免把
        # 有相关经历的人员因年限字段缺失误判为0；具体年限仍保留审计。
        score = min((value for _minimum, value in tiers), default=Decimal(0))
    else:
        for minimum, value in tiers:
            if years >= minimum:
                score = value
                break
    score = _cap(score, criterion)
    detail = f"项目负责人{person.name}类似项目经验约{years}年，按年限档计{score}分"
    return FastPathResult(
        score=score, detail=detail, verdict="pass" if score > 0 else "fail",
        facts={"persons": [{"name": person.name, "role": person.role, "years": str(years)}]},
        pages=sorted(set(person.pages) | set(person.experience_pages)),
        zero_reason=None if score > 0 else "confirmed_absent",
        complete=True, confidence="high",
    )


def _person_experience_score(criterion: dict, inventory: MaterialInventory) -> FastPathResult:
    maximum = _max(criterion) or Decimal(0)
    minimum_years = None
    match = _RE_YEARS_MIN.search(_rule_text(criterion))
    if match:
        minimum_years = Decimal(match.group(1))
    per_person = _RE_PER_PERSON.search(_rule_text(criterion))
    per = _decimal(per_person.group(1)) if per_person else Decimal(1)
    eligible: list[PersonRecord] = []
    related_re = re.compile(r"会议|多媒体|音视频|运维|维护|维保")
    for person in inventory.persons:
        if not re.search(r"驻场|维保|成员|团队", person.role or ""):
            continue
        person_text = person.experience or ""
        for page in inventory.pages:
            if person.name and person.name in str(page.get("text") or ""):
                person_text += "\n" + str(page.get("text") or "")
        years = person.years or _experience_years(person_text)
        if years is not None and minimum_years is not None:
            if years >= minimum_years and re.search(r"驻场|维保|维护|团队|技术员", person.role or ""):
                eligible.append(person)
            continue
        if person.experience_pages or re.search(r"年|场", person_text):
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


def _has_special_cert_pair(certs: list) -> bool:
    """c53「电工作业和登高作业证，具备齐全的每人得1分」：须同持电工族+登高族。"""
    joined = "、".join(str(c) for c in (certs or []))
    has_electric = any(k in joined for k in _SPECIAL_CERT_ELECTRIC)
    has_climbing = any(k in joined for k in _SPECIAL_CERT_CLIMBING)
    return has_electric and has_climbing


def _person_special_certs(criterion: dict, inventory: MaterialInventory) -> FastPathResult:
    maximum = _max(criterion) or Decimal(0)
    per_person = _RE_PER_PERSON.search(_rule_text(criterion))
    per = _decimal(per_person.group(1)) if per_person else Decimal(1)
    # P1-4：齐全 = 同时具备电工作业族 + 登高/高处族（见 _has_special_cert_pair）。
    eligible = []
    for person in inventory.persons:
        person_text = person.name + "\n" + "、".join(person.certs or [])
        for page in inventory.pages:
            page_text = str(page.get("text") or "")
            if person.name and person.name in page_text:
                start = max(0, page_text.find(person.name) - 350)
                end = min(len(page_text), page_text.find(person.name) + len(person.name) + 650)
                person_text += "\n" + page_text[start:end]
        if _has_special_cert_pair(person.certs) or _has_special_cert_pair([person_text]):
            eligible.append(person)
    score = min(maximum, Decimal(len(eligible)) * (per or Decimal(0)))
    detail = (
        "特种作业证两证齐全人员：" + "、".join(f"{p.name}（{'/'.join(p.certs)}）" for p in eligible[:10])
        if eligible else "未从团队表检出电工+登高两证齐全人员"
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
    complete = inventory.value_section_found
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
# Binary response commitment
# ---------------------------------------------------------------------------
def _response_commitment_score(criterion: dict, inventory: MaterialInventory) -> FastPathResult:
    """“优于要求响应时间一半且有可行方式”是二值承诺项，不套响应小时档位。"""
    maximum = _max(criterion) or Decimal(0)
    text = inventory.full_text or ""
    anchors = []
    for match in re.finditer(r"应急响应|非招标人工作时间|响应时间", text):
        start = max(0, match.start() - 300)
        end = min(len(text), match.end() + 700)
        anchors.append(text[start:end])
    window = "\n".join(anchors)
    has_half = bool(re.search(r"优于[^。；\n]{0,40}(?:一半|1\s*/\s*2|50\s*%)", window))
    has_method = bool(re.search(r"方式|措施|流程|值班|电话|远程|到场|可行|响应机制", window))
    score = maximum if has_half and has_method else Decimal(0)
    score = _cap(score, criterion)
    readable = bool(text.strip()) and not any(
        str(page.get("source") or "") in ("ocr", "ocr_img") for page in inventory.pages
    )
    complete = readable or (has_half and has_method)
    detail = (
        "已核验非工作时间应急响应承诺优于要求一半，并含可行响应方式"
        if score > 0 else
        "未核验到同时满足‘优于要求一半’和可行响应方式的承诺"
    )
    return FastPathResult(
        score=score, detail=detail + f"；合计 {score} 分",
        verdict="pass" if score > 0 else ("fail" if complete else "concern"),
        facts={"response_commitment": {"half": has_half, "method": has_method}},
        pages=sorted({
            int(page.get("page_no") or 0) for page in inventory.pages
            if re.search(r"应急响应|非招标人工作时间|响应时间", str(page.get("text") or ""))
        }),
        zero_reason=None if score > 0 else ("confirmed_absent" if complete else "no_facts"),
        complete=complete,
        confidence=_confidence_from(inventory, complete),
    )


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------
def _platform_query_families(inventory: MaterialInventory) -> set[str]:
    families: set[str] = set()
    for page in inventory.pages:
        found, _numbers = platform_query_details(
            str(page.get("text") or ""), require_seal=True
        )
        families.update(found)
    return families


def _cert_count_tier_score(criterion: dict, inventory: MaterialInventory) -> FastPathResult:
    maximum = _max(criterion) or Decimal(0)
    query_required = requires_platform_certificate_query(criterion)
    query_families = _platform_query_families(inventory) if query_required else set()
    families: list[str] = []
    skipped_unverified: list[str] = []
    for cert in inventory.certs:
        key = cert.normalized or normalize_cert(cert.name)
        if not (key.startswith("ISO") or "信息技术服务" in (cert.name or "")):
            continue
        if query_required:
            if query_families and key not in query_families and cert.backing is False:
                continue
            if not query_families and cert.backing is False:
                continue
            if not query_families and cert.backing is None:
                skipped_unverified.append(key)
        if key not in families:
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
    if skipped_unverified:
        detail += f"（{'、'.join(skipped_unverified)} 仅有申报/未见查询截图，按未核验计入）"
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
        family = None
        if "电子与智能化" in name:
            family = "电子与智能化工程专业承包"
            if level == "一级":
                value = Decimal(1)
            elif level == "二级":
                value = Decimal("0.5")
        elif "音视频集成" in name:
            family = "音视频集成工程能力"
            if level == "一级":
                value = Decimal(1)
            elif level == "二级":
                value = Decimal("0.5")
        if value is not None and family is not None:
            scores[family] = max(scores.get(family, Decimal(0)), value)
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


# ---------------------------------------------------------------------------
# Person-level certificates (拟派负责人/工程师的 ITIL、PMP 等)
# ---------------------------------------------------------------------------
_RE_CERT_TARGET = re.compile(r"(?:具有|具备|持有)\s*([^，。；;、]{2,40}?)\s*(?:认证|证书)")
_RE_LEADER_ROLE = re.compile(r"负责人|项目经理|项目总监")


def _norm_material_text(value: Any) -> str:
    return re.sub(r"[^0-9a-zA-Z一-龥]", "", str(value or "")).lower()


def _person_cert_targets(text: str) -> list[str]:
    """规则文本点名的证书族：`具有<X>认证/证书得N分` → [X]。

    只认"具有/具备/持有…认证/证书"句式——"提供证书复印件""以上认证证书"
    等材料说明不会成为证书目标。
    """
    seen: set[str] = set()
    out: list[str] = []
    for match in _RE_CERT_TARGET.finditer(text or ""):
        name = re.sub(r"\s+", " ", match.group(1)).strip(" 、，。；;")
        key = _norm_material_text(name)
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _person_in_scope(person: PersonRecord, scope: str) -> bool:
    is_leader = bool(_RE_LEADER_ROLE.search(str(person.role or "")))
    if scope == "leader":
        return is_leader
    if scope == "member":
        return not is_leader
    return True


def _person_has_cert(person: PersonRecord, target_key: str) -> bool:
    return any(target_key and target_key in _norm_material_text(cert)
               for cert in getattr(person, "certs", []) or [])


def _cert_copy_state(target: str, pages: list[dict]) -> bool | None:
    """证书复印件页三态（与公司证书背书同语义）：OCR/扫描页命中 → True；
    仅文本申报、文档含扫描页 → None（计入+疑问）；全文可读且无命中 → False。"""
    key = _norm_material_text(target)
    if not key:
        return None
    has_scan = any(str(p.get("source") or "") in ("ocr", "ocr_img") for p in pages)
    for page in pages:
        if (str(page.get("source") or "") in ("ocr", "ocr_img")
                and key in _norm_material_text(page.get("text") or "")):
            return True
    return None if has_scan else False


def _person_cert_score(criterion: dict, inventory: MaterialInventory) -> FastPathResult:
    params = dict(criterion.get("params") or {})
    scope = str(params.get("person_scope") or "")
    maximum = _max(criterion) or Decimal(0)
    per_item = _decimal(params.get("per_item")) or Decimal(0)
    max_items = int(params.get("max_items") or 0)
    targets = _person_cert_targets(_rule_text(criterion))
    persons = [p for p in inventory.persons if _person_in_scope(p, scope)]
    matched: list[str] = []
    backing: dict[str, bool | None] = {}
    holders: list[str] = []
    for target in targets:
        key = _norm_material_text(target)
        hit = [p for p in persons if _person_has_cert(p, key)]
        if not hit:
            continue
        matched.append(target)
        backing[target] = _cert_copy_state(target, inventory.pages)
        holders.extend(f"{p.name}（{target}）" for p in hit)
    if str(params.get("_objective_form") or "") == "per_item":
        units = len({p.name for p in persons
                     if any(_person_has_cert(p, _norm_material_text(t)) for t in targets)})
    else:
        units = len(matched)
    effective = min(Decimal(units), Decimal(max_items)) if max_items else Decimal(units)
    score = _cap(effective * per_item, criterion)
    if matched:
        detail = "人员证书：" + "、".join(holders)
    elif not inventory.personnel_table_found:
        detail = "未定位到项目团队人员情况表"
    elif not persons:
        detail = ("未识别到项目负责人" if scope == "leader" else "未识别到团队成员")
    else:
        detail = "团队人员情况表中未检出规则点名的证书"
    doubts = [name for name, state in backing.items() if state is None]
    if doubts:
        detail += f"（{'、'.join(doubts)} 仅见申报、未见复印件页，按未核验计入并标疑问）"
    missing = [name for name, state in backing.items() if state is False]
    if missing:
        detail += f"（{'、'.join(missing)} 确认无证书复印件，不计）"
    complete = inventory.personnel_table_found and bool(persons)
    zero_reason = None if score > 0 else ("confirmed_absent" if complete else "no_facts")
    return FastPathResult(
        score=score,
        detail=detail + f"；合计 {score} 分",
        verdict="pass" if score > 0 else ("fail" if complete else "concern"),
        facts={"certificates": matched, "cert_backing": backing,
               "holders": holders},
        pages=[],
        zero_reason=zero_reason,
        complete=complete,
        confidence="high" if inventory.personnel_table_found else "low",
    )


def _generic_engine_score(criterion: dict, inventory: MaterialInventory,
                         params: dict[str, Any]) -> FastPathResult | None:
    from app.services.automatic_scoring_service import _ENGINES
    from app.engine.objective import score_count

    facts = inventory.to_bid_facts()
    engine = params.get("engine")
    # 承诺值字段的全文抽取语料（包A）：该家 source=='text' 页的文本层拼接，
    # 纯文本零成本（严禁 OCR/视觉/读页图）。facts.text 已是全页文本，语料作为
    # score_value_* 的第三级兜底契约注入，供前两级（窗口/送审选段）都抽不到时使用。
    text_corpus = "\n".join(
        str(p.get("text") or "") for p in inventory.pages
        if str(p.get("source") or "text") == "text"
    )
    scoring_criterion = {
        **criterion,
        "verified_facts": {"inventory": inventory.audit},
        "cert_backing": inventory.cert_backing,
        "cert_section_present": inventory.cert_section_present,
        "fulltext_corpus": text_corpus,
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
    unresolved = getattr(result, "unresolved", None)
    verdict = "pass" if score > 0 else "fail"
    complete = not bool(unresolved)
    if engine in ("value_tier", "value_linear") and unresolved == "no_facts":
        # 包A 第 3 步：承诺值"抽取失败"不是"不符合"结论——不得按 fail/0 定格。
        # 交 fastpath_cell 走残差 LLM 抽值轮，仍抽不到则落暂定兜底
        # （kind=value_extraction_unresolved，未定分待人工）。
        zero_reason = "value_extraction_unresolved"
        verdict = "concern"
        complete = False
    return FastPathResult(
        score=score, detail=str(result.detail or "") + f"；合计 {score} 分",
        verdict=verdict,
        facts={"detail": result.detail},
        pages=[],
        zero_reason=zero_reason,
        confidence="medium",
        complete=complete,
    )


def score_objective(criterion: dict, inventory: MaterialInventory) -> FastPathResult | None:
    plan = build_objective_plan(criterion)
    if plan is None:
        return None
    if plan.kind == "contracts":
        return _score_contracts(criterion, inventory)
    if plan.kind == "leader_experience":
        return _leader_experience_score(criterion, inventory)
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
    if plan.kind == "response_commitment":
        return _response_commitment_score(criterion, inventory)
    if plan.kind == "person_certs":
        return _person_cert_score(criterion, inventory)
    if plan.kind == "cert_count_tier":
        return _cert_count_tier_score(criterion, inventory)
    if plan.kind == "cert_level_sum":
        return _cert_level_sum_score(criterion, inventory)
    if plan.kind == "attribute_tier":
        return _generic_engine_score(criterion, inventory, plan.params)
    if plan.kind == "engine":
        return _generic_engine_score(criterion, inventory, plan.params)
    return None
