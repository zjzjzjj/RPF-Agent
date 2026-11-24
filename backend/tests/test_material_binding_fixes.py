# -*- coding: utf-8 -*-
"""PR1 材料链绑定修复契约测试（2026-11-24 run155 实证）。

三病灶：①业绩材料绑定断链（键名+三态）②证书↔查询截图断链（盲态/ISO20000）
③CMMI 四层识别失败。红线：确认缺判 0 保留；未核实计入+疑问；绝不静默 0。
"""
from __future__ import annotations

from decimal import Decimal


# ---- ②截图盲态：OCR 噪声截图页 ≠ 确认无 ---------------------------------

def test_detect_cert_section_noisy_scan_is_blind_not_false() -> None:
    """run155 咏数实证：截图页是扫描图+OCR 噪声文本 → 盲态 None，
    不得判 False 清零 ISO。"""
    from app.engine.objective import detect_cert_section

    pages = [
        {"document_id": 1, "page_no": 1, "source": "ocr", "known_blank": False,
         "embedded_image_ratio": 0.55, "text": "第1页 证书图片 装订线"},
        {"document_id": 1, "page_no": 2, "source": "ocr", "known_blank": False,
         "embedded_image_ratio": 0.6, "text": "第2页 装订线 页码 认证平台打印件"},
    ]
    det = detect_cert_section(pages)
    assert det.state is None, det.basis


def test_detect_cert_section_readable_no_signal_is_false() -> None:
    """可读长文本页无任何截图信号 → False（确认未见，清零口径保留）。"""
    from app.engine.objective import detect_cert_section

    pages = [
        {"document_id": 1, "page_no": 1, "source": "text", "known_blank": False,
         "embedded_image_ratio": 0.0,
         "text": "质量管理体系认证证书" * 30 + "本证书有效期至2027年"},
    ]
    det = detect_cert_section(pages)
    assert det.state is False


# ---- ②ISO20000 名称匹配 -------------------------------------------------

def test_find_cert_copy_pages_iso20000_variant() -> None:
    """归一化尾 6 字对 'ISO/IEC 20000-1:2018' 零命中（旧口径）——新模式组命中。"""
    from app.engine.objective import find_cert_copy_pages

    pages = [("text", "ISO/IEC 20000-1:2018 信息技术服务管理体系认证证书 证书编号 001 发证 有效期至2027")]
    hits = find_cert_copy_pages(pages, "ISO20000")
    assert hits, "ISO20000 复印件页必须能绑上"
    assert hits[0][0] == "copy"


def test_find_cert_copy_pages_chinese_alias() -> None:
    from app.engine.objective import find_cert_copy_pages

    pages = [("text", "信息技术服务管理体系认证证书（复印件） 证书编号 ABC-2024 发证日期 2024 年 3 月 有效期至 2027 年")]
    hits = find_cert_copy_pages(pages, "ISO20000")
    assert hits and hits[0][0] == "copy"


# ---- ③CMMI：声称行兜底 + 归一化 ------------------------------------------

def test_normalize_cert_cmmi_aliases() -> None:
    from app.engine.objective import normalize_cert

    assert normalize_cert("CMMI 三级") == "CMMI"
    assert normalize_cert("能力成熟度3级") == "CMMI"
    assert normalize_cert("CSMM3级") == "CMMI"


def test_cmmi_claims_fallback_counts_with_concern() -> None:
    """声称行（已通过 CMMI…）不是复印件，但必须被看到：计入+疑问，绝不静默 0。"""
    from app.engine.facts import BidFacts
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    facts.cert_claims = ["CMMI三级评估"]
    criterion = {"score_type": "count", "name": "CMMI", "max_score": "2",
                 "params": {"field": "cmmi", "per_item": "2"},
                 "description": "具有有效期内 CMMI 认证二级及以上得 2 分"}
    result = score_count(criterion, criterion.get("params") or {}, facts)
    assert result.score == Decimal("2") or result.score == Decimal("2.00")
    assert result.conflict is True
    assert "声称" in (result.detail or "")


def test_cmmi_absent_confirmed_still_zero() -> None:
    """红线：清单非空且确认无 CMMI → 0 分保留（confirmed_absent）。"""
    from app.engine.facts import BidFacts
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    facts.certificates = ["ISO9001"]
    criterion = {"score_type": "count", "name": "CMMI", "max_score": "2",
                 "params": {"field": "cmmi", "per_item": "2"},
                 "description": "具有有效期内 CMMI 认证二级及以上得 2 分"}
    result = score_count(criterion, criterion.get("params") or {}, facts)
    assert result.score == Decimal("0") or result.score == Decimal("0.00")
    assert result.zero_reason == "confirmed_absent"


# ---- ②硬闸：盲态不清零 ---------------------------------------------------

def test_screenshot_gate_blind_counts_with_concern() -> None:
    """cert_section_present=None（盲态）：ISO 照计并标疑问，不得清零。"""
    from app.engine.facts import BidFacts
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    facts.certificates = ["ISO9001", "ISO27001", "ISO20000"]
    criterion = {
        "score_type": "count", "name": "体系认证", "max_score": "3",
        "params": {"field": "certificates", "per_item": "1"},
        "description": "每提供一项有效期内的认证证书得1分，最高3分",
        "required_artifacts": "证书复印件及查询结果截图",
        "cert_section_present": None,
    }
    result = score_count(criterion, criterion.get("params") or {}, facts)
    assert (result.score or 0) > 0, "盲态不得清零 ISO"
    assert result.conflict is True
    assert "截图节不可读" in (result.detail or "")


def test_screenshot_gate_confirmed_missing_zeroes_iso() -> None:
    """红线：cert_section_present=False（可读确认无截图）→ ISO 清零保留。"""
    from app.engine.facts import BidFacts
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    facts.certificates = ["ISO9001", "ISO27001", "ISO20000"]
    criterion = {
        "score_type": "count", "name": "体系认证", "max_score": "3",
        "params": {"field": "certificates", "per_item": "1"},
        "description": "每提供一项有效期内的认证证书得1分，最高3分",
        "required_artifacts": "证书复印件及查询结果截图",
        "cert_section_present": False,
    }
    result = score_count(criterion, criterion.get("params") or {}, facts)
    assert (result.score or 0) == 0
    assert result.zero_reason in ("confirmed_absent", "unreadable", "no_facts")


# ---- PR3/PR4：存在性接入 locator + 漏评分护栏 ------------------------------

def test_cmmi_locator_record_when_text_misses() -> None:
    """扫描 CMMI 页无 cert_context 词被文本路跳过（run155 c45 全 0 残余场景）
    → 目录列示的 CMMI 窗口登记为存在性记录（计入+疑问，不静默 0）。"""
    from app.engine.material_inventory import build_inventory

    toc = {"document_id": 1, "page_no": 1, "source": "text", "known_blank": False,
           "material_tags": [],
           "text": "投标文件目录\nCMMI 能力成熟度证书………30\n其他材料………40"}
    pages = [toc] + [
        {"document_id": 1, "page_no": n, "source": "ocr", "known_blank": False,
         "material_tags": [], "text": ""} for n in range(2, 45)
    ]
    inv = build_inventory(9, pages)
    assert any((c.normalized or "") == "CMMI" for c in inv.certs), inv.certs
    assert "CMMI" in (inv.facts.certificates or [])


def test_detect_cert_section_locator_window_signal() -> None:
    """目录列示'查询截图'窗口 = 节存在信号（文本层看不见时不盲态不判缺）。"""
    from app.engine.objective import detect_cert_section

    pages = [{"document_id": 1, "page_no": 1, "source": "text", "known_blank": False,
              "text": "投标文件正文，无任何截图标题与平台指纹。" * 5}]
    det = detect_cert_section(pages, locator_windows=[(30, 35)])
    assert det.state is True
    assert "目录" in det.basis


_D = Decimal

RULE10 = (
    "投标产品业绩：根据投标人提供自2022年1月1日（合同签订时间为准）至今的同类产品业绩，"
    "要求合同金额100万及以上，每个得1分，最高得2分。"
    "【证明材料】须提供业绩合同、验收证明材料及合同配套的至少一张发票复印件并加盖投标人公章，"
    "以上资料未提供的不得分。"
)


def _perf_rule(description=None, **param_changes):  # noqa: ANN001, ANN202
    params = {"field": "performance", "per_item": "1",
              "min_amount_yuan": "1000000", **param_changes}
    return {"code": "c25", "name": "投标产品业绩", "score_type": "count",
            "max_score": "2", "description": description or RULE10, "params": params}


def test_guard_escalates_missing_to_unverified(monkeypatch) -> None:  # noqa: ANN001
    """护栏：目录列有材料页但绑定未闭合 -> 禁止按缺件判 0（计入+疑问）。"""
    from app.engine.facts import BidFacts
    from app.engine.material_inventory import ContractRecord, MaterialInventory
    from app.engine.objective_fastpath import _score_contracts

    inv = MaterialInventory(
        bidder_id=1, pages=[], full_text="", facts=BidFacts(text=""),
        contracts=[], locator_index={"invoice": {50}, "acceptance": {60}},
        performance_section_found=True,
    )
    record = ContractRecord(
        name="某研究所维保", amount_yuan=_D("1200000"),
        pages=[1], materials={"contract": [1]}, source="table",
        materials_state={"contract": True, "invoice": False, "acceptance": False})
    inv.contracts = [record]
    monkeypatch.setenv("BID_EVAL_MATERIAL_LOCATOR", "on")
    monkeypatch.setenv("BID_EVAL_MATERIAL_GUARD", "1")
    result = _score_contracts(_perf_rule(RULE10), inv)
    assert result.score > 0, "目录列有材料页 -> 禁止按缺件判 0"
    assert any("目录列有材料页" in (w.get("message") or "") for w in result.warnings)
    assert result.verdict == "concern"


def test_guard_zero_reason_material_binding_conflict(monkeypatch) -> None:  # noqa: ANN001
    """护栏升级后 0 分 -> zero_reason=material_binding_conflict（非评审结论）。"""
    from app.engine.facts import BidFacts
    from app.engine.material_inventory import ContractRecord, MaterialInventory
    from app.engine.objective_fastpath import _score_contracts

    inv = MaterialInventory(
        bidder_id=1, pages=[], full_text="", facts=BidFacts(text=""),
        contracts=[], locator_index={"invoice": {50}},
        performance_section_found=True,
    )
    inv.contracts = [ContractRecord(
        name="某研究所维保", amount_yuan=_D("1200000"), pages=[1],
        materials={"contract": [1]}, source="table",
        materials_state={"contract": True, "invoice": False})]
    monkeypatch.setenv("BID_EVAL_MATERIAL_LOCATOR", "on")
    monkeypatch.setenv("BID_EVAL_MATERIAL_GUARD", "1")
    # 无档位细则 + per_item=0：护栏升级留痕但计分 0 → 零因必须是
    # binding_conflict（0 分不是评审结论）
    no_tier_rule = ("投标产品业绩：每个得1分，最高得2分。【证明材料】须提供"
                    "业绩合同、验收证明及发票复印件，以上资料未提供的不得分。")
    result = _score_contracts(_perf_rule(no_tier_rule, per_item="0"), inv)
    assert result.score == 0
    assert result.zero_reason == "material_binding_conflict", result.zero_reason


def test_guard_off_keeps_confirmed_absence_zero(monkeypatch) -> None:  # noqa: ANN001
    """护栏关闭：确认缺件仍判 0（红线）。"""
    from app.engine.facts import BidFacts
    from app.engine.material_inventory import ContractRecord, MaterialInventory
    from app.engine.objective_fastpath import _score_contracts

    inv = MaterialInventory(
        bidder_id=1, pages=[], full_text="", facts=BidFacts(text=""),
        contracts=[], locator_index={"invoice": {50}},
        performance_section_found=True,
    )
    inv.contracts = [ContractRecord(
        name="某研究所维保", amount_yuan=_D("1200000"), pages=[1],
        materials={"contract": [1]}, source="table",
        materials_state={"contract": True, "invoice": False, "acceptance": False})]
    monkeypatch.setenv("BID_EVAL_MATERIAL_LOCATOR", "on")
    monkeypatch.setenv("BID_EVAL_MATERIAL_GUARD", "0")
    result = _score_contracts(_perf_rule(RULE10), inv)
    assert result.score == 0
    assert "未提供的不得分" in result.detail
