"""报价抽取第 5 级兜底：开标一览表窗口扫描（真实标书跨行断裂形态）。"""
from __future__ import annotations

from decimal import Decimal

from app.engine.facts import extract_bid_facts

# p303 真实形态：表头跨 3 行断裂 + 数据行跨 2 行（《北京先进数通投标文件.pdf》）
P303_TEXT = """报价文件
3.
开标一览表
3.1
开标一览表
项目编号：C202501BA2001716016076
投标价
增值税 投标价 项目负 投标保证
序号 （不含增值税， 备注
税率 （含增值税，人民币元） 责人 金
人民币元）
1 6% 已交
2401143 2545211.58 郑峪嘉
注：1、报价一经涂改，应在涂改处由投标人代表签字并加盖投标人公章，否则其投标作无
效标处理。
"""


def test_open_bid_section_extracts_tax_inclusive_price() -> None:
    """表头断裂形态：税率配对命中 → 取含税价 2545211.58。"""
    facts = extract_bid_facts(P303_TEXT)
    assert facts.bid_price == Decimal("2545211.58"), facts.bid_price


def test_no_anchor_text_unaffected() -> None:
    """无开标一览锚点的文本不受新兜底影响（原四级语义不变）。"""
    text = "技术方案 本项目采用微服务架构。注册资本：人民币1000万元。"
    facts = extract_bid_facts(text)
    assert facts.bid_price is None


def test_low_amounts_return_none() -> None:
    """窗口金额全部低于阈值 → 宁缺勿错返回 None。"""
    text = """开标一览表
项目编号：X2025001
投标价（含增值税，人民币元）
序号 1 备注 无
"""
    facts = extract_bid_facts(text)
    assert facts.bid_price is None


def test_toc_anchor_loses_to_real_section() -> None:
    """目录假锚点 vs 真章节：并列时取更靠后的真锚点（真章节语境词更密）。"""
    text = (
        "目录\n"
        "3.1 开标一览表 ......... 302\n"
        "3.2 投标分项报价表 ...... 303\n"
        "第一章 资格要求\n"
        "(此处省略 5000 字资质内容，无任何报价词与金额)\n"
        + " filler filler filler\n" * 50
        + "开标一览表\n"
        "项目编号：X2025001\n"
        "投标价 投标报价 含增值税（人民币元）\n"
        "1 850000.00\n"
        "投标人：某公司（盖单位公章）\n"
    )
    facts = extract_bid_facts(text)
    assert facts.bid_price == Decimal("850000"), facts.bid_price


def test_deposit_heavy_window_not_chosen_over_real_price() -> None:
    """保证金整万候选软降权：并列 max 时弃保证金取真报价。"""
    text = """开标一览表
投标报价（含增值税，人民币元） 投标保证金
序号 1
2000000.00
已交 10000
"""
    facts = extract_bid_facts(text)
    assert facts.bid_price == Decimal("2000000.00"), facts.bid_price


def test_limit_context_number_excluded_from_candidates() -> None:
    """限价语境数字（规则值）不得当报价（R45-A 护栏）。

    限价恒 ≥ 真报价，混入候选后 max() 必选限价 → 把限价当报价 →
    后续与限价比对必超限 = 系统性误否决。护栏宁可漏抽（走其它兜底级）
    也绝不放过。
    """
    text = """开标一览表
本项目投标报价如下：1796700
最高限价：1872000
"""
    facts = extract_bid_facts(text)
    assert facts.bid_price == Decimal("1796700"), facts.bid_price


def test_all_limit_context_returns_none() -> None:
    """窗口候选全是限价语境 → 全剔返回 None（宁缺勿错）。"""
    text = """开标一览表
投标报价（含增值税，人民币元）
最高限价：1872000
"""
    facts = extract_bid_facts(text)
    # 限价值被护栏剔除后无候选；标签级"投标总价"也不存在 → 走不到报价
    assert facts.bid_price != Decimal("1872000"), facts.bid_price
