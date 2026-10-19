"""确定性引擎单测：价格公式（手算对照）、排名并列、事实抽取、校验器。

每一个价格公式用例都有独立手算的期望值——这是"客观分复算一致性 0 差异"验收的基石。
"""
from __future__ import annotations

from decimal import Decimal

from app.engine.aggregate import rank_rows
from app.engine.facts import extract_bid_facts, parse_upper_amount
from app.engine.models import PriceBid, RankRow
from app.engine.price import compute_benchmark, compute_price_scores
from app.rules.validators import run_validators

D = Decimal


# ---------------------------------------------------------------------------
# 大写金额解析
# ---------------------------------------------------------------------------
def test_parse_upper_amount():
    assert parse_upper_amount("人民币柒佰陆拾万元整") == D("7600000")
    assert parse_upper_amount("柒佰零伍万元整") == D("7050000")
    assert parse_upper_amount("壹仟贰佰万元整") == D("12000000")
    # 角/分按小数处理，不丢精度
    assert parse_upper_amount("人民币壹万贰仟叁佰肆拾伍元陆角柒分") == D("12345.67")
    assert parse_upper_amount("没有大写金额 123.45") is None


# ---------------------------------------------------------------------------
# 价格分（手算对照）
# ---------------------------------------------------------------------------
def _formula() -> dict:
    return {
        "formula_type": "benchmark_ratio",
        "benchmark_method": "average",
        "k": "0.95",
        "max_score": "40",
        "upper_bound": "40",
        "rounding": 2,
        "exclude_invalid": True,
    }


def test_price_sample_case():
    """样例数据真值：基准价 6,863,750；甲 36.13 / 乙 40.00 / 丁 36.61 / 戊 36.13 / 丙 39.22。"""
    bids = [
        PriceBid(1, "华信", D("7600000")),
        PriceBid(2, "云速", D("6200000")),
        PriceBid(3, "恒通", D("7000000"), excluded=True),  # 建议否决 → 不进基准价
        PriceBid(4, "智联", D("7500000")),
        PriceBid(5, "方舟", D("7600000")),
    ]
    comp = compute_price_scores(bids, _formula())

    assert comp.benchmark == D("6863750.00")
    assert "×0.95" in comp.formula_explanation
    by_id = {r.bidder_id: r for r in comp.results}
    assert by_id[1].price_score == D("36.13")
    assert by_id[2].price_score == D("40.00")  # 44.28 封顶
    assert by_id[3].price_score == D("39.22")  # 不进基准价但仍算分
    assert by_id[3].excluded_from_benchmark is True
    assert by_id[4].price_score == D("36.61")
    assert by_id[5].price_score == D("36.13")


def test_price_single_bidder():
    comp = compute_price_scores([PriceBid(1, "唯一", D("5000000"))], _formula())
    assert comp.benchmark == D("4750000.00")  # 5000000×0.95
    # 基准价 = 报价×0.95，故唯一投标人得 40×0.95 = 38（价格分 = 满分×基准价/评标价）
    assert comp.results[0].price_score == D("38.00")


def test_price_all_same():
    bids = [PriceBid(i, f"b{i}", D("1000000")) for i in (1, 2, 3)]
    comp = compute_price_scores(bids, _formula())
    assert all(r.price_score == D("38.00") for r in comp.results)  # 40×0.95


def test_price_no_valid_bids():
    bids = [PriceBid(1, "甲", D("100"), excluded=True)]
    comp = compute_price_scores(bids, _formula())
    assert comp.benchmark is None
    assert comp.results[0].price_score == D("0")


def test_price_deviation_deduct():
    """偏离扣分型：满分 40，以最低价为基准，每偏离 1% 扣 1 分。"""
    formula = {
        "formula_type": "deviation_deduct", "benchmark_method": "lowest",
        "k": "1", "max_score": "40", "deduct_rate": "1", "rounding": 2,
        "exclude_invalid": False, "lower_bound": "0",
    }
    bids = [PriceBid(1, "平", D("1000000")), PriceBid(2, "高", D("1100000"))]
    comp = compute_price_scores(bids, formula)
    by_id = {r.bidder_id: r for r in comp.results}
    assert by_id[1].price_score == D("40.00")  # 与基准价一致，偏离 0
    assert by_id[2].price_score == D("30.00")  # 偏离 +10% → 40-10


def test_benchmark_average_below():
    # 平均价 103.33，低于平均价的是 100 与 90，其平均 = 95
    base, expl = compute_benchmark([D("100"), D("90"), D("120")], "average_below", D("1"))
    assert base == D("95.00")
    assert "低于平均价" in expl


def test_price_asymmetric_deduct_real_case():
    """不对称扣分率：真实项目（浙江中烟行业烟叶平台运维）5 家报价。

    公式：有效投标人 ≤5 家 → 全部报价算术平均为最佳报价值；等于基准得满分 60；
    每低 1 个百分点扣 0.4 分，每高 1 个百分点扣 0.8 分。

    期望值取自该项目官方《价格评分表》，逐家严格相等——单一 deduct_rate
    表达不了这种上下不同费率，故走 params_json 的 deduct_rate_down/up。
    """
    formula = {
        "formula_type": "deviation_deduct",
        "benchmark_method": "average",
        "k": "1",
        "max_score": "60",
        "rounding": 2,
        "exclude_invalid": False,
        "lower_bound": "0",
        "upper_bound": "60",
        "params_json": {"deduct_rate_down": "0.4", "deduct_rate_up": "0.8"},
    }
    bids = [
        PriceBid(1, "浙江创联", D("1526400.00")),
        PriceBid(2, "厦门海晟融创", D("1796700.00")),
        PriceBid(3, "深圳小河流", D("1800000.00")),
        PriceBid(4, "浩鲸云计算", D("1857999.80")),
        PriceBid(5, "咏数智能", D("1749000.00")),
    ]
    comp = compute_price_scores(bids, formula)
    assert comp.benchmark == D("1746019.96")  # 官方评标基准价
    official = {
        1: "54.97", 2: "57.68", 3: "57.53", 4: "54.87", 5: "59.86",
    }
    got = {r.bidder_id: str(r.price_score) for r in comp.results}
    assert got == official


def test_price_symmetric_rate_unchanged_by_extension():
    """只给 deduct_rate 时两个方向都取它——扩展不得改变既有行为。"""
    formula = {
        "formula_type": "deviation_deduct", "benchmark_method": "lowest",
        "k": "1", "max_score": "60", "deduct_rate": "0.5", "rounding": 2,
        "exclude_invalid": False, "lower_bound": "0", "upper_bound": "60",
    }
    bids = [PriceBid(1, "基准", D("1000000")), PriceBid(2, "高10", D("1100000"))]
    comp = compute_price_scores(bids, formula)
    by_id = {r.bidder_id: r.price_score for r in comp.results}
    assert by_id[1] == D("60.00")   # 与基准价一致，偏离 0
    assert by_id[2] == D("55.00")   # 偏离 +10% → 60-0.5×10（上下同费率）


def test_benchmark_second_lowest():
    base, _ = compute_benchmark([D("100"), D("90"), D("120")], "second_lowest", D("1"))
    assert base == D("100.00")


# ---------------------------------------------------------------------------
# 排名与并列
# ---------------------------------------------------------------------------
def test_rank_basic_order():
    rows = [
        RankRow(1, "甲", D("80.00"), D("760"), D("45"), D("15")),
        RankRow(2, "乙", D("90.00"), D("620"), D("20"), D("10")),
        RankRow(3, "丙", D("70.00"), D("700"), D("40"), D("8")),
    ]
    ranked = rank_rows(rows)
    assert [r.name for r in ranked] == ["乙", "甲", "丙"]
    assert ranked[0].rank == 1


def test_rank_tie_broken_by_price():
    """总分并列 → 报价低者在前。"""
    rows = [
        RankRow(1, "高价甲", D("80"), D("800"), D("45"), D("15")),
        RankRow(2, "低价乙", D("80"), D("700"), D("45"), D("15")),
    ]
    ranked = rank_rows(rows)
    assert ranked[0].name == "低价乙"
    assert ranked[1].tie_break_level == 1
    assert any("报价" in e for e in ranked[1].tie_explanations)


def test_rank_tie_break_chain():
    """总分、报价都相同 → 技术分高者在前；再同 → 名称排序。"""
    rows = [
        RankRow(1, "乙公司", D("80"), D("700"), D("40"), D("15")),
        RankRow(2, "甲公司", D("80"), D("700"), D("45"), D("15")),
    ]
    ranked = rank_rows(rows)
    assert ranked[0].name == "甲公司"
    assert ranked[1].tie_break_level == 2

    rows2 = [
        RankRow(1, "乙公司", D("80"), D("700"), D("45"), D("15")),
        RankRow(2, "甲公司", D("80"), D("700"), D("45"), D("15")),
    ]
    ranked2 = rank_rows(rows2)
    # 全部并列 → 名称字典序（Unicode 码位：乙 U+4E59 < 甲 U+7532）
    assert ranked2[0].name == "乙公司"
    assert ranked2[1].tie_break_level == 4


def test_rank_rounding_before_sort():
    """取整后才比较：80.004 与 80.001 应视为并列（精度 2），由报价分先后。"""
    rows = [
        RankRow(1, "浮点高", D("80.004"), D("800"), D("45"), D("10")),
        RankRow(2, "浮点低", D("80.001"), D("700"), D("45"), D("10")),
    ]
    ranked = rank_rows(rows)
    assert ranked[0].name == "浮点低"  # 总分取整后并列，报价低者在前


def test_rank_veto_still_ranked():
    """建议否决者保留名次供交叉验证（veto_flag 只是标记）。"""
    rows = [
        RankRow(1, "被建议否决", D("88"), D("700"), D("40"), D("15"), veto_flag=True),
        RankRow(2, "正常", D("80"), D("760"), D("45"), D("15")),
    ]
    ranked = rank_rows(rows)
    assert ranked[0].name == "被建议否决"
    assert ranked[0].veto_flag is True


# ---------------------------------------------------------------------------
# 事实抽取
# ---------------------------------------------------------------------------
YI_TEXT = """一、投标函
我方（云速信息技术有限公司）已仔细研究招标文件全部内容，愿以投标总价人民币陆佰贰拾万元整
（¥6,200,000.00）承担本项目的全部工作内容。
二、开标一览表
1 | 政务云平台软件开发 | 2,600,000.00
2 | 硬件设备采购 | 2,000,000.00
 | 合计 | 6,200,000.00
 | 投标总价 | 6,200,000.00
三、资格证明文件
注册资本：人民币2,000万元
1 | 某县政务信息化平台项目 | 3,500,000.00 | 2024年
2 | 某街道智慧社区平台项目 | 3,200,000.00 | 2023年
（五）投标保证金缴纳凭证
我公司已通过银行转账方式缴纳投标保证金人民币5万元（¥50,000.00），转账凭证附后。
（六）体系认证证书
1 | ISO9001质量管理体系认证 | QMS-2023-0455 | 2026年12月
2 | CMMI3级认证 | CMMI-DEV-2023-0902 | 2027年8月
我公司声明：截至开标之日，我公司未被列入失信被执行人名单。
四、技术方案
我公司已通过ISO27001信息安全管理体系认证（证书编号：ISMS-2025-0887），具备完善的信息安全管理能力。
"""


def test_extract_bid_facts_yi():
    facts = extract_bid_facts(YI_TEXT, documents=["投标函", "开标一览表"])
    assert facts.bid_price == D("6200000.00")
    assert facts.bid_price_upper_value == D("6200000")
    assert facts.price_items_total == D("6200000.00")
    assert facts.registered_capital == D("20000000")
    assert len(facts.performance_amounts) == 2
    assert len(facts.certificates) == 2
    assert facts.cert_claims and "ISO27001" in facts.cert_claims[0]
    assert facts.has_guarantee is True
    assert facts.credit_statement is True


def test_extract_guarantee_missing():
    text = "（五）投标保证金缴纳凭证\n（本部分内容略）\n注册资本：人民币3,000万元"
    facts = extract_bid_facts(text)
    assert facts.has_guarantee is False
    assert facts.registered_capital == D("30000000")


def test_extract_guarantee_negative_context():
    """否定语境不得判为已缴纳（曾因裸子串"缴纳投标保证金"误判）。"""
    facts = extract_bid_facts("我公司未缴纳投标保证金，申请免缴。")
    assert facts.has_guarantee is False
    facts2 = extract_bid_facts("已通过银行转账缴纳投标保证金人民币5万元。")
    assert facts2.has_guarantee is True


def test_extract_price_colon_variant():
    """“投标总价：6,200,000.00”这一形态必须抽得到。"""
    facts = extract_bid_facts("投标总价：6,200,000.00")
    assert facts.bid_price == D("6200000.00")


def test_price_fallback_requires_context():
    """¥ 回退必须带语境：保证金金额不得被当成投标报价。"""
    text = "履约保证金：¥50,000.00\n售后服务承诺：¥0.00"
    facts = extract_bid_facts(text)
    assert facts.bid_price is None  # 无投标语境 → 宁可不抽，不能乱抽
    facts2 = extract_bid_facts("投标报价：¥7,000,000.00")
    assert facts2.bid_price == D("7000000.00")


# ---- 真实标书格式（浙江中烟项目实测）------------------------------------------
# 样例数据里的 "投标总价：金额" 同行写法在真实标书中很少见；金额通常落在
# 表头**下一行**，且小写前常夹货币符号。以下四种形态均取自真实开标一览表。


def test_price_real_standalone_bid_sheet():
    """独立开标一览表：表头行 + 大写/小写各占一行。"""
    text = "\n".join(
        [
            "开标一览表",
            "招标编号: C202501BA2001716013881",
            "投标总价（含税）(元) 税率 项目负责人 投标保证金 备注",
            "大写:壹佰柒拾玖万陆仟柒佰元 6% 高文强 10000元 无",
            "整",
            "小写：1796700",
        ]
    )
    facts = extract_bid_facts(text)
    assert facts.bid_price == D("1796700")


def test_price_real_lower_case_with_currency_symbol():
    """小写与数字之间夹货币符号（"小写：￥1,526,400.00"）必须仍能抽到。"""
    text = "\n".join(
        [
            "投标总价（含税） 税率 投标保证金",
            "小写：￥1,526,400.00 元",
            "人民币",
            "1 6% 杨祥海 无",
            "壹万元整",
            "大写：壹佰伍拾贰万陆仟肆佰元整",
        ]
    )
    facts = extract_bid_facts(text)
    assert facts.bid_price == D("1526400.00")


def test_price_real_upper_case_row_without_label():
    """正文表格：表头后一行是裸大写金额，没有"大写"字样。"""
    text = "\n".join(
        [
            "3-1 开标一览表",
            "序号 投标总价（含税） 税率 项目负责人 投标保证金 备注",
            "1 壹佰柒拾肆万玖仟元整 6% 郑得龙 壹万元 /",
        ]
    )
    facts = extract_bid_facts(text)
    assert facts.bid_price == D("1749000")


def test_price_real_incl_vat_row():
    """分项报价表的含税合计行。"""
    text = "\n".join(
        [
            "投标分项报价表",
            "投标价（不含增值税,人民币元） 1650000",
            "增值税税率 6 %",
            "投标价（含增值税,人民币元） 1749000.00",
        ]
    )
    facts = extract_bid_facts(text)
    assert facts.bid_price == D("1749000.00")


def test_price_real_deposit_not_taken_as_bid_price():
    """大写回退不得把投标保证金当成投标报价。

    真实表格里保证金同样写作大写（"人民币壹万元整"），若只按"首个大写金额"取，
    会把 10000 当成报价——直接污染价格分这个最敏感的确定性输入。
    """
    text = "\n".join(
        [
            "投标总价（含税） 税率 项目负责人 投标保证金 备注",
            "大写:壹佰伍拾贰万陆仟肆佰元 6% 杨祥海 人民币壹万元整 无",
            "整",
        ]
    )
    facts = extract_bid_facts(text)
    assert facts.bid_price == D("1526400")


def test_extract_performance_wan_unit():
    """表头声明"万元"时金额要 ×10000，否则 10000 倍低估。"""
    text = "\n".join(
        [
            "类似项目业绩一览表",
            "序号 项目名称 合同金额（万元） 签订时间",
            "1 某政务云项目 420 2024年",
            "2 某数据平台项目 380 2023年",
        ]
    )
    facts = extract_bid_facts(text)
    assert facts.performance_amounts == [D("4200000"), D("3800000")]


def test_extract_performance_row_number_variants():
    """行首编号 1. / 1、 / （1） 都要能识别。"""
    base = [
        "序号 项目名称 合同金额（元） 签订时间",
        None,  # 占位，逐个替换
    ]
    for variant, expect in (("1. 某A项目 4,000,000.00 2024年", 1), ("1、某A项目 4,000,000.00 2024年", 1)):
        text = base[0] + "\n" + variant
        facts = extract_bid_facts(text)
        assert len(facts.performance_amounts) == expect, variant


def test_extract_placeholder():
    text = "技术方案：本项目为XX项目提供云平台建设服务。"
    facts = extract_bid_facts(text)
    assert facts.placeholders and "XX项目" in facts.placeholders[0]


# ---------------------------------------------------------------------------
# 校验器
# ---------------------------------------------------------------------------
def _valid_snapshot() -> dict:
    return {
        "package": {"project_id": "P", "version": 1, "status": "draft", "total_max": 100},
        "criteria": [
            {"code": "1", "category": "price", "name": "价格分", "max_score": "40",
             "score_type": "formula", "is_veto": False, "source_quote": "价格分=40×（基准价/评标价）",
             "formula": {"formula_type": "benchmark_ratio", "benchmark_method": "average",
                          "k": "0.95"}},
            {"code": "2", "category": "technical", "name": "技术方案", "max_score": "20",
             "score_type": "subjective", "is_veto": False, "source_quote": "技术方案（20分）",
             "levels": [
                 {"label": "优", "min": "17", "max": "20", "criteria": "优判据"},
                 {"label": "良", "min": "12", "max": "16", "criteria": "良判据"},
                 {"label": "一般", "min": "6", "max": "11", "criteria": "一般判据"},
                 {"label": "差", "min": "0", "max": "5", "criteria": "差判据"},
             ]},
            {"code": "3", "category": "technical", "name": "实施方案", "max_score": "15",
             "score_type": "subjective", "is_veto": False, "source_quote": "实施方案（15分）",
             "levels": [
                 {"label": "优", "min": "13", "max": "15", "criteria": "优"},
                 {"label": "良", "min": "9", "max": "12", "criteria": "良"},
                 {"label": "一般", "min": "4", "max": "8", "criteria": "一般"},
                 {"label": "差", "min": "0", "max": "3", "criteria": "差"},
             ]},
            {"code": "4", "category": "technical", "name": "售后", "max_score": "10",
             "score_type": "subjective", "is_veto": False, "source_quote": "售后（10分）",
             "levels": [
                 {"label": "优", "min": "9", "max": "10", "criteria": "优"},
                 {"label": "良", "min": "6", "max": "8", "criteria": "良"},
                 {"label": "一般", "min": "3", "max": "5", "criteria": "一般"},
                 {"label": "差", "min": "0", "max": "2", "criteria": "差"},
             ]},
            {"code": "5", "category": "commercial", "name": "业绩", "max_score": "9",
             "score_type": "count", "is_veto": False, "source_quote": "每个业绩得3分"},
            {"code": "6", "category": "commercial", "name": "认证", "max_score": "6",
             "score_type": "count", "is_veto": False, "source_quote": "每项认证得2分"},
            {"code": "7", "category": "qualification", "name": "注册资本", "max_score": None,
             "score_type": "veto", "is_veto": True, "veto_consequence": "disqualify",
             "source_quote": "注册资本不低于1000万元"},
        ],
    }


def test_validators_clean_snapshot():
    findings = run_validators(_valid_snapshot(), node_exists=lambda _: True)
    errors = [f for f in findings if f.severity == "error"]
    assert errors == [], [e.message for e in errors]


def test_validator_level_gap():
    snap = _valid_snapshot()
    snap["criteria"][1]["levels"][2]["min"] = "7"  # 5→7 之间空洞
    findings = run_validators(snap)
    assert any(f.code == "LEVEL_GAP" for f in findings)


def test_validator_level_overlap_and_top():
    snap = _valid_snapshot()
    # 内部相交：良 12-18 与 优 17-20 在 17-18 上真重叠 → error
    snap["criteria"][1]["levels"][1]["max"] = "18"
    findings = run_validators(snap)
    assert any(f.code == "LEVEL_OVERLAP" and f.severity == "error" for f in findings)


def test_validator_level_shared_boundary_warn_only():
    """真实标书写法「得0-2分/得2-3分」：端点共享只是 warn，不再阻断冻结。

    取自浙江中烟招标文件第 97 页的实际档位形状（故障案例）。
    """
    snap = {
        "package": {"project_id": "P", "version": 1, "status": "draft"},
        "criteria": [
            {"code": "1", "category": "technical", "name": "故障响应流程及应急预案",
             "max_score": "4", "score_type": "subjective", "is_veto": False,
             "source_quote": "故障响应流程及应急预案（4分）",
             "levels": [
                 {"label": "得3-4分", "min": "3", "max": "4", "criteria": "故障响应及时"},
                 {"label": "得2-3分", "min": "2", "max": "3", "criteria": "故障响应较及时"},
                 {"label": "得0-2分", "min": "0", "max": "2", "criteria": "故障响应不够及时"},
             ]},
        ],
    }
    findings = run_validators(snap, total_max=4)
    shared = [f for f in findings if f.code == "LEVEL_OVERLAP"]
    assert len(shared) == 2 and all(f.severity == "warn" for f in shared)
    errors = [f for f in findings if f.severity == "error"]
    assert errors == [], [e.message for e in errors]


def test_validator_levels_missing_downgrade():
    """主观项无档位：有满分 → warn（按 0-满分综合评定）；无满分 → error。"""
    snap = _valid_snapshot()
    snap["criteria"][2]["levels"] = []  # 有 max_score=15 → warn
    findings = run_validators(snap)
    missing = [f for f in findings if f.code == "LEVELS_MISSING"]
    assert len(missing) == 1 and missing[0].severity == "warn"

    snap["criteria"][2]["max_score"] = None  # 无满分 → error
    findings = run_validators(snap)
    missing = [f for f in findings if f.code == "LEVELS_MISSING"]
    assert len(missing) == 1 and missing[0].severity == "error"


def test_validator_veto_scored():
    snap = _valid_snapshot()
    snap["criteria"][6]["max_score"] = "5"  # 否决项给分 → 法规红线
    findings = run_validators(snap)
    assert any(f.code == "VETO_SCORED" for f in findings)


def test_validator_total_mismatch():
    snap = _valid_snapshot()
    snap["criteria"][1]["max_score"] = "25"  # 总分变 105
    snap["criteria"][1]["levels"][0]["max"] = "25"
    findings = run_validators(snap)
    assert any(f.code in ("TOTAL_SCORE", "LEVEL_TOP_MISMATCH") for f in findings)


def test_validator_placeholder():
    snap = _valid_snapshot()
    snap["criteria"][2]["description"] = "本项目为XX项目提供实施服务"
    findings = run_validators(snap)
    assert any(f.code == "PLACEHOLDER_LEFT" for f in findings)


def test_validator_quote_required():
    snap = _valid_snapshot()
    snap["criteria"][4]["source_quote"] = None
    findings = run_validators(snap)
    assert any(f.code == "QUOTE_REQUIRED" for f in findings)


def test_validator_level_bottom_mismatch():
    """最低档下界必须为 0（区间要覆盖 [0, max]）。"""
    snap = _valid_snapshot()
    snap["criteria"][1]["levels"][3]["min"] = "2"  # 差 2-5，0-1 分落空
    findings = run_validators(snap)
    assert any(f.code == "LEVEL_BOTTOM_MISMATCH" for f in findings)


def test_validator_price_params_incomplete():
    snap = _valid_snapshot()
    snap["criteria"][0]["formula"]["k"] = None
    findings = run_validators(snap)
    assert any(f.code == "PRICE_PARAMS_INCOMPLETE" for f in findings)


# ---------------------------------------------------------------------------
# document_present 词组匹配（「机器看不见」≠「不符合」，2026-10-14）
# ---------------------------------------------------------------------------
def _facts(text: str):
    from app.engine.facts import extract_bid_facts

    return extract_bid_facts(text)


def test_document_present_phrase_split_hit():
    """名称按标点拆词组，任一命中即 pass（招标文件该句是「或」列举）。"""
    from app.engine.objective import run_check

    facts = _facts("我公司具有营业执照及相关资质，现附复印件。")
    r = run_check(
        "document_present",
        {"document": "营业执照、法人证书或其他组织登记证明文件的复印件"},
        facts,
    )
    assert r.status == "pass" and "营业执照" in r.message


def test_document_present_normalized_hit():
    """OCR 软换行/空格打断子串时，归一化后仍应命中。"""
    from app.engine.objective import run_check

    facts = _facts("详见附件：营 业 执 照 复 印 件。")
    r = run_check("document_present", {"document": "营业执照复印件"}, facts)
    assert r.status == "pass"


def test_document_present_not_found_is_unknown_not_fail():
    """全部词组未命中 → unknown + need_human（机器看不见 ≠ 不存在），不再直接 fail。"""
    from app.engine.objective import run_check

    facts = _facts("本投标文件不含任何证照信息。")
    r = run_check("document_present", {"document": "投标保证金缴纳凭证"}, facts)
    assert r.status == "unknown" and r.need_human


def test_document_present_guarantee_regex_miss_phrase_fallback():
    """has_guarantee 正则失效（词序不同）时，词组兜底（归一化）仍能 pass。"""
    from app.engine.objective import run_check

    facts = _facts("投标保证金 缴纳凭证复印件见附件。")
    assert not facts.has_guarantee  # 正则要求「缴纳投标保证金」连续出现，此处词序不同
    r = run_check("document_present", {"document": "投标保证金缴纳凭证"}, facts)
    assert r.status == "pass"
