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
