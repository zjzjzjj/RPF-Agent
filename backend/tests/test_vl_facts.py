# -*- coding: utf-8 -*-
"""VL 事实合并 + 三闸契约测试（2026-11-24「VL 报事实、代码算分」）。

红线锁定：VL 事实只补不抢（文本值优先、materials_state 只升不降、派生副本
不污染共享缓存）；三闸只认细则明示条件（日期窗外剔除、类型三态、盖章不阻计）；
VL false 归一 None 不判缺（防 VL 误杀）。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.engine.material_inventory import (
    CertRecord, ContractRecord, MaterialInventory,
)
from app.engine.objective import (
    date_gate_state, date_window_from_rule, type_gate_state,
)
from app.engine.vl_facts import merge_vl_facts
from app.engine.facts import BidFacts


def _inv(contracts):  # noqa: ANN001, ANN202
    return MaterialInventory(
        bidder_id=1, pages=[], full_text="", facts=BidFacts(text=""),
        contracts=contracts, performance_section_found=True,
    )


# ---- 合并：键 / 只升不降 / 补空 / 派生副本 ---------------------------------

def test_merge_same_case_by_name_amount() -> None:
    rec = ContractRecord(name="某研究所维保", amount_yuan=Decimal("1200000"),
                         pages=[1], materials={"contract": [1]},
                         materials_state={"contract": True, "invoice": None})
    inv = _inv([rec])
    out = merge_vl_facts(inv, [{
        "kind": "contract", "name": "某研究所维保项目", "amount_yuan": "1200000",
        "has_invoice": True, "has_acceptance": True,
        "evidence": [{"document_id": 1, "page_no": 9, "observation": "发票一份"}],
    }])
    assert len(out.contracts) == 1, "同名同金额并成一条（宁误合勿双计）"
    merged = out.contracts[0]
    assert merged.materials_state["invoice"] is True
    assert merged.materials_state["acceptance"] is True
    assert 9 in merged.pages and 1 in merged.pages
    # 派生副本：共享 inventory 不被污染
    assert inv.contracts[0].materials_state.get("acceptance") is None


def test_query_number_ignores_separator_only_differences() -> None:
    cert = CertRecord(name="ISO27001", normalized="ISO27001",
                      certificate_number="00124IS20163R2M/6100",
                      backing=True, document_id=1, pages=[1])
    inv = MaterialInventory(
        bidder_id=1, pages=[], full_text="", facts=BidFacts(text=""),
        certs=[cert], cert_section_present=True,
    )
    out = merge_vl_facts(inv, [{
        "kind": "certificate", "name": "ISO27001平台查询结果",
        "certificate_number": "00124IS20163R2M6100", "has_query_screenshot": True,
        "evidence": [{"document_id": 1, "page_no": 2,
                      "observation": "全国认证认可信息公共服务平台证书详情页，证书编号00124IS20163R2M6100，盖有投标人红色公章"}],
    }])
    assert out.platform_query_certificates == {"ISO27001"}


def test_certificate_number_accepts_platform_prefix_alias() -> None:
    out = merge_vl_facts(_inv([]), [
        {"kind": "certificate", "name": "ISO9001质量管理体系认证",
         "certificate_number": "CN23-03503A", "has_seal": True,
         "evidence": [{"document_id": 1, "page_no": 61, "observation": (
             "认证证书，证书号 N.CN23-03503A，标准 ISO 9001:2015，"
             "有效期至2026-03-23，盖有投标人红色公章"
         )}]},
        {"kind": "certificate", "name": "ISO9001质量管理体系认证（CNCA平台查询）",
         "certificate_number": "N.CN23-03503A", "has_query_screenshot": True,
         "has_seal": True,
         "evidence": [{"document_id": 1, "page_no": 62, "observation": (
             "全国认证认可信息公共服务平台证书详情页，证书编号 N.CN23-03503A，"
             "认证项目质量管理体系认证（ISO9001），页面盖有投标人公章"
         )}]},
    ])
    assert [cert.certificate_number for cert in out.certs if cert.backing] == ["N.CN23-03503A"]
    assert out.platform_query_certificates == {"ISO9001"}


def test_merge_distinct_cases_coexist() -> None:
    rec = ContractRecord(name="项目A", amount_yuan=Decimal("700000"))
    out = merge_vl_facts(_inv([rec]), [{
        "kind": "contract", "name": "项目B", "amount_yuan": "800000",
        "has_contract": True,
        "evidence": [{"document_id": 1, "page_no": 5, "observation": "合同一份"}],
    }])
    assert len(out.contracts) == 2


def test_merge_only_upgrades_state() -> None:
    rec = ContractRecord(name="项目A", amount_yuan=Decimal("700000"),
                         materials_state={"contract": True, "invoice": False})
    out = merge_vl_facts(_inv([rec]), [{
        "kind": "contract", "name": "项目A", "amount_yuan": "700000",
        "has_invoice": True,
        "evidence": [{"document_id": 1, "page_no": 7, "observation": "发票复印件"}],
    }])
    st = out.contracts[0].materials_state
    assert st["contract"] is True, "文本 True 不得被降"
    assert st["invoice"] is True, "VL 图上确认有 → 翻案成 True（只升不降）"


def test_merge_fills_empty_amount_and_date_only() -> None:
    rec = ContractRecord(name="项目A", amount_yuan=Decimal("700000"),
                         signed_date="2024-01-25")
    out = merge_vl_facts(_inv([rec]), [{
        "kind": "contract", "name": "项目A", "amount_yuan": "999",
        "sign_date": "2023-01-01",
        "has_contract": True,
        "evidence": [{"document_id": 1, "page_no": 7, "observation": "合同"}],
    }])
    assert out.contracts[0].amount_yuan == Decimal("700000"), "文本金额优先"
    assert out.contracts[0].signed_date == "2024-01-25", "文本日期优先"


def test_merge_cert_and_screenshot_flag() -> None:
    out = merge_vl_facts(_inv([]), [
        {"kind": "certificate", "name": "ISO9001 质量管理体系认证证书",
         "has_query_screenshot": True,
         "evidence": [{"document_id": 1, "page_no": 30, "observation": "全国认证认可信息公共服务平台证书信息 ISO9001，可见红色公章"}]},
        {"kind": "certificate", "name": "ISO27001 信息安全管理体系认证证书",
         "has_query_screenshot": True,
         "evidence": [{"document_id": 1, "page_no": 31, "observation": "裁判文书查询结果截图，盖有投标人公章"}]},
        {"kind": "certificate", "name": "ISO9001 质量管理体系认证证书",
         "evidence": [{"document_id": 1, "page_no": 32, "observation": "ISO9001 证书原件扫描"}]},
        {"kind": "person", "name": "张三", "person_years": "6",
         "evidence": [{"document_id": 1, "page_no": 40, "observation": "简历"}]},
    ])
    assert any((c.normalized or "") == "ISO9001" for c in out.certs), out.certs
    assert out.cert_section_present is True
    assert out.platform_query_certificates == {"ISO9001"}
    assert out.persons and out.persons[0].years == Decimal("6")


def test_query_platform_binds_certificate_number_or_same_explicit_family():
    def evidence(page, observation):
        return [{"document_id": 1, "page_no": page, "observation": observation}]

    facts = [
        {"kind": "certificate", "name": "ISO9001", "certificate_number": "01234Q1345",
         "has_cert_proof": True,
         "evidence": evidence(1, "ISO9001 证书原件，证书编号 01234Q1345")},
        {"kind": "certificate", "name": "ISO27001", "certificate_number": "05678I9876",
         "has_cert_proof": True,
         "evidence": evidence(2, "ISO27001 证书原件，证书编号 05678I9876")},
        {"kind": "certificate", "name": "ISO9001", "has_query_screenshot": True,
         "evidence": evidence(3, "全国认证认可信息公共服务平台证书详情页，证书编号 01234Q1345，红章")},
        {"kind": "certificate", "name": "ISO27001", "has_query_screenshot": True,
         "evidence": evidence(4, "全国认证认可信息公共服务平台证书详情页，信息安全管理体系，证书编号 12345Z，红章")},
        {"kind": "certificate", "name": "ISO20000", "has_query_screenshot": True,
         "evidence": evidence(5, "全国认证认可信息公共服务平台证书详情页，信息技术服务管理体系，红章")},
    ]
    result = merge_vl_facts(_inv([]), facts)
    assert result.platform_query_certificates == {"ISO9001", "ISO20000"}


@pytest.mark.parametrize(("combined", "query_label", "numbers"), [
    (False, "证书详情页", ("16424Q30532R3M", "00124IS20163R2M/6100", "0012024ITSM0101R2NM/6100")),
    (True, "查询页", ("N.CN23-03503A", "N.CN23-04908D", "N.CN23-04908F")),
    (True, "查询结果截图", ("39324Q2050R0M", "0350125IS30792R0S", "0352023ITSM595R1LMN")),
])
def test_iso_binding_recovers_copy_flag_from_original_and_exact_query_pages(
        combined: bool, query_label: str, numbers: tuple[str, ...],
) -> None:
    from app.engine.vl_rescore import rescore_from_merged_facts

    facts = []
    families = ("ISO9001", "ISO27001", "ISO20000")
    labels = ("质量管理体系", "信息安全管理体系", "信息技术服务管理体系")
    for index, (family, label, number) in enumerate(zip(families, labels, numbers)):
        original = {"document_id": 1, "page_no": 61 + index * 2, "observation": (
            f"认证机构出具的《{label}认证证书》，注册号{number}，符合{family}，"
            "发证日期2024年5月9日，有效期至2027年5月9日，盖有投标人红色公章。"
        )}
        screenshot = {"document_id": 1, "page_no": 62 + index * 2, "observation": (
            f"全国认证认可信息公共服务平台{query_label}，证书编号{number}，"
            "证书状态有效，页面盖有投标人公章。"
        )}
        fact = {"kind": "certificate", "name": f"{label}认证证书（{family}）",
                "certificate_number": number, "has_cert_proof": None, "has_seal": True}
        if combined:
            facts.append({**fact, "has_query_screenshot": True, "evidence": [original, screenshot]})
        else:
            facts.extend([{**fact, "evidence": [original]},
                          {**fact, "has_query_screenshot": True, "evidence": [screenshot]}])

    inventory = merge_vl_facts(_inv([]), facts)

    assert {cert.certificate_number for cert in inventory.certs if cert.backing} == set(numbers)
    assert {tuple(cert.pages) for cert in inventory.certs} == {(61,), (63,), (65,)}
    assert inventory.platform_query_certificates == set(families)
    result = rescore_from_merged_facts({
        "name": "管理体系认证", "score_type": "count", "max_score": "3",
        "description": "ISO9001、ISO27001、ISO20000证书每项1分，最高3分",
        "required_evidence": "须提供证书复印件和全国认证认可信息公共服务平台查询结果截图并加盖公章",
        "params": {"field": "certificates", "per_item": "1", "max_items": "3"},
    }, inventory)
    assert result is not None and result.complete and result.score == Decimal(3)


def test_iso_original_accepts_documented_seal_flag_with_institution_seal():
    inventory = merge_vl_facts(_inv([]), [{
        "kind": "certificate", "name": "ISO9001", "certificate_number": "02925QJDD30R7L",
        "has_seal": True,
        "evidence": [{"document_id": 1, "page_no": 61, "observation": (
            "质量管理体系认证证书复印件，注册号02925QJDD30R7L，"
            "发证日期2025年4月7日，有效期至2028年4月6日，盖有认证中心印章"
        )}],
    }])
    assert len(inventory.certs) == 1
    assert inventory.certs[0].backing is True
    assert inventory.certs[0].certificate_number == "02925QJDD30R7L"

@pytest.mark.parametrize("variant", ["screenshot_only", "unrelated_screenshot", "wrong_number", "cross_document"])
def test_iso_binding_infers_copy_only_from_original_and_requires_exact_query(variant: str) -> None:
    original = {"document_id": 1, "page_no": 61, "observation": (
        "ISO27001认证证书复印件，证书号00124IS20163R2M/6100，有效期至2027年5月9日，盖有投标人红色公章"
    )}
    query = {"document_id": 1, "page_no": 62, "observation": (
        "全国认证认可信息公共服务平台查询页，ISO27001，证书编号00124IS20163R2M/6100，盖有投标人红色公章"
    )}
    if variant == "unrelated_screenshot":
        query["observation"] = "裁判文书查询结果截图，盖有投标人公章"
    elif variant == "wrong_number":
        query["observation"] = query["observation"].replace("/6100", "/6300")
    elif variant == "cross_document":
        query["document_id"] = 2
    evidence = [query] if variant == "screenshot_only" else [original, query]
    inventory = merge_vl_facts(_inv([]), [{
        "kind": "certificate", "name": "ISO27001", "certificate_number": "00124IS20163R2M/6100",
        "has_cert_proof": None, "has_query_screenshot": True, "evidence": evidence,
    }])

    if variant == "screenshot_only":
        assert not inventory.certs
    else:
        assert inventory.certs[0].pages == [61]
        assert not inventory.platform_query_certificates


# ---- 金额伪迹补正（2026-11-25 翻案通道收口）---------------------------------

def test_merge_corrects_artifact_amount() -> None:
    """文本 0.01 元伪迹 + VL 大额 → 补正并并成一条（run160 创联 −2 根因）。"""
    rec = ContractRecord(name="浙江中烟一体化数字化仓储系统运维服务合同",
                         amount_yuan=Decimal("0.01"), pages=[117])
    out = merge_vl_facts(_inv([rec]), [{
        "kind": "contract", "name": "浙江中烟一体化数字化仓储系统运维服务合同",
        "amount_yuan": "742800", "has_contract": True,
        "evidence": [{"document_id": 1, "page_no": 118,
                      "observation": "合同金额柒拾肆万贰仟捌佰元"}],
    }])
    assert len(out.contracts) == 1, "伪迹同案并成一条（防双计）"
    assert out.contracts[0].amount_yuan == Decimal("742800")
    assert out.contracts[0].amount_corrected is True


def test_merge_amount_text_priority_when_plausible() -> None:
    rec = ContractRecord(name="项目A", amount_yuan=Decimal("700000"))
    out = merge_vl_facts(_inv([rec]), [{
        "kind": "contract", "name": "项目A", "amount_yuan": "50",
        "has_contract": True,
        "evidence": [{"document_id": 1, "page_no": 7, "observation": "合同"}],
    }])
    assert out.contracts[0].amount_yuan == Decimal("700000"), "文本可信额优先"
    assert out.contracts[0].amount_corrected is False


def test_merge_both_artifact_amounts_merge_without_correction() -> None:
    rec = ContractRecord(name="项目B", amount_yuan=Decimal("500"))
    out = merge_vl_facts(_inv([rec]), [{
        "kind": "contract", "name": "项目B", "amount_yuan": "800",
        "has_contract": True,
        "evidence": [{"document_id": 1, "page_no": 7, "observation": "合同"}],
    }])
    assert len(out.contracts) == 1
    assert out.contracts[0].amount_yuan == Decimal("500"), "两值皆伪迹只并案不改值"


# ---- person 归属/材料三态并集（只升不降）-----------------------------------

def test_merge_person_states_only_up() -> None:
    rec = ContractRecord(name="项目A", amount_yuan=Decimal("700000"),
                         person_names=["张三"],
                         person_states={"work_proof": False, "experience_proof": None})
    out = merge_vl_facts(_inv([rec]), [{
        "kind": "contract", "name": "项目A", "amount_yuan": "700000",
        "person_names": ["张三", "李四"], "has_work_proof": True,
        "evidence": [{"document_id": 1, "page_no": 7,
                      "observation": "业绩证明载明张三姓名"}],
    }])
    t = out.contracts[0]
    assert t.person_names == ["张三", "李四"], "归属并集"
    assert t.person_states["work_proof"] is True, "VL True 翻案 False→True（只升不降）"
    assert t.person_states["experience_proof"] is None
    assert rec.person_states["work_proof"] is False, "派生副本不污染共享记录"


# ---- 三闸 -----------------------------------------------------------------

def test_date_window_from_rule_forms() -> None:
    assert date_window_from_rule(
        "自2022年1月1日（合同签订时间为准）至今的同类产品业绩") == ("2022-01-01", None)
    assert date_window_from_rule("2022年1月1日起签订的合同") == ("2022-01-01", None)
    start, end = date_window_from_rule("近3年（2022年1月1日至2024年12月31日）业绩")
    assert start == "2022-01-01" and end == "2024-12-31"
    assert date_window_from_rule("每个得1分，最高2分") == (None, None), "无明示日期不启用"


def test_date_gate_state_tri() -> None:
    window = ("2022-01-01", None)
    assert date_gate_state("2024-01-25", window) is True
    assert date_gate_state("2021-12-31", window) is False
    assert date_gate_state("2024年1月25日", window) is True
    assert date_gate_state(None, window) is None
    assert date_gate_state("看不清", window) is None
    assert date_gate_state("2021-01-01", (None, None)) is None, "窗未启用不判"


def test_type_gate_state_tri() -> None:
    rule = "类似系统运维项目经验，每个案例2分"
    assert type_gate_state("某平台运维服务项目", rule) is True
    assert type_gate_state("湖北广彩印刷项目", rule) is None, "无法确认≠确认不符"
    assert type_gate_state("某网络系统建设项目", rule) is False
    assert type_gate_state(None, rule) is None
    assert type_gate_state("某系统集成项目", "类似项目经验") is None, "细则未点名运维不启用"


def test_acceptance_project_type_takes_precedence_over_contract_name():
    from app.engine.objective import documented_project_type_state

    record = ContractRecord("某人工智能运维系统开发项目", Decimal("1843000"), project_type="运维服务")
    result = merge_vl_facts(_inv([record]), [{
        "kind": "contract", "name": record.name, "project_type": "软件开发", "has_acceptance": True,
        "evidence": [{"document_id": 1, "page_no": 104, "observation": "工程终验证书，系统类型：软件开发"}],
    }])
    case = result.contracts[0]
    assert case.project_type == "软件开发"
    assert documented_project_type_state(case.project_type, case.name, "类似运维项目") is True
    assert documented_project_type_state(None, "平台运维服务项目", "类似运维项目") is True
