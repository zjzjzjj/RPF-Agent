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


# ---------------------------------------------------------------------------
# classify_cert_backing（2026-11-02 P0-5 证书背书三态软闸门）
# ---------------------------------------------------------------------------
def _facts_with_certs(*names: str):
    from app.engine.facts import BidFacts

    return BidFacts(text="", certificates=list(names))


def test_cert_backing_confirmed_when_section_present() -> None:
    """查询截图/平台指纹确认存在 → True（正常计入）。"""
    from app.engine.objective import classify_cert_backing

    fb = classify_cert_backing(
        _facts_with_certs("ISO9001质量管理体系认证证书"),
        [("text", "查询结果截图页 " * 20)],
        (True, "文本标题：查询结果截图"),
    )
    assert fb["ISO9001"] is True


def test_cert_backing_none_on_low_evidence_page_not_killed() -> None:
    """run77 c38 防误杀：p39 只有标题、证书是扫描图（OCR 输出过少）→ None
    （计入+疑问），不得判 False 清零——截图文本层看不见≠没提供。"""
    from app.engine.objective import classify_cert_backing

    fb = classify_cert_backing(
        _facts_with_certs("ITSS信息技术服务标准符合性证书"),
        [("ocr", "2.7.1 ITSS 信息技术服务标准符合性证书\n39")],  # <30 字
        (False, "未见查询截图标题或认证平台指纹"),
    )
    from app.engine.objective import normalize_cert

    assert fb[normalize_cert("ITSS信息技术服务标准符合性证书")] is None  # 低证据页→不误杀


def test_cert_backing_false_when_readable_text_confirms_absent() -> None:
    """可读文本确认无背书页（无低证据页、明示未见）→ False（不计入）。
    run77 c39 反向：管理体系证书没截图却给满 2 分的虚高方向。"""
    from app.engine.objective import classify_cert_backing

    fb = classify_cert_backing(
        _facts_with_certs("ISO9001质量管理体系认证证书", "ISO14001环境管理体系认证"),
        [("text", "这是完全可读的文本页，没有任何证书复印件或截图内容。" * 5)],
        (False, "未见查询截图标题或认证平台指纹"),
    )
    assert fb["ISO9001"] is False
    assert fb["ISO14001"] is False


def test_cert_backing_unknown_without_section_info() -> None:
    """cert_section 信息缺失 → None（不猜，旧链路不误杀）。"""
    from app.engine.objective import classify_cert_backing

    fb = classify_cert_backing(
        _facts_with_certs("CMMI5级证书"), [("text", "随便的可读文本。" * 10)], None
    )
    assert list(fb.values()) == [None]  # 信息不足 → 不猜


def test_score_count_tier_backing_gate() -> None:
    """count_tier 接闸门：False 不计、None 计入+疑问、无 backing 照计。"""
    from decimal import Decimal

    from app.engine.facts import BidFacts
    from app.engine.objective import score_count_tier

    from app.engine.objective import normalize_cert as _nc

    crit = {
        "max_score": 2,
        "cert_backing": {
            _nc("ISO9001质量管理体系认证证书"): True,
            _nc("ISO14001环境管理体系认证证书"): False,
            _nc("ISO20000信息技术服务管理体系认证证书"): None,
        },
    }
    params = {"engine": "count_tier", "field": "certificates",
              "tiers": [{"min_count": 3, "score": "2"}, {"min_count": 2, "score": "1"}]}
    facts = BidFacts(text="", certificates=[
        "ISO9001质量管理体系认证证书", "ISO14001环境管理体系认证证书",
        "ISO20000信息技术服务管理体系认证证书",
    ])
    r = score_count_tier(crit, params, facts)
    # False 剔掉 → 2 本计入（True+None）→ 命中 ≥2 档 1 分；None 标疑问
    assert r.score == Decimal("1")
    assert r.unverified_counted == 1
    assert "确认无背书页不计" in r.detail
    # 无 backing 信息 → 旧口径照计（3 本 → ≥3 档 2 分）
    crit2 = {"max_score": 2}
    r2 = score_count_tier(crit2, params, facts)
    assert r2.score == Decimal("2")


# --- R3-4 逐本证书背书（2026-11-03 run78 云平台证书 5/5 全计满实证） --------

def test_cert_backing_per_cert_copy_page_true() -> None:
    """用友式：复印件页可读（证书名+发证/编号特征）→ True 计入。"""
    from app.engine.objective import classify_cert_backing, normalize_cert

    pages = [
        ("text", "证书清单表：阿里云产品认证证书 云平台运维培训证书 列表行。" * 3),
        ("ocr", "阿里云产品认证证书 证书编号 ALY-2019-001 发证 2019年 有效期至2027年"),
    ]
    fb = classify_cert_backing(_facts_with_certs("阿里云产品认证证书"), pages, None)
    assert fb[normalize_cert("阿里云产品认证证书")] is True


def test_cert_backing_per_cert_claim_only_with_scans_none() -> None:
    """用友式（防误杀）：只有声称行、文档有扫描页 → None 计入+疑问，不得清零。"""
    from app.engine.objective import classify_cert_backing, normalize_cert

    pages = [
        ("text", "证书清单表：华为云认证证书 培训证书 列表行。" * 3),
        ("ocr_img", "扫描图片页，OCR 输出很少"),  # <30 字低证据 → has_scan
    ]
    fb = classify_cert_backing(_facts_with_certs("华为云认证证书"), pages, None)
    assert fb[normalize_cert("华为云认证证书")] is None


def test_cert_backing_per_cert_claim_only_readable_false() -> None:
    """德勤式（不许满额）：全文可读、证书只有声称行无复印件 → False 不计。
    run78 云平台「提供两项得1分」用证书清单声称凑数的反例。"""
    from app.engine.objective import classify_cert_backing, normalize_cert

    pages = [
        ("text", "证书清单表：云平台运维培训证书（声称） 另一本证书（声称） 列表行。" * 3),
        ("text", "公司简介页，全部内容文本层可读，无任何复印件或证书编号内容。" * 3),
    ]
    fb = classify_cert_backing(_facts_with_certs("云平台运维培训证书"), pages, None)
    assert fb[normalize_cert("云平台运维培训证书")] is False


def test_cert_backing_per_cert_low_hit_none() -> None:
    """命中候选页但低证据（扫描图看不清）→ None（德勤式：不可读不许满额，
    同时不误杀——计入+疑问）。"""
    from app.engine.objective import classify_cert_backing, normalize_cert

    pages = [
        ("text", "证书清单表：ITSS信息技术服务标准符合性证书 列表行。" * 3),
        ("ocr", "ITSS 证书"),
    ]
    fb = classify_cert_backing(_facts_with_certs("ITSS信息技术服务标准符合性证书"), pages, None)
    assert fb[normalize_cert("ITSS信息技术服务标准符合性证书")] is None
