# -*- coding: utf-8 -*-
"""认证"查询截图"证据双通道判定回归（2026-10-21 重做）。

校准自真实项目四家投标文件（浙江中烟）：
- 龙即/致成有文本标题（"7.1.2 全国认证认可信息公共服务平台查询结果截图" /
  "查询网址 + 查询截图："）→ 真，真实认证分 4/4；
- 诚道的证书页图片本身就是认证平台查询结果截图（OCR 正文含平台名）→ 真，真实 3；
- 先数只有证书扫描件（有"证书编号"但无平台界面）→ 假，真实 1。
"""
from __future__ import annotations


def test_title_line_detected() -> None:
    from app.engine.objective import cert_evidence_present

    pages = [("text", "7.1.2 全国认证认可信息公共服务平台查询结果截图\n- 112 -")]
    ok, basis = cert_evidence_present(pages)
    assert ok and "文本标题" in basis


def test_long_requirement_page_with_query_labels_detected() -> None:
    """致成形态：证书页 1000 字，含"查询网址 / 查询截图："（旧实现整页跳过）。"""
    from app.engine.objective import cert_evidence_present

    body = "查询网址：  http://cx.cnca.cn/CertECloud/result/skipDetail?rzjgId=CNCA-R-2015-189\n" \
           + "证书内容 " * 150 + "\n查询截图：\n41"
    ok, _basis = cert_evidence_present([("text", body)])
    assert ok


def test_toc_line_not_counted() -> None:
    from app.engine.objective import cert_evidence_present

    pages = [("text", "7.1.2 全国认证认可信息公共服务平台查询结果截图............112")]
    ok, _ = cert_evidence_present(pages)
    assert not ok


def test_requirement_restatement_not_counted() -> None:
    """索引表复述"须提供查询结果截图，未提供不得分"不算投标人提供了截图。"""
    from app.engine.objective import cert_evidence_present

    pages = [("text", "投标人须提供有效期内的认证证书及认证平台查询结果截图，未按要求提供不得分的处理")]
    ok, _ = cert_evidence_present(pages)
    assert not ok


def test_ocr_platform_fingerprint_detected() -> None:
    """诚道形态：证书页图片就是平台查询结果截图（OCR 正文含平台名）。"""
    from app.engine.objective import cert_evidence_present

    ocr_text = (
        "360极速浏览器 帮助\n国家市场监督管理总局 全国认证认可信息公共服务平台 认证规则\n"
        "当前位置：认证结果 / 管理体系认证\n证书编号：02623Q30431R5M-HZ\n"
        "杭州诚道科技股份有限公司"
    )
    ok, basis = cert_evidence_present([("ocr_img", ocr_text)])
    assert ok and "OCR" in basis


def test_certificate_scan_without_platform_not_detected() -> None:
    """先数形态：证书扫描件 OCR 正文（有"证书编号/注册号"但不是平台界面）。"""
    from app.engine.objective import cert_evidence_present

    ocr_text = (
        "信息技术服务管理体系认证证书\n(正本)\n兹证明\n北京先进数通信息技术股份公司\n"
        "统一社会信用代码：91110108801146341D\n已按照 ISO/IEC 20000-1:2018 标准要求\n"
        "注册号：01222IS0725R3L 颁证日期：2022.09.14 有效期至：2025.09.13\n"
        "本证书信息可在国家认证认可监督管理委员会官方网站上查询"
    )
    ok, _ = cert_evidence_present([("ocr_img", ocr_text)])
    assert not ok, "证书扫描件不等于平台查询截图（真实评审也是按无截图给先数 1 分）"


def test_text_channel_does_not_use_ocr_fingerprints() -> None:
    """通道 B 只对 OCR 页生效：文本层里出现平台名不单独作为证据。"""
    from app.engine.objective import cert_evidence_present

    ok, _ = cert_evidence_present([("text", "全国认证认可信息公共服务平台 认证结果 证书状态")])
    assert not ok
