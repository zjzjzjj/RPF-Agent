"""投标文件的结构化真源（内容与答案同源）。

5 家投标人按"缺陷注入用例集"设计：
- 甲 华信科技：正例（技术详实、量化指标齐备）
- 乙 云速信息：低价激进 + 引用造假（声称 ISO27001 但证书复印件缺失、技术方案出现他公司名）
- 丙 恒通软件：缺投标保证金凭证 → 符合性失败（建议否决）
- 丁 智联云图：大小写金额不一致 + 分项合计≠总价 + "XX项目"占位符残留
- 戊 方舟数字：常规中间值（与甲同价，供价格分与排名对照）
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BidderSpec:
    key: str
    name: str
    unified_code: str
    registered_capital_yuan: int
    price_yuan: int
    price_upper: str  # 大写金额（丁故意写错）
    price_items: list[tuple[str, int]]
    qualification_cert: str | None  # None = 缺失
    performances: list[dict]  # [{name, amount_yuan, year}]
    credit_statement: bool
    has_bid_letter: bool = True
    has_guarantee: bool = True  # 丙 = False
    certificates: list[tuple[str, str, str]] = field(default_factory=list)  # (名称, 编号, 有效期)
    cert_claims: list[str] = field(default_factory=list)  # 正文中声称但可能无证书的认证
    tech_plan: str = ""
    implementation: str = ""
    after_sales: str = ""
    anomalies: list[str] = field(default_factory=list)


BIDDERS: list[BidderSpec] = [
    BidderSpec(
        key="jia",
        name="华信科技股份有限公司",
        unified_code="91330100MA2XXXXX01",
        registered_capital_yuan=50_000_000,
        price_yuan=7_600_000,
        price_upper="柒佰陆拾万元整",
        price_items=[
            ("政务云平台软件开发", 3_200_000),
            ("硬件设备采购", 2_400_000),
            ("实施部署与培训", 1_000_000),
            ("三年运维服务", 1_000_000),
        ],
        qualification_cert="信息系统集成及服务资质二级（证书编号：XT-2023-0421，有效期至2027年5月）",
        performances=[
            {"name": "某区政务云平台建设项目", "amount_yuan": 4_200_000, "year": 2024},
            {"name": "某局数据共享交换平台项目", "amount_yuan": 3_800_000, "year": 2024},
            {"name": "某委统一身份认证系统项目", "amount_yuan": 3_500_000, "year": 2023},
        ],
        credit_statement=True,
        certificates=[
            ("ISO9001质量管理体系认证", "QMS-2024-1188", "2027年3月"),
            ("ISO27001信息安全管理体系认证", "ISMS-2024-2260", "2027年6月"),
            ("CMMI3级认证", "CMMI-DEV-2024-0357", "2028年1月"),
        ],
        tech_plan=(
            "我公司深入理解本项目需求，提供完整、先进的技术方案。系统架构采用微服务与云原生设计，"
            "提供详细的架构图与部署方案。关键技术指标如下：平台并发处理能力不低于5000TPS；"
            "数据传输采用SM4国密算法加密，存储加密采用SM4与AES-256双算法；"
            "容灾备份采用两地三中心架构，RPO不超过15分钟，RTO不超过30分钟；"
            "统一身份认证支持不少于50万用户。上述指标均提供可验证的量化说明与测试方法。"
        ),
        implementation=(
            "实施计划详尽，里程碑与交付物明确到具体日期：2026年11月1日完成需求确认，"
            "2026年12月15日完成系统设计评审，2027年1月31日完成开发与内部测试，"
            "2027年2月28日完成部署上线。项目经理具备高级职称及PMP证书。"
            "针对人员流失、需求变更等风险制定具体可操作的应对措施。"
        ),
        after_sales=(
            "售后服务体系完善：提供7×24小时响应服务，故障响应时间不超过30分钟，"
            "现场到场时间不超过4小时；在本地设立服务团队及备件库，"
            "提供不少于3名驻场工程师及年度巡检服务。"
        ),
    ),
    BidderSpec(
        key="yi",
        name="云速信息技术有限公司",
        unified_code="91330100MA2XXXXX02",
        registered_capital_yuan=20_000_000,
        price_yuan=6_200_000,
        price_upper="陆佰贰拾万元整",
        price_items=[
            ("政务云平台软件开发", 2_600_000),
            ("硬件设备采购", 2_000_000),
            ("实施部署与培训", 800_000),
            ("三年运维服务", 800_000),
        ],
        qualification_cert="信息系统集成及服务资质二级（证书编号：XT-2022-0876）",
        performances=[
            {"name": "某县政务信息化平台项目", "amount_yuan": 3_500_000, "year": 2024},
            {"name": "某街道智慧社区平台项目", "amount_yuan": 3_200_000, "year": 2023},
        ],
        credit_statement=True,
        certificates=[
            ("ISO9001质量管理体系认证", "QMS-2023-0455", "2026年12月"),
            ("CMMI3级认证", "CMMI-DEV-2023-0902", "2027年8月"),
        ],
        cert_claims=["ISO27001信息安全管理体系认证（证书编号：ISMS-2025-0887）"],
        tech_plan=(
            "我公司采用先进的云平台技术，确保项目高质量交付。系统具有良好的性能和扩展性，"
            "充分运用大数据、人工智能等先进技术，采用业界领先的技术架构，"
            "能够满足招标人的各项需求。本技术方案参考了云端智联科技有限公司在同类项目中的成功经验。"
        ),
        implementation=(
            "实施计划包括需求分析、系统设计、开发测试、部署上线等阶段，"
            "计划在合同签订后三个月内完成主要建设工作，人员配置根据项目进展动态调整。"
        ),
        after_sales=(
            "我公司提供完善的售后服务，确保系统稳定运行。服务内容包括系统维护、"
            "故障处理、技术咨询等，服务团队随时响应招标人需求。"
        ),
        anomalies=["ISO27001 声称有证书但证书复印件缺失", "技术方案中出现他公司名称（云端智联科技有限公司）"],
    ),
    BidderSpec(
        key="bing",
        name="恒通软件科技有限公司",
        unified_code="91330100MA2XXXXX03",
        registered_capital_yuan=30_000_000,
        price_yuan=7_000_000,
        price_upper="柒佰万元整",
        price_items=[
            ("政务云平台软件开发", 3_000_000),
            ("硬件设备采购", 2_100_000),
            ("实施部署与培训", 950_000),
            ("三年运维服务", 950_000),
        ],
        qualification_cert="信息系统集成及服务资质二级（证书编号：XT-2021-0330）",
        performances=[
            {"name": "某市不动产登记信息平台项目", "amount_yuan": 4_800_000, "year": 2024},
            {"name": "某区智慧城市运行管理中心项目", "amount_yuan": 3_600_000, "year": 2023},
        ],
        credit_statement=True,
        has_guarantee=False,  # 缺陷：缺保证金凭证
        certificates=[("ISO9001质量管理体系认证", "QMS-2022-0771", "2026年11月")],
        tech_plan=(
            "技术方案较完整，对项目需求理解较深入。系统采用主流云平台架构，"
            "主要技术指标有量化说明：并发处理能力3000TPS，支持三级等保要求。"
        ),
        implementation=(
            "实施计划较详尽，里程碑明确：合同签订后30日完成需求确认，90日完成开发部署。"
            "项目组配置项目经理1名、开发人员8名、测试人员3名。"
        ),
        after_sales=(
            "售后服务体系较完善：提供5×8小时响应服务，故障响应时间不超过2小时，"
            "配置专职服务团队3人。"
        ),
        anomalies=["投标文件中缺少投标保证金缴纳凭证"],
    ),
    BidderSpec(
        key="ding",
        name="智联云图信息技术有限公司",
        unified_code="91330100MA2XXXXX04",
        registered_capital_yuan=15_000_000,
        price_yuan=7_500_000,
        price_upper="柒佰零伍万元整",  # 缺陷：与小写不一致（705万 vs 750万）
        price_items=[
            ("政务云平台软件开发", 3_100_000),
            ("硬件设备采购", 2_300_000),
            ("实施部署与培训", 1_050_000),
            ("三年运维服务", 1_000_000),  # 分项合计 7,450,000 ≠ 总价 7,500,000
        ],
        qualification_cert="信息系统集成及服务资质二级（证书编号：XT-2023-0655）",
        performances=[
            {"name": "某市政务服务网升级项目", "amount_yuan": 5_200_000, "year": 2024},
            {"name": "某区数据治理平台项目", "amount_yuan": 3_300_000, "year": 2024},
            {"name": "某局协同办公平台项目", "amount_yuan": 3_100_000, "year": 2023},
        ],
        credit_statement=True,
        certificates=[
            ("ISO9001质量管理体系认证", "QMS-2023-0980", "2027年2月"),
            ("ISO27001信息安全管理体系认证", "ISMS-2023-1450", "2027年4月"),
        ],
        tech_plan=(
            "技术方案基本完整，对项目需求理解一般。本项目为XX项目提供政务云平台建设服务，"
            "系统架构设计基本合理，采用成熟技术路线。"
        ),
        implementation=(
            "实施计划基本完整，分为启动、设计、开发、上线四个阶段，"
            "各阶段周期根据实际情况安排，里程碑较粗略。"
        ),
        after_sales=(
            "售后服务体系较完善：提供5×8小时响应服务，故障响应时间不超过2小时，"
            "并提供远程技术支持。"
        ),
        anomalies=["大写金额（柒佰零伍万元整）与小写金额（7,500,000）不一致",
                   "分项报价合计（7,450,000）与投标总价（7,500,000）不一致",
                   "技术方案含“XX项目”占位符残留"],
    ),
    BidderSpec(
        key="wu",
        name="方舟数字技术有限公司",
        unified_code="91330100MA2XXXXX05",
        registered_capital_yuan=40_000_000,
        price_yuan=7_600_000,
        price_upper="柒佰陆拾万元整",
        price_items=[
            ("政务云平台软件开发", 3_300_000),
            ("硬件设备采购", 2_300_000),
            ("实施部署与培训", 1_000_000),
            ("三年运维服务", 1_000_000),
        ],
        qualification_cert="信息系统集成及服务资质二级（证书编号：XT-2022-0512）",
        performances=[
            {"name": "某市公共数据开放平台项目", "amount_yuan": 4_000_000, "year": 2024},
            {"name": "某区智慧园区管理平台项目", "amount_yuan": 3_400_000, "year": 2023},
        ],
        credit_statement=True,
        certificates=[
            ("ISO9001质量管理体系认证", "QMS-2024-0301", "2027年5月"),
            ("ISO27001信息安全管理体系认证", "ISMS-2024-0788", "2027年7月"),
        ],
        tech_plan=(
            "技术方案较完整，对项目需求理解较深入。系统采用微服务架构，"
            "主要技术指标有量化说明：并发处理能力不低于4000TPS，支持国密算法加密。"
        ),
        implementation=(
            "实施计划较详尽，里程碑明确：需求确认、设计评审、开发测试、部署上线四个阶段"
            "均设定明确日期，项目组配置合理，有基本风险应对措施。"
        ),
        after_sales=(
            "售后服务体系较完善：提供5×8小时响应服务，故障响应时间不超过2小时，"
            "配置服务团队并提供远程支持。"
        ),
    ),
]

# 期望结论（expected_scores.json 的内容；价格分由 generate_sample_bids.py 计算后写入）
EXPECTED_QUALIFICATION = {
    "jia": {"2.1.1": "pass", "2.1.2": "pass", "2.1.3": "pass", "2.1.4": "pass",
            "2.2.1": "pass", "2.2.2": "pass", "2.2.3": "pass"},
    "yi": {"2.1.1": "pass", "2.1.2": "pass", "2.1.3": "pass", "2.1.4": "pass",
           "2.2.1": "pass", "2.2.2": "pass", "2.2.3": "pass"},
    "bing": {"2.1.1": "pass", "2.1.2": "pass", "2.1.3": "pass", "2.1.4": "pass",
             "2.2.1": "pass", "2.2.2": "recommend_reject", "2.2.3": "pass"},
    "ding": {"2.1.1": "pass", "2.1.2": "pass", "2.1.3": "pass", "2.1.4": "pass",
             "2.2.1": "pass", "2.2.2": "pass", "2.2.3": "conflict"},
    "wu": {"2.1.1": "pass", "2.1.2": "pass", "2.1.3": "pass", "2.1.4": "pass",
           "2.2.1": "pass", "2.2.2": "pass", "2.2.3": "pass"},
}

EXPECTED_COMMERCIAL = {  # (业绩分, 认证分)
    "jia": (9, 6),
    "yi": (6, 4),   # ISO27001 无证书复印件 → 不计分 + 冲突标记
    "bing": (6, 2),
    "ding": (9, 4),
    "wu": (6, 4),
}

EXPECTED_TECH_LEVEL = {  # 技术方案 / 项目实施方案 / 售后服务方案
    "jia": ("优", "优", "优"),
    "yi": ("差", "一般", "一般"),
    "bing": ("良", "良", "良"),
    "ding": ("一般", "一般", "良"),
    "wu": ("良", "良", "良"),
}


def build_blocks(spec: BidderSpec) -> list[dict]:
    b: list[dict] = [
        {"kind": "title", "text": f"{spec.name}"},
        {"kind": "h1", "text": "投 标 文 件"},
        {"kind": "meta", "text": "项目名称：某市智慧政务云平台建设项目"},
        {"kind": "meta", "text": "招标编号：ZJZB-2026-0117"},
        {"kind": "pagebreak"},
    ]

    # 投标函
    b += [
        {"kind": "h1", "text": "一、投标函"},
        {"kind": "para", "text": f"致：某市大数据发展管理局、浙嘉招标代理有限公司"},
        {"kind": "para", "text": f"我方（{spec.name}）已仔细研究招标文件全部内容，"
                                 f"愿以投标总价人民币{spec.price_upper}（¥{spec.price_yuan:,.2f}）"
                                 f"承担本项目的全部工作内容。"},
        {"kind": "para", "text": "我方承诺：本投标文件所提供的一切资料均真实、准确、有效，"
                                 "如有虚假，愿承担相应法律责任。"},
        {"kind": "para", "text": f"投标人（盖章）：{spec.name}"},
        {"kind": "para", "text": "法定代表人或授权代表（签字）：＿＿＿＿＿"},
        {"kind": "para", "text": "日期：2026年10月20日"},
    ]
    if not spec.has_bid_letter:
        b = b[:-6]  # 理论上不会用到；保留结构对称

    # 开标一览表
    total_items = sum(v for _, v in spec.price_items)
    rows = [["序号", "分项名称", "金额（元）"]]
    for i, (name, amount) in enumerate(spec.price_items, start=1):
        rows.append([str(i), name, f"{amount:,.2f}"])
    rows.append(["", "合计", f"{total_items:,.2f}"])
    rows.append(["", "投标总价", f"{spec.price_yuan:,.2f}"])
    b += [
        {"kind": "pagebreak"},
        {"kind": "h1", "text": "二、开标一览表"},
        {"kind": "table", "rows": rows, "col_widths": [1, 5, 3]},
    ]

    # 资格证明文件
    b += [
        {"kind": "pagebreak"},
        {"kind": "h1", "text": "三、资格证明文件"},
        {"kind": "h2", "text": "（一）营业执照信息"},
        {"kind": "para", "text": f"投标人名称：{spec.name}"},
        {"kind": "para", "text": f"统一社会信用代码：{spec.unified_code}"},
        {"kind": "para", "text": f"注册资本：人民币{spec.registered_capital_yuan / 10_000:,.0f}万元"},
        {"kind": "h2", "text": "（二）资质证书"},
    ]
    if spec.qualification_cert:
        b.append({"kind": "para", "text": spec.qualification_cert})
    else:
        b.append({"kind": "para", "text": "（本部分未提供资质证书复印件）"})

    b.append({"kind": "h2", "text": "（三）类似项目业绩"})
    perf_rows = [["序号", "项目名称", "合同金额（元）", "签订时间"]]
    for i, p in enumerate(spec.performances, start=1):
        perf_rows.append([str(i), p["name"], f"{p['amount_yuan']:,.2f}", f"{p['year']}年"])
    b.append({"kind": "table", "rows": perf_rows, "col_widths": [0.8, 5, 2.4, 1.4]})

    b.append({"kind": "h2", "text": "（四）信用声明"})
    if spec.credit_statement:
        b.append({"kind": "para", "text": "我公司声明：截至开标之日，我公司未被列入失信被执行人名单、"
                                          "重大税收违法失信主体名单及政府采购严重违法失信行为记录名单。"})
    else:
        b.append({"kind": "para", "text": "（本部分未提供信用声明）"})

    if spec.has_guarantee:
        b.append({"kind": "h2", "text": "（五）投标保证金缴纳凭证"})
        b.append({"kind": "para", "text": "我公司已通过银行转账方式缴纳投标保证金人民币5万元（¥50,000.00），"
                                          "转账凭证附后。"})
    else:
        b.append({"kind": "h2", "text": "（五）投标保证金缴纳凭证"})
        b.append({"kind": "para", "text": "（本部分内容略）"})

    b.append({"kind": "h2", "text": "（六）体系认证证书"})
    if spec.certificates:
        cert_rows = [["序号", "证书名称", "证书编号", "有效期至"]]
        for i, (cname, cno, expiry) in enumerate(spec.certificates, start=1):
            cert_rows.append([str(i), cname, cno, expiry])
        b.append({"kind": "table", "rows": cert_rows, "col_widths": [0.8, 4.6, 3, 1.8]})
        b.append({"kind": "para", "text": "上述证书复印件附后，均在有效期内。"})
    else:
        b.append({"kind": "para", "text": "（本部分未提供体系认证证书）"})

    # 技术部分
    b += [
        {"kind": "pagebreak"},
        {"kind": "h1", "text": "四、技术方案"},
        {"kind": "para", "text": spec.tech_plan},
    ]
    for claim in spec.cert_claims:
        b.append({"kind": "para", "text": f"我公司已通过{claim}，具备完善的信息安全管理能力。"})

    b += [
        {"kind": "pagebreak"},
        {"kind": "h1", "text": "五、项目实施方案"},
        {"kind": "para", "text": spec.implementation},
        {"kind": "pagebreak"},
        {"kind": "h1", "text": "六、售后服务方案"},
        {"kind": "para", "text": spec.after_sales},
    ]
    return b
