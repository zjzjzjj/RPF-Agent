# -*- coding: utf-8 -*-
"""VL 事实合并 + 三闸契约测试（2026-11-24「VL 报事实、代码算分」）。

红线锁定：VL 事实只补不抢（文本值优先、materials_state 只升不降、派生副本
不污染共享缓存）；三闸只认细则明示条件（日期窗外剔除、类型三态、盖章不阻计）；
VL false 归一 None 不判缺（防 VL 误杀）。
"""
from __future__ import annotations

from decimal import Decimal

from app.engine.material_inventory import (
    ContractRecord, MaterialInventory,
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
         "evidence": [{"document_id": 1, "page_no": 30, "observation": "查询结果截图"}]},
        {"kind": "person", "name": "张三", "person_years": "6",
         "evidence": [{"document_id": 1, "page_no": 40, "observation": "简历"}]},
    ])
    assert any((c.normalized or "") == "ISO9001" for c in out.certs), out.certs
    assert out.cert_section_present is True
    assert out.persons and out.persons[0].years == Decimal("6")


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
