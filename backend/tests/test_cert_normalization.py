# -*- coding: utf-8 -*-
"""证书名归一与 OCR 形态的证书计数回归。

2026-10-21 真实故障：OCR 从未生效时看不出这个问题；OCR 修复后证书文本变成
"ISO/IEC 20000-1:2018""GB/T19001-2016/ISO9001:2015""信息技术服务管理体系
认证证书"等形态，旧实现（行首 ISO/CMMI + `"ISO20000" in cert.upper()`）全部
匹配不上，有证书也计 0。
"""
from __future__ import annotations

from decimal import Decimal

from app.engine.facts import BidFacts, extract_bid_facts


def test_normalize_cert_truth_table() -> None:
    from app.engine.objective import normalize_cert

    cases = {
        "ISO/IEC 20000-1:2018": "ISO20000",
        "ISO9001": "ISO9001",
        "ISO27001信息安全管理体系认证": "ISO27001",
        "GB/T19001-2016/ISO9001:2015": "ISO9001",
        "质量管理体系认证证书": "ISO9001",
        "信息安全管理体系认证证书": "ISO27001",
        "信息技术服务管理体系认证证书": "ISO20000",
        "CMMI 三级": "CMMI",
        "CMMI5级": "CMMI",
    }
    for raw, expected in cases.items():
        assert normalize_cert(raw) == expected, raw


def test_cert_facts_from_ocr_text() -> None:
    """OCR 出的证书正文：中文证书名与 ISO/IEC 编号都要收进 certificates。"""
    text = """信息技术服务管理体系认证证书
(正本)
兹证明
北京先进数通信息技术股份公司
已按照
ISO/IEC 20000-1:2018
标准要求建立并实施了信息技术服务管理体系。

质量管理体系认证证书
注册号：016ZB25Q30420R7L
质量管理体系符合GB/T19001-2016/ISO9001:2015标准
"""
    facts = extract_bid_facts(text)
    assert "ISO20000" in facts.certificates
    assert "ISO9001" in facts.certificates
    # 同一体系两种写法（中文名 + GB/T 编号同行）只算一项
    assert len([c for c in facts.certificates if c == "ISO9001"]) == 1


def test_cert_claim_not_counted_as_certificate() -> None:
    """正文声称"已通过 ISO9001"不算证书复印件（仍进 cert_claims）。"""
    facts = extract_bid_facts("我公司已通过ISO9001质量管理体系认证（证书编号：QMS-2023-0455）。")
    assert facts.certificates == []
    assert facts.cert_claims


def test_score_count_uses_normalized_certificates() -> None:
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    facts.certificates = ["ISO/IEC 20000-1:2018", "GB/T19001-2016/ISO9001:2015", "CMMI 三级"]
    crit = {"name": "投标人认证及资质情况", "max_score": Decimal("4"),
            "required_artifacts": "证书复印件"}
    result = score_count(crit, {"field": "certificates", "per_item": "1", "max_items": "3"}, facts)
    assert result.score == Decimal("3")
    assert result.unresolved is None


def test_score_count_cert_screenshot_gate_zeroes_iso_keeps_cmmi() -> None:
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    facts.certificates = ["ISO9001", "ISO27001", "ISO20000", "CMMI"]
    crit = {
        "name": "投标人认证及资质情况", "max_score": Decimal("4"),
        "required_artifacts": "查询结果截图", "cert_section_present": False,
    }
    result = score_count(crit, {"field": "certificates", "per_item": "1", "max_items": "3"}, facts)
    assert result.score == Decimal("1"), "无查询截图 → ISO 三项 0，CMMI 照计"
