"""招标文件的结构化真源（内容与答案同源）。

`build_blocks()` 渲染为中文 PDF；`EXPECTED_RULES` 是规则编译器应抽出的结构化真值，
供离线回归断言（Mock 编译器逐字段对照）。
"""
from __future__ import annotations

PROJECT = {
    "name": "某市智慧政务云平台建设项目",
    "code": "ZJZB-2026-0117",
    "purchaser": "某市大数据发展管理局",
    "agency": "浙嘉招标代理有限公司",
    "max_price_yuan": 8_000_000,
    "max_price_text": "人民币800万元（¥8,000,000.00）",
}

# ---------------------------------------------------------------------------
# 资格要求（veto）与符合性条款（veto）
# ---------------------------------------------------------------------------
QUALIFICATION = [
    {
        "code": "2.1.1",
        "name": "独立法人及注册资本",
        "text": "投标人须具有独立法人资格，注册资本不低于人民币1000万元。",
        "check_type": "value_in_range",
        "params": {"field": "registered_capital", "min": 10_000_000, "unit": "元"},
        "on_fail": "recommend_reject",
    },
    {
        "code": "2.1.2",
        "name": "信息系统集成资质",
        "text": "投标人须具有信息系统集成及服务资质二级及以上证书。",
        "check_type": "document_present",
        "params": {"document": "信息系统集成及服务资质证书", "min_level": "二级"},
        "on_fail": "recommend_reject",
    },
    {
        "code": "2.1.3",
        "name": "类似项目业绩",
        "text": "投标人近三年（2023年1月1日至今）须具有至少2个合同金额不低于300万元的类似项目业绩。",
        "check_type": "count_at_least",
        "params": {"field": "performance", "min_count": 2, "min_amount_yuan": 3_000_000},
        "on_fail": "recommend_reject",
    },
    {
        "code": "2.1.4",
        "name": "非失信被执行人",
        "text": "投标人未被列入失信被执行人名单（以开标当日“信用中国”网站查询结果为准）。",
        "check_type": "contains_phrase",
        "params": {"field": "credit_statement", "phrase": "非失信被执行人"},
        "on_fail": "recommend_reject",
    },
]

COMPLIANCE = [
    {
        "code": "2.2.1",
        "name": "投标函及授权委托书",
        "text": "投标文件须包含投标函及法定代表人授权委托书。",
        "check_type": "document_present",
        "params": {"document": "投标函及法定代表人授权委托书"},
        "on_fail": "recommend_reject",
    },
    {
        "code": "2.2.2",
        "name": "投标保证金",
        "text": "投标人须提交投标保证金人民币5万元，保证金缴纳凭证须装订于投标文件资格证明部分。",
        "check_type": "document_present",
        "params": {"document": "投标保证金缴纳凭证", "amount_yuan": 50_000},
        "on_fail": "recommend_reject",
    },
    {
        "code": "2.2.3",
        "name": "报价唯一且不超限价",
        "text": "投标报价须唯一且不超过最高限价800万元，报价以《开标一览表》为准。",
        "check_type": "math_relation",
        "params": {"field": "bid_price", "max_yuan": 8_000_000, "unique": True},
        "on_fail": "recommend_reject",
    },
]

# ---------------------------------------------------------------------------
# 价格分（40 分）——公式在正文里（验证正文抽取）
# ---------------------------------------------------------------------------
PRICE = {
    "code": "3.3.1",
    "name": "价格分",
    "max_score": 40,
    "formula_text": (
        "评标基准价的计算：以所有通过初步审查的投标人的有效投标报价的算术平均值乘以0.95"
        "作为评标基准价。价格分计算：价格分=40×（评标基准价÷评标价），计算结果保留两位小数，"
        "价格分最高不超过40分。"
    ),
    "formula_type": "benchmark_ratio",
    "benchmark_method": "average",
    "k": "0.95",
    "upper_bound": 40,
    "rounding": 2,
    "exclude_invalid": True,
}

# ---------------------------------------------------------------------------
# 技术分（45 分）——档位表在表格里（验证表格抽取）
# ---------------------------------------------------------------------------
TECH_CRITERIA = [
    {
        "code": "3.3.2.1",
        "name": "技术方案",
        "max_score": 20,
        "levels": [
            ("优", 17, 20, "技术方案完整、先进，对项目需求理解深入，系统架构设计合理，并发处理能力、"
                          "数据加密、容灾备份等关键技术指标均有明确且可验证的量化指标，提供详细的架构图与部署方案。"),
            ("良", 12, 16, "技术方案较完整，对项目需求理解较深入，系统架构设计较合理，主要技术指标有量化说明。"),
            ("一般", 6, 11, "技术方案基本完整，对项目需求理解一般，系统架构设计基本合理，技术指标描述较笼统。"),
            ("差", 0, 5, "技术方案不完整或存在明显缺陷，未体现对项目需求的理解。"),
        ],
    },
    {
        "code": "3.3.2.2",
        "name": "项目实施方案",
        "max_score": 15,
        "levels": [
            ("优", 13, 15, "实施计划详尽，里程碑与交付物明确到具体日期，人员配置合理（项目经理具备高级职称及"
                          "PMP证书），风险应对措施具体可操作。"),
            ("良", 9, 12, "实施计划较详尽，里程碑明确，人员配置较合理，有基本风险应对措施。"),
            ("一般", 4, 8, "实施计划基本完整，里程碑较粗略，人员配置未明确。"),
            ("差", 0, 3, "实施计划缺失或明显不可行。"),
        ],
    },
    {
        "code": "3.3.2.3",
        "name": "售后服务方案",
        "max_score": 10,
        "levels": [
            ("优", 9, 10, "售后服务体系完善，承诺7×24小时响应，故障响应时间不超过30分钟，现场到场时间不超过"
                         "4小时，提供本地化服务团队及备件库。"),
            ("良", 6, 8, "售后服务体系较完善，承诺5×8小时响应，故障响应时间不超过2小时，有服务团队配置。"),
            ("一般", 3, 5, "售后服务承诺基本满足招标要求，无量化服务指标。"),
            ("差", 0, 2, "售后服务方案缺失或不能满足基本要求。"),
        ],
    },
]

# ---------------------------------------------------------------------------
# 商务分（15 分）——计数类（count）
# ---------------------------------------------------------------------------
COMMERCIAL = [
    {
        "code": "3.3.3.1",
        "name": "类似业绩",
        "max_score": 9,
        "rule_text": "投标人每提供1个近三年合同金额不低于300万元的类似项目业绩得3分，最多得9分，须提供合同复印件。",
        "check_type": "count_at_least",
        "params": {"field": "performance", "min_amount_yuan": 3_000_000, "per_item": 3, "max_items": 3},
    },
    {
        "code": "3.3.3.2",
        "name": "体系认证",
        "max_score": 6,
        "rule_text": "投标人具有ISO9001质量管理体系认证、ISO27001信息安全管理体系认证、CMMI3级及以上认证的，"
                     "每提供一项得2分，最多得6分，须提供有效期内的证书复印件。",
        "check_type": "count_at_least",
        "params": {"field": "certificates", "per_item": 2, "max_items": 3},
    },
]

# 编译器应抽出的结构化真值（expected_rules.json 的内容）
EXPECTED_RULES = {
    "project_code": PROJECT["code"],
    "criteria": [
        *[
            {"code": q["code"], "category": "qualification", "name": q["name"],
             "max_score": None, "score_type": "veto", "is_veto": True,
             "veto_consequence": "disqualify", "check_type": q["check_type"]}
            for q in QUALIFICATION
        ],
        *[
            {"code": c["code"], "category": "compliance", "name": c["name"],
             "max_score": None, "score_type": "veto", "is_veto": True,
             "veto_consequence": "disqualify", "check_type": c["check_type"]}
            for c in COMPLIANCE
        ],
        {"code": PRICE["code"], "category": "price", "name": PRICE["name"],
         "max_score": 40, "score_type": "formula", "is_veto": False,
         "formula_type": PRICE["formula_type"], "benchmark_method": PRICE["benchmark_method"],
         "k": PRICE["k"], "upper_bound": 40},
        *[
            {"code": t["code"], "category": "technical", "name": t["name"],
             "max_score": t["max_score"], "score_type": "subjective", "is_veto": False,
             "levels": [{"label": lv[0], "min": lv[1], "max": lv[2]} for lv in t["levels"]]}
            for t in TECH_CRITERIA
        ],
        *[
            {"code": c["code"], "category": "commercial", "name": c["name"],
             "max_score": c["max_score"], "score_type": "count", "is_veto": False}
            for c in COMMERCIAL
        ],
    ],
    "total_max": 100,
}


def build_blocks() -> list[dict]:
    """渲染为 PDF 块序列。"""
    b: list[dict] = []
    b += [
        {"kind": "title", "text": PROJECT["name"]},
        {"kind": "h1", "text": "招 标 文 件"},
        {"kind": "meta", "text": f"招标编号：{PROJECT['code']}"},
        {"kind": "meta", "text": f"招标人：{PROJECT['purchaser']}"},
        {"kind": "meta", "text": f"招标代理机构：{PROJECT['agency']}"},
        {"kind": "meta", "text": "二〇二六年九月"},
        {"kind": "pagebreak"},
    ]

    # 第一章
    b += [
        {"kind": "h1", "text": "第一章 招标公告"},
        {"kind": "h2", "text": "一、项目概况"},
        {"kind": "para", "text": f"项目名称：{PROJECT['name']}。"},
        {"kind": "para", "text": f"招标编号：{PROJECT['code']}。"},
        {"kind": "para", "text": "建设内容：政务云平台基础设施、统一身份认证、数据共享交换、"
                                 "运维管理平台及配套安全体系的设计、开发、部署与三年运维服务。"},
        {"kind": "para", "text": f"最高限价：{PROJECT['max_price_text']}，投标报价超过最高限价的，其投标将被否决。"},
        {"kind": "h2", "text": "二、招标文件的获取"},
        {"kind": "para", "text": "凡有意参加投标者，请于2026年9月28日至2026年10月9日（法定节假日除外），"
                                 "在招标代理机构处获取招标文件。"},
        {"kind": "h2", "text": "三、投标截止时间及开标时间"},
        {"kind": "para", "text": "投标截止时间与开标时间为2026年10月20日9时30分，地点为某市公共资源交易中心三楼开标室。"},
        {"kind": "pagebreak"},
    ]

    # 第二章
    b += [
        {"kind": "h1", "text": "第二章 投标人须知"},
        {"kind": "h2", "text": "一、投标人资格要求"},
    ]
    for q in QUALIFICATION:
        b.append({"kind": "para", "text": f"（{_cn(q['code'][-1])}）{q['text']}"})
    b += [
        {"kind": "h2", "text": "二、投标文件的编制"},
    ]
    for i, c in enumerate(COMPLIANCE, start=1):
        b.append({"kind": "para", "text": f"（{_cn(str(i))}）{c['text']}"})
    b += [
        {"kind": "h2", "text": "三、投标文件的递交"},
        {"kind": "para", "text": "投标文件应密封递交，正本一份、副本四份，电子版（U盘）一份。"},
        {"kind": "h2", "text": "四、投标报价"},
        {"kind": "para", "text": "投标报价应为完成本项目全部工作内容的总价，包含软件开发、硬件采购、"
                                 "实施部署、三年运维及税费等全部费用。"},
        {"kind": "pagebreak"},
    ]

    # 第三章 评标办法
    b += [
        {"kind": "h1", "text": "第三章 评标办法"},
        {"kind": "h2", "text": "一、评标方法"},
        {"kind": "para", "text": "本项目采用综合评分法，总分100分，其中价格分40分、技术分45分、商务分15分。"},
        {"kind": "h2", "text": "二、初步审查"},
        {"kind": "para", "text": "初步审查包括资格性审查与符合性审查。投标人任一项审查不合格的，其投标作否决处理。"},
        {"kind": "h2", "text": "三、详细评审"},
        {"kind": "para", "text": "（一）价格分（40分）"},
        {"kind": "para", "text": PRICE["formula_text"]},
        {"kind": "para", "text": "（二）技术分（45分）"},
        {"kind": "para", "text": "1. 技术方案（20分）"},
    ]
    b.append(_levels_table(TECH_CRITERIA[0]))
    b.append({"kind": "para", "text": "2. 项目实施方案（15分）"})
    b.append(_levels_table(TECH_CRITERIA[1]))
    b.append({"kind": "para", "text": "3. 售后服务方案（10分）"})
    b.append(_levels_table(TECH_CRITERIA[2]))
    b += [
        {"kind": "para", "text": "（三）商务分（15分）"},
        {"kind": "para", "text": f"1. 类似业绩（9分）{COMMERCIAL[0]['rule_text']}"},
        {"kind": "para", "text": f"2. 体系认证（6分）{COMMERCIAL[1]['rule_text']}"},
        {"kind": "pagebreak"},
    ]

    # 第四章
    b += [
        {"kind": "h1", "text": "第四章 合同条款"},
        {"kind": "h2", "text": "一、合同工期"},
        {"kind": "para", "text": "合同签订后90个日历日内完成系统上线，试运行30日后组织终验。"},
        {"kind": "h2", "text": "二、付款方式"},
        {"kind": "para", "text": "合同签订后支付30%，系统上线试运行合格后支付40%，终验合格后支付25%，"
                                 "质保金5%于质保期满后支付。"},
        {"kind": "h2", "text": "三、违约责任"},
        {"kind": "para", "text": "逾期交付的，每逾期一日按合同总额的0.5‰支付违约金，累计不超过合同总额的5%。"},
        {"kind": "pagebreak"},
    ]

    # 第五章
    b += [
        {"kind": "h1", "text": "第五章 投标文件格式"},
        {"kind": "h2", "text": "一、投标函"},
        {"kind": "para", "text": "投标函应载明投标报价（小写与大写）、项目名称、招标编号，"
                                 "并由法定代表人或授权代表签字并加盖单位公章。"},
        {"kind": "h2", "text": "二、开标一览表"},
        {"kind": "para", "text": "开标一览表应载明投标总价及分项报价明细。"},
        {"kind": "h2", "text": "三、资格证明文件"},
        {"kind": "para", "text": "包括营业执照、资质证书、业绩合同复印件、信用声明、保证金凭证等。"},
    ]
    return b


def _levels_table(crit: dict) -> dict:
    rows = [["档次", "分值区间", "评分标准"]]
    for label, lo, hi, text in crit["levels"]:
        rows.append([label, f"{lo}-{hi}分", text])
    return {"kind": "table", "rows": rows, "col_widths": [1.2, 1.6, 8.2]}


def _cn(digit: str) -> str:
    return {"1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六"}.get(digit, digit)
