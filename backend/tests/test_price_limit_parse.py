"""R46-A：限价原文解析、键名归一、税口径决策表（价格类误否决修复）。

parse 形态全部来自评分细则语料台账的通用句式（非项目拟合）。
"""
from __future__ import annotations

from decimal import Decimal

from app.engine.price_limit import (
    align_tax_basis,
    infer_math_scope,
    parse_price_limit_text,
    resolve_limit_keys,
)

D = Decimal


# ---------------------------------------------------------------------------
# parse_price_limit_text：细则原文确定性解析
# ---------------------------------------------------------------------------
def test_parse_total_with_tax_and_rate():
    """测试6 原文形态：187.2 万元（人民币，□不含税 ☑含税，税率6%）。"""
    text = "评审因素：最高限价为：187.2 万元（人民币，□不含税 ☑含税，税率 6 %）超过最高限价的投标文件将被否决。"
    out, warn = parse_price_limit_text(text)
    assert warn is None
    assert out is not None
    assert out["max_amount_yuan"] == "1872000"
    assert out["price_includes_tax"] is True  # ☑含税（勾选框优先）
    assert out["tax_rate"] == "6"


def test_parse_wan_yuan_excl_tax():
    """语料形态：411.50万元(不含税)。"""
    out, warn = parse_price_limit_text("最高投标限价 411.50万元(不含税)，超过的投标无效")
    assert warn is None
    assert out is not None
    assert out["max_amount_yuan"] == "4115000"
    assert out["price_includes_tax"] is False


def test_parse_yearly_caps():
    """测试2 原文形态：分年度限价（含税6%）。"""
    text = (
        "最高限价为：第一年度151.98 万元，第二年度168.32 万元，"
        "第三年度184.66 万元（含税，税率6%）超过最高限价的投标文件将被否决。"
    )
    out, warn = parse_price_limit_text(text)
    assert warn is None
    assert out is not None
    assert out["yearly_max_amount_yuan"] == ["1519800", "1683200", "1846600"]
    assert out["price_includes_tax"] is True
    assert out["tax_rate"] == "6"
    assert "max_amount_yuan" not in out  # 年度形态不冒充总价


def test_parse_unit_price_kind():
    """语料形态：98230.00元/万套 → 单价型，不产出总价限价。"""
    out, warn = parse_price_limit_text("最高限价 98230.00元/万套，超过即否决")
    assert warn is None
    assert out == {"price_limit_kind": "unit_price"}


def test_parse_ambiguous_multiple_totals_returns_none():
    """多个不等总价限价 → 歧义不猜（None + warning）。"""
    out, warn = parse_price_limit_text("最高限价187.2万元；控制价200万元")
    assert out is None
    assert warn is not None and "不等" in warn


def test_parse_no_limit_context_returns_none():
    """无限价语境的普通金额段不解析（防止把报价当限价）。"""
    out, warn = parse_price_limit_text("投标总价：1796700 元，税率6%")
    assert out is None
    assert warn is None


def test_parse_limit_word_without_amount_returns_none():
    """限价条款句但原文没写金额（"超过最高限价的投标文件将被否决"）→ None。"""
    out, warn = parse_price_limit_text("投标报价超过最高限价的，其投标将被否决")
    assert out is None
    assert warn is None


# ---------------------------------------------------------------------------
# resolve_limit_keys：键名归一（双键名消灭）
# ---------------------------------------------------------------------------
def test_resolve_keys_alias_equivalence():
    """max_yuan/max 别名与规范键 max_amount_yuan 归一后等价。"""
    assert resolve_limit_keys({"max_amount_yuan": "1872000"})[0] == D("1872000")
    assert resolve_limit_keys({"max_yuan": "1872000"})[0] == D("1872000")
    assert resolve_limit_keys({"max": "1872000"})[0] == D("1872000")
    # 规范键优先
    assert resolve_limit_keys({"max_amount_yuan": "1872000", "max_yuan": "999"})[0] == D("1872000")
    # 非正/非法 → None
    assert resolve_limit_keys({"max_yuan": "0"})[0] is None
    assert resolve_limit_keys({"max_yuan": "abc"})[0] is None


def test_resolve_keys_yearly_and_tax():
    total, yearly, incl, rate, kind = resolve_limit_keys(
        {
            "yearly_max_amount_yuan": ["1519800", "1683200", "1846600"],
            "price_includes_tax": True,
            "tax_rate": "6",
        }
    )
    assert total is None
    assert yearly == [D("1519800"), D("1683200"), D("1846600")]
    assert incl is True
    assert rate == D("6")
    assert kind is None


def test_infer_scope_from_text_and_params():
    """范围推断：限价条款→{limit}；算术语→{arith}；显式限价键→limit。"""
    assert infer_math_scope({"name": "投标报价不得超过最高限价"}) == frozenset({"limit"})
    assert infer_math_scope({"name": "投标总价大写与小写金额应一致"}) == frozenset({"arith"})
    assert infer_math_scope({"name": "响应文件签署", "params": {}}) == frozenset()
    assert infer_math_scope({"name": "响应文件签署", "params": {"max_yuan": "1"}}) == frozenset({"limit"})
    # c27 型："清单分项报价"不触发 arith（刻意收窄，防误归因）
    assert infer_math_scope(
        {"name": "投标报价最高限价及清单分项报价",
         "source_quote": "投标报价超过招标文件中的最高限价的，以及采用工程量清单招标的工程项目超过清单分项报价的；"}
    ) == frozenset({"limit"})


# ---------------------------------------------------------------------------
# align_tax_basis：口径决策表（不对称停比、双盲照比）
# ---------------------------------------------------------------------------
def test_align_both_blind_compares():
    """双盲（双方都未知）→ 直接比较（普通标书常态，用户拍板）。"""
    aligned, ok = align_tax_basis(D("1796700"), None, D("1872000"), None, None)
    assert ok is True and aligned == D("1796700")


def test_align_same_basis_compares():
    aligned, ok = align_tax_basis(D("1796700"), True, D("1872000"), True, None)
    assert ok is True and aligned == D("1796700")


def test_align_asymmetric_stops():
    """单边已知 → 停比（不含税限价×含税报价的系统性误否决防线）。"""
    assert align_tax_basis(D("1796700"), True, D("1872000"), None, None)[1] is False
    assert align_tax_basis(D("1796700"), None, D("1872000"), True, None)[1] is False


def test_align_convert_with_rate():
    """口径不同 + 税率可得 → 换算到限价口径后比较。
    报价不含税 4,115,000、限价含税（6%）→ 对齐 4,361,900。"""
    aligned, ok = align_tax_basis(D("4115000"), False, D("5000000"), True, D("6"))
    assert ok is True
    assert aligned == D("4361900.00")
    # 反向：报价含税、限价不含税 → ÷1.06
    aligned2, ok2 = align_tax_basis(D("4361900"), True, D("4115000"), False, D("6"))
    assert ok2 is True
    assert aligned2 == D("4115000.00")


def test_align_different_basis_no_rate_stops():
    """口径不同 + 税率不可得 → 停比。"""
    assert align_tax_basis(D("1796700"), True, D("1872000"), False, None)[1] is False


# ---------------------------------------------------------------------------
# _check_math 集成：原文解析兜底、冲突以原文为准、口径停比
# ---------------------------------------------------------------------------
def test_check_math_parses_limit_from_clause_text():
    """params 无限价 → 从条款原文解析（测试6 形态）→ 正常比对。"""
    from app.engine.facts import extract_bid_facts
    from app.engine.objective import run_check

    facts = extract_bid_facts("")
    facts.bid_price = D("1796700")
    facts.bid_price_includes_tax = True
    c = {
        "code": "c13",
        "name": "投标报价不得超过最高限价",
        "params": {},
        "source_quote": "最高限价为：187.2 万元（人民币，□不含税 ☑含税，税率 6 %）超过最高限价的投标文件将被否决。",
    }
    result = run_check("math_relation", {}, facts, criterion=c)
    assert result.status == "pass"
    assert "1,872,000" in result.message
    assert "未作限价比对" not in result.message


def test_check_math_params_conflict_prefers_text():
    """params 与原文不等 → 以原文为准并留痕 warning。"""
    from app.engine.facts import extract_bid_facts
    from app.engine.objective import run_check

    facts = extract_bid_facts("")
    facts.bid_price = D("1796700")
    facts.bid_price_includes_tax = True
    c = {
        "code": "c13",
        "name": "投标报价不得超过最高限价",
        "params": {"max_amount_yuan": "1000000"},  # 规则包里错的限价
        "source_quote": "最高限价为：187.2 万元（人民币，☑含税，税率 6 %）",
    }
    result = run_check("math_relation", c["params"], facts, criterion=c)
    # 原文 1,872,000 为准：1,796,700 ≤ 1,872,000 → pass（若按 params 100 万会误 fail）
    assert result.status == "pass"
    assert "以细则原文解析为准" in result.message


def test_check_math_asymmetric_tax_stops_comparison():
    """限价口径已知（含税）、报价口径未知 → 停比 pass+待核对（不对称停比）。"""
    from app.engine.facts import extract_bid_facts
    from app.engine.objective import run_check

    facts = extract_bid_facts("")
    facts.bid_price = D("1796700")
    facts.bid_price_includes_tax = None  # 口径盲
    c = {
        "code": "c13",
        "name": "投标报价不得超过最高限价",
        "params": {"max_amount_yuan": "1872000", "price_includes_tax": True},
    }
    result = run_check("math_relation", c["params"], facts, criterion=c)
    assert result.status == "pass"
    assert result.need_human is True
    assert "口径不对称" in result.message


def test_check_math_unit_price_kind_skips_total_compare():
    """单价型限价 → 不拿单价当总价比（skipped 注记）。"""
    from app.engine.facts import extract_bid_facts
    from app.engine.objective import run_check

    facts = extract_bid_facts("")
    facts.bid_price = D("1796700")
    c = {
        "code": "c13",
        "name": "投标报价不得超过最高限价",
        "params": {"price_limit_kind": "unit_price"},
    }
    result = run_check("math_relation", c["params"], facts, criterion=c)
    assert result.status == "pass"
    assert "单价型限价" in result.message
