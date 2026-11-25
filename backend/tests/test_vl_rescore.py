# -*- coding: utf-8 -*-
"""facts→score_count 重算适配器契约测试（2026-11-25 翻案通道收口）。

红线锁定：owner_proof/业主证明绝不映射 work_proof；cert_section_present 三态
直传（盲态 None 不得压成 False 清零）；type_eligible 恒 bool 不 raise；
确认缺判 0 不放宽（zero_reason 透传）；无法构造 → None（安全态）。
"""
from __future__ import annotations

from decimal import Decimal

from app.engine.facts import BidFacts
from app.engine.material_inventory import (
    CertRecord, ContractRecord, MaterialInventory, PersonRecord,
)
from app.engine.vl_rescore import rescore_from_merged_facts


def _inv(contracts=None, certs=None, persons=None, audit=None,
         cert_backing=None, cert_present=False, text=""):
    return MaterialInventory(
        bidder_id=1, pages=[], full_text=text, facts=BidFacts(text=text),
        contracts=contracts or [], certs=certs or [], persons=persons or [],
        cert_section_present=cert_present, cert_backing=cert_backing or {},
        audit=audit or {}, performance_section_found=True,
    )


CRIT_CERT = {
    "score_type": "count", "name": "投标人认证及资质情况—管理体系认证",
    "max_score": "3",
    "params": {"field": "certificates", "per_item": "1", "max_items": "3"},
    "description": "每提供一项有效期内的认证证书得1分，最高3分。",
    "required_artifacts": "证书复印件及查询结果截图",
}

CRIT_CMMI = {
    "score_type": "count", "name": "投标人认证及资质情况—CMMI/CSMM 认证",
    "max_score": "2",
    "params": {"field": "cmmi", "per_item": "2", "max_items": "1"},
    "description": "具有有效期内 CMMI 认证二级及以上得 2 分。",
}

CRIT_PERSON = {
    "code": "c48", "score_type": "count", "name": "项目团队—项目负责人业绩",
    "max_score": "4",
    "description": ("项目负责人具有五年及以上工作经验，且作为项目负责人具有类似系统"
                    "运维项目实施/服务经验，每案例2分，最高4分。【证明材料】须提供"
                    "业绩证明材料、工作经验证明及社保证明，未提供的不得分。"),
    "params": {"person_scope": "leader", "field": "performance", "per_item": "2",
               "max_items": "2"},
}


def _cert(name, backing=True, pages=None):
    from app.engine.objective import normalize_cert

    return CertRecord(name=name, normalized=normalize_cert(name),
                      pages=list(pages or [10]), backing=backing)


# ---- 证书格（c44/c45）-------------------------------------------------------

def test_rescore_cert_counts_with_backing() -> None:
    inv = _inv(certs=[_cert("ISO9001"), _cert("ISO27001"), _cert("ISO20000")],
               audit={"cert_section_state": None})
    res = rescore_from_merged_facts(CRIT_CERT, inv)
    assert res is not None
    assert res.score == Decimal("3.00"), "三本证书复印件（backing=True）照计"
    assert res.zero_reason is None


def test_rescore_cert_blind_section_counts_with_concern() -> None:
    """盲态（截图节不可读 None）→ 照计+疑问，不得压成 False 清零（run155 教训）。"""
    inv = _inv(certs=[_cert("ISO9001")], audit={"cert_section_state": None})
    res = rescore_from_merged_facts(CRIT_CERT, inv)
    assert res.score == Decimal("1.00")
    assert "截图节不可读" in (res.detail or ""), res.detail


def test_rescore_cert_confirmed_no_screenshot_clears_iso() -> None:
    """红线：截图节确认无（False）→ ISO 清零（细则'须提供查询结果截图'）。"""
    inv = _inv(certs=[_cert("ISO9001")], audit={"cert_section_state": False})
    res = rescore_from_merged_facts(CRIT_CERT, inv)
    assert res.score == 0


def test_rescore_cert_screenshot_true_upgrades() -> None:
    """VL has_query_screenshot=true 升级 cert_section_present=True → 计入无疑。"""
    inv = _inv(certs=[_cert("ISO9001")], audit={"cert_section_state": None},
               cert_present=True)
    res = rescore_from_merged_facts(CRIT_CERT, inv)
    assert res.score == Decimal("1.00")
    assert "截图节不可读" not in (res.detail or "")


def test_rescore_cmmi_from_record_backing() -> None:
    inv = _inv(certs=[_cert("CMMI 三级")], audit={"cert_section_state": None})
    res = rescore_from_merged_facts(CRIT_CMMI, inv)
    assert res is not None
    assert res.score == Decimal("2.00")


# ---- person_scope 业绩格（c48/c49）------------------------------------------

def _person_case_inv(**state):
    rec = ContractRecord(
        name="某平台运维服务项目", amount_yuan=Decimal("700000"),
        signed_date="2024-03-01", pages=[1, 2],
        materials={"contract": [1]}, materials_state={"contract": True},
        person_names=["杨祥海"],
        person_states={"work_proof": True, "experience_proof": True,
                       "social_security": True},
        project_type="运维服务", source="vl")
    for k, v in (state or {}).items():
        if k == "person_states":
            rec.person_states.update(v)
        else:
            setattr(rec, k, v)
    persons = [PersonRecord(name="杨祥海", role="项目负责人", pages=[9])]
    return _inv(contracts=[rec], persons=persons)


def test_rescore_person_cases_scores_with_full_chain() -> None:
    res = rescore_from_merged_facts(CRIT_PERSON, _person_case_inv())
    assert res is not None
    assert res.score == Decimal("2.00"), f"全链案例应计 2 分：{res.detail}"
    assert res.zero_reason is None


def test_rescore_person_missing_work_proof_zero() -> None:
    """红线：业绩证明缺（含仅业主证明）→ 案例不计，绝不放水。"""
    inv = _person_case_inv(person_states={"work_proof": None,
                                          "experience_proof": None,
                                          "social_security": None})
    inv.contracts[0].materials["owner_proof"] = [5]
    res = rescore_from_merged_facts(CRIT_PERSON, inv)
    assert res is not None
    assert res.score == 0, f"work_proof 无正向证据不得计分：{res.detail}"


def test_rescore_person_relaxed_partial_chain() -> None:
    """放宽口径（2026-11-25 用户拍板）：业绩证明可见+归属 → 经验证明/社保
    以线索计入并标疑问。"""
    inv = _person_case_inv(person_states={"work_proof": True,
                                          "experience_proof": None,
                                          "social_security": None})
    res = rescore_from_merged_facts(CRIT_PERSON, inv)
    assert res is not None
    assert res.score == Decimal("2.00"), res.detail
    assert "放宽口径" in (res.detail or "")


def test_rescore_person_confirmed_missing_still_blocks() -> None:
    """红线：可读确认缺（False）不放宽——确认缺判 0 不放宽。"""
    inv = _person_case_inv(person_states={"work_proof": True,
                                          "experience_proof": True,
                                          "social_security": False})
    res = rescore_from_merged_facts(CRIT_PERSON, inv)
    assert res is not None
    assert res.score == 0, f"确认缺（False）必须阻计：{res.detail}"


def test_cmmi_field_inference_beats_generic_cert_kw() -> None:
    """CMMI/CSMM 是具体证书名（cmmi 专属分支），不得落到 certificates
    （否则任何证书都给分——2026-11-25 c45 实证的放水面）。"""
    from app.engine.objective import _infer_count_field
    from app.rules.check_infer import classify_scoring_form

    crit = {"name": "投标人认证及资质情况—CMMI/CSMM 认证",
            "description": "具有有效期内CMMI认证二级或CSMM认证二级及以上得2分。"}
    assert _infer_count_field(crit) == "cmmi"
    form = classify_scoring_form(crit["name"] + "\n" + crit["description"])
    assert form is not None and form[1].get("field") == "cmmi", form


def test_rescore_person_type_eligible_never_none() -> None:
    """type_eligible 恒 bool（None→True 保留+疑问）——managed raise 不触发。"""
    inv = _person_case_inv(project_type=None)  # 名称含"运维服务"→True
    res = rescore_from_merged_facts(CRIT_PERSON, inv)
    assert res is not None and res.score == Decimal("2.00")
    # 明确非运维 → 剔除
    inv2 = _person_case_inv(project_type=None)
    inv2.contracts[0].name = "某网络系统建设项目"
    res2 = rescore_from_merged_facts(CRIT_PERSON, inv2)
    assert res2 is not None and res2.score == 0


def test_rescore_person_amount_tier_prefilters_no_raise() -> None:
    """managed 金额档：无金额案例预滤（防 raise），有金额照档计。"""
    crit = dict(CRIT_PERSON)
    crit["params"] = dict(CRIT_PERSON["params"], min_amount_yuan="500000",
                          tiers=[{"min_amount_yuan": "500000", "per_item": "2"}])
    inv = _person_case_inv()
    rec2 = ContractRecord(name="某运维项目B", pages=[3],
                          person_names=["杨祥海"],
                          person_states={"work_proof": True, "experience_proof": True,
                                         "social_security": True},
                          materials_state={"contract": True}, source="vl")
    inv.contracts.append(rec2)  # 无金额 → 预滤
    res = rescore_from_merged_facts(crit, inv)
    assert res is not None
    assert res.score > 0, res.detail
    assert "无可核验金额" in (res.detail or "")


def test_rescore_uncovered_shape_returns_none() -> None:
    crit = {"score_type": "count", "name": "承诺响应时间", "max_score": "4",
            "params": {"engine": "value_tier", "field": "response_hours"}}
    assert rescore_from_merged_facts(crit, _inv()) is None
    # 人员级证书格不硬算
    crit2 = {"score_type": "count", "name": "人员证书", "max_score": "2",
             "params": {"person_scope": "member", "field": "certificates"}}
    assert rescore_from_merged_facts(crit2, _inv()) is None


def test_rescore_mixed_tiers_degrades_without_managed() -> None:
    """档位混杂 raise → 降级非 managed 重试（不带 verified_facts）。"""
    crit = dict(CRIT_PERSON)
    crit["params"] = dict(CRIT_PERSON["params"],
                          tiers=[{"min_count": "2", "score": "1"},
                                 {"min_amount_yuan": "500000", "per_item": "1"}])
    res = rescore_from_merged_facts(crit, _person_case_inv())
    assert res is not None, "降级重试仍应给出结论"
