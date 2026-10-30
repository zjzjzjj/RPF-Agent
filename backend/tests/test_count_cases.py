# -*- coding: utf-8 -*-
"""业绩/人员案例核验回归（2026-10-21）。

真实故障：T1.2（团队成员经验 6 分）四家全 0（真实 6/0/0/6）、c12（负责人经验
4 分）龙即 0（真实约 4）。根因链：
① `_llm_verify_cases` 只把全文选段的 15k 字符送模型，几百页标书里案例页进不了
   上下文；② `_sanitize_case` 把"要求文本影子"判法套在短清单上，误杀合法引文；
③ 只要 cases 非空就清空 unresolved，0 分成为正式结论（无法与"未定分"区分）；
④ `person_scope` 用字符串精确比较，"leader（项目负责人）"整项归零。
"""
from __future__ import annotations

from decimal import Decimal

from app.engine.facts import BidFacts
from app.parsing.normalize import normalize_no_ws as _norm

REQ_TEXT = "【证明材料】须提供合同复印件、发票复印件和业主证明（或验收材料），不提供或提供不全的不得分"


# ---------------------------------------------------------------------------
# _sanitize_case
# ---------------------------------------------------------------------------
def test_sanitize_flags_requirement_shadow() -> None:
    from app.services.scoring_service import _sanitize_case

    case = {
        "name": "某项目", "contract": True, "invoice": True, "acceptance": True,
        "quote": "须提供合同复印件、发票复印件和业主证明",  # 照抄要求文本
    }
    out = _sanitize_case(case, REQ_TEXT)
    assert out["sanitized"] == "requirement_shadow"
    assert not any(out[k] for k in ("contract", "invoice", "acceptance"))


def test_sanitize_keeps_legit_quote_with_entities() -> None:
    from app.services.scoring_service import _sanitize_case

    case = {
        "name": "浙江中烟门户运维", "contract": True, "invoice": True, "acceptance": False,
        "quote": "2023年6月签订，合同金额2,208,000元，服务期三年",
    }
    out = _sanitize_case(case, REQ_TEXT)
    assert out["sanitized"] == "ok"
    assert out["contract"] is True and out["invoice"] is True


def test_sanitize_short_quote_with_number_is_ok() -> None:
    from app.services.scoring_service import _sanitize_case

    out = _sanitize_case({"name": "x", "quote": "2024年6月", "contract": True}, REQ_TEXT)
    assert out["sanitized"] == "ok"


# ---------------------------------------------------------------------------
# 候选页取样
# ---------------------------------------------------------------------------
def test_case_candidate_pages_finds_deep_pages() -> None:
    from app.services.scoring_service import _case_candidate_pages

    pages = [(i, f"无关内容第{i}页" * 20, "text") for i in range(1, 200)]
    pages[157] = (158, "合同名称：某运维项目 甲方：浙江中烟 合同金额：2,208,000元", "text")
    batches = _case_candidate_pages(pages)
    sent = {no for batch in batches for no, _t in batch}
    assert 158 in sent, "案例页必须进入送审上下文（旧实现只看 15k 字符选段）"
    assert 157 in sent and 159 in sent, "±1 页扩展窗"


def test_case_candidate_pages_batches_by_budget() -> None:
    from app.services.scoring_service import _CASE_BATCH_CHARS, _case_candidate_pages

    big = "合同 甲方 乙方 " + "内容" * 4000  # 约 8k 字符/页
    pages = [(i, big, "text") for i in range(1, 12)]
    batches = _case_candidate_pages(pages)
    assert len(batches) >= 2
    for batch in batches:
        assert sum(len(t) for _no, t in batch) <= _CASE_BATCH_CHARS * 1.2


# ---------------------------------------------------------------------------
# role 归一
# ---------------------------------------------------------------------------
def test_normalize_role_variants() -> None:
    from app.engine.objective import _normalize_role

    assert _normalize_role({"role": "Leader"}) == "leader"
    assert _normalize_role({"role": "项目负责人"}) == "leader"
    assert _normalize_role({"role": "leader（项目负责人）"}) == "leader"
    assert _normalize_role({"role": "member"}) == "member"
    assert _normalize_role({"role": "团队成员"}) == "member"
    assert _normalize_role({"role": ""}) is None


# ---------------------------------------------------------------------------
# unresolved 三态
# ---------------------------------------------------------------------------
def _count_crit(**kw) -> dict:
    base = {
        "name": "项目团队成员（除项目负责人外）有类似项目经验",
        "max_score": Decimal("6"),
        "required_artifacts": "合同复印件、发票复印件、验收材料",
        "params": {"field": "performance", "per_item": "1.5", "max_items": "4",
                   "person_scope": "member"},
    }
    base.update(kw)
    return base


def test_score_count_all_cases_sanitized_is_unresolved() -> None:
    """有案例但引文全被降级 → 不写正式 0 分，标未定分。"""
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    crit = _count_crit(verified_cases=[
        {"name": "A", "role": "member", "contract": False, "invoice": False,
         "acceptance": False, "quote": "", "sanitized": "requirement_shadow"},
        {"name": "B", "role": "member", "contract": False, "invoice": False,
         "acceptance": False, "quote": "", "sanitized": "quote_too_short"},
    ])
    result = score_count(crit, crit["params"], facts)
    assert result.unresolved == "cases_unverified"
    assert result.conflict is True


def test_score_count_legit_case_missing_material_scores_zero() -> None:
    """合法案例但材料缺失 → 0 分是真实评审结论（先数业绩 0 = 确无发票/验收）。"""
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    crit = _count_crit(verified_cases=[
        {"name": "A", "role": "member", "contract": True, "invoice": False,
         "acceptance": False, "quote": "2023年6月签订合同，金额80万元", "sanitized": "ok"},
    ])
    result = score_count(crit, crit["params"], facts)
    assert result.unresolved is None
    assert result.score == Decimal("0")
    assert result.conflict is True


def test_score_count_role_variants_not_zeroed() -> None:
    """role 写成"团队成员"也必须按 member 计入（旧实现精确比较 "member"）。"""
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    crit = _count_crit(verified_cases=[
        {"name": "A", "role": "团队成员", "work_proof": True, "social_security": True,
         "experience_proof": True, "person_name": "张三", "quote": "项目经理：张三 三年运维经验 社保已缴", "sanitized": "ok"},
    ])
    result = score_count(crit, crit["params"], facts)
    assert result.score == Decimal("1.5")


def test_score_count_no_cases_stays_unresolved() -> None:
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    crit = _count_crit()
    result = score_count(crit, crit["params"], facts)
    # 语义锚：无案例 → 必须 unresolved（不能把 0 当评审结论）。
    # 诊断码由 field_missing 精化为 no_facts：人员级 field 不缺（刻意清空走
    # verified_cases），缺的是案例——旧码把排查引向错误方向（2026-10-26）。
    assert result.unresolved == "no_facts"


def test_score_count_leader_type_gate_drops_non_ops_cases() -> None:
    """负责人经验 + 因素含"运维"：集成/维保/建设类案例确定性剔除（真实评审口径）。

    2026-10-23 run 47 实证：prompt 类型门槛被 LLM 无视，先数设备维保/网络
    建设类案例仍计 4 分（真实 0）；运维类案例必须保留（龙即香溢运维合同）。
    """
    from app.engine.objective import score_count

    def _leader_crit(cases):
        return {
            "name": "项目负责人经验",
            "description": "项目负责人具有五年及以上工作经验且作为项目负责人具有"
                           "类似系统运维项目的实施/服务经验的，每提供一个案例得2分",
            "max_score": Decimal("4"),
            "required_artifacts": "合同复印件、社保证明",
            "params": {"person_scope": "leader", "per_item": "2", "max_items": "2"},
            "verified_cases": cases,
        }

    facts = BidFacts(text="")
    # 先数形态：设备维保/网络建设/集成服务 → 全剔 → 0
    crit = _leader_crit([
        {"name": "建行北京分行华为设备维保项目", "role": "leader",
         "work_proof": True, "experience_proof": True, "social_security": True,
         "quote": "郑峪嘉 曾担任项目经理", "sanitized": "ok"},
        {"name": "再保险集团核心网络系统建设", "role": "leader",
         "work_proof": True, "experience_proof": True, "social_security": True,
         "quote": "郑峪嘉 曾担任项目经理", "sanitized": "ok"},
    ])
    res = score_count(crit, crit["params"], facts)
    assert res.score == Decimal("0")
    assert "类型门槛" in res.detail
    # 龙即形态：运维服务类 → 保留计满
    crit2 = _leader_crit([
        {"name": "浙江香溢控股有限公司全省协同办公系统运维服务维护合同", "role": "leader",
         "work_proof": True, "experience_proof": True, "social_security": True,
         "quote": "王耘 曾担任项目负责人", "sanitized": "ok"},
        {"name": "2021年浙江中烟企务系统运维服务项目", "role": "leader",
         "work_proof": True, "experience_proof": True, "social_security": True,
         "quote": "蒋立华 担任项目经理", "sanitized": "ok"},
    ])
    res2 = score_count(crit2, crit2["params"], facts)
    assert res2.score == Decimal("4")


def test_score_count_leader_type_gate_keeps_platform_named_cases() -> None:
    """类型门槛三态：平台名运维案例保留+疑问，明确非运维单一形态剔除。

    2026-10-29 run 64 实证：旧"名称须含运维|维护"把"二维码综合管理平台
    项目"这类实质运维案例当"集成/建设类"剔除（人工计满分）——
    机器无法确认类型 ≠ 确认不符。
    """
    from app.engine.objective import score_count

    crit = {
        "name": "项目负责人经验",
        "description": "项目负责人具有五年及以上工作经验且作为项目负责人具有"
                       "类似系统运维项目的实施/服务经验的，每提供一个案例得2分",
        "max_score": Decimal("4"),
        "required_artifacts": "合同复印件、社保证明",
        "params": {"person_scope": "leader", "per_item": "2", "max_items": "2"},
        "verified_cases": [
            # 平台名（无运维/维护字样、无明确非运维标记）→ 保留 + 疑问
            {"name": "浙江中烟《二维码综合管理平台》项目", "role": "leader",
             "work_proof": True, "experience_proof": True, "social_security": True,
             "quote": "姜维 曾担任项目负责人", "sanitized": "ok"},
            {"name": "河北中烟企业二维码管理系统项目", "role": "leader",
             "work_proof": True, "experience_proof": True, "social_security": True,
             "quote": "姜维 曾担任项目经理", "sanitized": "ok"},
            # 明确非运维单一形态 → 剔除
            {"name": "某市局网络系统建设项目", "role": "leader",
             "work_proof": True, "experience_proof": True, "social_security": True,
             "quote": "张三 曾担任项目经理", "sanitized": "ok"},
        ],
    }
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("4")  # 2 个保留案例 × 2 分
    assert "类型无法机器确认" in res.detail
    assert "剔除 1 个" in res.detail
    assert res.conflict is True


def test_overlay_experience_proof_rescue() -> None:
    """experience_proof 文档级救援：证明页含人员姓名 → False 升 True；
    找不到证明页 → 保持原判不强 False（机器没看见 ≠ 确认缺失）。"""
    from app.services.scoring_service import _overlay_experience_proof

    pages = [
        (1, "投标文件正文", "text"),
        (50, "项目负责人简历 工作经验证明 姜维 自2013年7月参加工作至今",
         "ocr"),
        (80, "普通方案页", "text"),
    ]
    cases = [
        {"person_name": "姜维", "experience_proof": False, "work_proof": True},
        {"person_name": "韩跃嵩", "experience_proof": None, "work_proof": True},
    ]
    _overlay_experience_proof(cases, pages)
    assert cases[0]["experience_proof"] is True   # 锚点救援
    assert cases[1]["experience_proof"] is None   # 无锚 → 保持原判

    # 全文档无证明页 → 全部保持原判（不强 False）
    cases2 = [{"person_name": "姜维", "experience_proof": False}]
    _overlay_experience_proof(cases2, [(1, "只有正文", "text")])
    assert cases2[0]["experience_proof"] is False


def test_score_count_member_scope_no_type_gate() -> None:
    """团队成员项因素无"运维"字样 → 不做类型过滤（诚道交警项目得计）。"""
    from app.engine.objective import score_count

    crit = _count_crit(verified_cases=[
        {"name": "2022年杭州交警业务系统使用服务项目", "role": "成员",
         "work_proof": True, "social_security": True, "experience_proof": True,
         "person_name": "李卓敏", "quote": "李卓敏 项目经理 2022年杭州交警项目",
         "sanitized": "ok"},
    ])
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("1.5")


# ---------------------------------------------------------------------------
# 确定性社保覆盖（2026-10-23 run 49：LLM 判社保方差 0↔6 来回摆）
# ---------------------------------------------------------------------------
def test_overlay_social_security_name_in_list() -> None:
    from app.services.scoring_service import _overlay_social_security

    pages = [
        (1, "社会保险缴费记录 序号 姓名 社会保障号\n1 王耘 330102", "ocr"),
    ]
    cases = [
        {"person_name": "王耘", "social_security": False},
        {"person_name": "宋宏", "social_security": True},
        {"person_name": "", "social_security": True},
    ]
    _overlay_social_security(cases, pages)
    assert cases[0]["social_security"] is True   # 名单内
    assert cases[1]["social_security"] is False  # 逐人名单不含 → False
    assert cases[2]["social_security"] is None   # 无法对应到人


def test_overlay_social_security_unit_wide_covers_all() -> None:
    """单位专用参保证明（截断名单）→ 全员覆盖 True（龙即/诚道/致成形态）。"""
    from app.services.scoring_service import _overlay_social_security

    pages = [
        (1, "社会保险参保证明（单位专用）单位名称：杭州龙即\n当前参保缴费总人数 156", "ocr"),
    ]
    cases = [{"person_name": "杨兰兰", "social_security": False}]
    _overlay_social_security(cases, pages)
    assert cases[0]["social_security"] is True


def test_overlay_social_security_annual_report_ignored() -> None:
    """企业年报公示的'社保信息（企业选择不公示）'不是缴纳证明。"""
    from app.services.scoring_service import _overlay_social_security

    pages = [
        (1, "2022年度报告 社保信息 城镇职工基本养老保险 1 人\n单位缴费基数 企业选择不公示", "ocr_img"),
    ]
    cases = [{"person_name": "郑峪嘉", "social_security": True}]
    _overlay_social_security(cases, pages)
    assert cases[0]["social_security"] is False  # 无真社保材料


def test_overlay_social_security_per_person_list() -> None:
    """逐人缴费名单（无单位级表述）：在名单 → True，不在 → False（先数形态）。"""
    from app.services.scoring_service import _overlay_social_security

    pages = [
        (1, "序号 姓名 社会保障号码 险种 缴费情况 本单位实际缴费月数\n"
            "1 郑峪嘉 130302196605151414 养老保险 2024年10月 2025年04月 7", "ocr"),
    ]
    cases = [
        {"person_name": "郑峪嘉", "social_security": False},
        {"person_name": "宋宏", "social_security": True},
    ]
    _overlay_social_security(cases, pages)
    assert cases[0]["social_security"] is True
    assert cases[1]["social_security"] is False


def test_overlay_work_proof_requires_evidence_page() -> None:
    """成员级 work_proof 确定性覆盖：姓名须出现在业绩证明材料页。

    2026-10-23 实证：先数成员只在简历表页出现（ev_pages=[] → 真实 0），
    龙即/诚道成员有合同/用户证明/经验证明页（真实 6）。
    """
    from app.services.scoring_service import _overlay_work_proof

    pages = [
        (1, "59 宋宏 男 硕士研 行业专家，高级运维工程师 建行分行基础设施资源整合工作", "text"),
        (2, "用户证明 兹证明吴奇钢参与2022年浙江中烟企务系统运维服务项目 业主单位盖章", "ocr"),
        (3, "项目经验证明详见5.1 我司团队人员中李卓敏深度参与了杭州交警项目", "ocr"),
    ]
    cases = [
        {"person_name": "宋宏", "work_proof": True},    # 仅简历行 → False
        {"person_name": "吴奇钢", "work_proof": False},  # 用户证明页 → True
        {"person_name": "李卓敏", "work_proof": False},  # 经验证明页 → True
        {"person_name": "", "work_proof": True},         # 无法对应 → None
    ]
    _overlay_work_proof(cases, pages)
    assert cases[0]["work_proof"] is False
    assert cases[1]["work_proof"] is True
    assert cases[2]["work_proof"] is True
    assert cases[3]["work_proof"] is None


# ---------------------------------------------------------------------------
# 材料清单解析（required_evidence 整句 → 清单）
# ---------------------------------------------------------------------------
def test_parse_required_artifacts_splits_clause() -> None:
    from app.engine.objective import parse_required_artifacts

    items = parse_required_artifacts(REQ_TEXT)
    assert "合同复印件" in items
    assert "发票复印件" in items
    assert any("验收" in x for x in items)
    assert not any("不得分" in x for x in items)


def test_parse_required_artifacts_without_marker() -> None:
    """required_evidence 没有【证明材料】标记时也要能解析出材料清单。"""
    from app.engine.objective import parse_required_artifacts

    items = parse_required_artifacts("须提供合同复印件、发票复印件以及认证平台查询结果截图")
    assert "合同复印件" in items and "发票复印件" in items
    assert any("截图" in x for x in items)


# ===========================================================================
# 2026-10-21 重做：echo 覆盖率 + 引文回标书定位（修"引文核验全毙 → 未定分"）
# ===========================================================================
# 真实故障（运行 #35 / O740）：投标人把评分规则整段抄进标书第 95 页（详细评审
# 索引表），真业绩在第 126 页。两页同送模型 → 引文与要求重叠 → `_sanitize_case`
# 旧的包含方向判据（`quote in req or req[:40] in quote`）全判"照抄" → 一家案例
# 全毙 → cases_unverified → 未定分。修法：①候选页剔除要求复述页；②判照抄改用
# n-gram 覆盖率；③引文回标书定位（新码 quote_not_in_bid）。

RULE_TEXT = (
    "投标人自2022年1月1日起至今具有信息系统运维案例，且合同金额达到80万元及以上，"
    "每提供一个得2分，最高得6分"
)
# 生产里 echo_n = normalize_no_ws(source_quote + required_evidence)
ECHO_TEXT = RULE_TEXT + REQ_TEXT
_REAL_QUOTE = "浙江中烟一体化综合门户平台建设项目合同金额96000000元2023年6月签订服务期三年"


def _sanitize(q: str, req: str = ECHO_TEXT, bid_norm=None, echo_n=None):
    from app.services.scoring_service import _sanitize_case

    return _sanitize_case(
        {"name": "x", "contract": True, "invoice": True, "acceptance": True, "quote": q},
        req, bid_norm=bid_norm, echo_n=echo_n,
    )


def test_sanitize_full_rule_echo_shadow_even_if_in_bid() -> None:
    """整条规则 ⊃ 引文（索引表复述）：即使引文真实存在于标书也必须判照抄。

    这是旧提案"引文在标书里就放行"的反作弊洞——投标人把规则抄进标书第 95 页，
    引文确实"在标书里"，但它是规则复述不是证据，放行会让伪造计数混进总分。
    """
    quote = RULE_TEXT + "【证明材料】须提供合同复印件、发票复印件和业主证明（或验收材料），不提供或提供不全的不得分"
    bid_norm = _norm("某页" + quote + "尾页")  # 引文确实在标书里
    out = _sanitize(quote, bid_norm=bid_norm)
    assert out["sanitized"] == "requirement_shadow"


def test_sanitize_pure_requirement_shadow_not_laundered_by_bid() -> None:
    """纯要求块 ⊂ 引文：bid 里有也不能"洗白"成证据。"""
    quote = "须提供合同复印件、发票复印件和业主证明"
    bid_norm = _norm("投标文件正文" + quote)
    out = _sanitize(quote, req=REQ_TEXT, bid_norm=bid_norm)
    assert out["sanitized"] == "requirement_shadow"


def test_sanitize_preamble_embed_with_real_evidence_is_ok() -> None:
    """要求头 + 真证据尾：旧 `req[:40] in quote` 会误杀；覆盖率低 → 放行。"""
    quote = REQ_TEXT + _REAL_QUOTE  # 头是要求，尾是真实业绩表内容
    bid_norm = _norm("某页" + quote + "尾页")
    out = _sanitize(quote, req=REQ_TEXT, bid_norm=bid_norm)
    assert out["sanitized"] == "ok"


def test_sanitize_genuine_quote_in_bid_is_ok() -> None:
    """真证据引文（无要求重叠）在标书里 → ok。"""
    bid_norm = _norm("前文" + _REAL_QUOTE + "后文")
    out = _sanitize(_REAL_QUOTE, bid_norm=bid_norm)
    assert out["sanitized"] == "ok"
    assert out.get("match_tier") == "exact"


def test_sanitize_fabricated_quote_not_in_bid() -> None:
    """引文回不到标书（精确/前缀梯都不中）→ quote_not_in_bid，三件套降 false。"""
    quote = "与华信集团签订合同金额1234万元2024年验收通过"
    out = _sanitize(quote, bid_norm=_norm("完全无关的标书内容"))
    assert out["sanitized"] == "quote_not_in_bid"
    assert not any(out[k] for k in ("contract", "invoice", "acceptance"))


def test_sanitize_paraphrase_head_matches_prefix_ladder() -> None:
    """OCR 表格串行改写：前 25 字逐字来自标书 → 前缀梯命中 → ok（记 match_tier）。"""
    quote = _REAL_QUOTE[:25] + "服务期三年额外补充说明"
    out = _sanitize(quote, bid_norm=_norm(_REAL_QUOTE))
    assert out["sanitized"] == "ok"
    assert out.get("match_tier", "").startswith("prefix") or out.get("match_tier") == "exact"


def test_sanitize_unlocatable_quote_falls_back_to_case_name() -> None:
    """引文被重排回不到标书，但案例名逐字在标书里 → 以案例名为锚放行。

    2026-10-23 实证（run 45 / 致成 c59）：OCR 表格串行重排后模型把引文写成
    通顺句，逐字定位失败；案例名"国网陕西渭南…运维服务项目"在 p143 逐字存在。
    旧逻辑整案例判 quote_not_in_bid → 归零，真实评审给 2 分。
    """
    from app.services.scoring_service import _sanitize_case

    name = "国网陕西渭南供电公司2024年信息系统运维服务项目"
    case = {
        "name": name,
        "person": "王兆君", "person_name": "王兆君",
        "work_proof": True, "experience_proof": True, "social_security": True,
        # 重排引文：内容真实但顺序与标书不同，回不到 bid_norm
        "quote": "担任国网陕西渭南供电公司2024年信息系统运维服务项目的一年期运维项目经理",
    }
    bid_norm = _norm("前文 项目名称：" + name + " 委托方：国网陕西尾页")
    out = _sanitize_case(case, ECHO_TEXT, bid_norm=bid_norm)
    assert out["sanitized"] == "ok"
    assert out.get("quote_source") == "case_name"
    assert out.get("match_tier") == "case_name"
    assert out["work_proof"] is True  # 材料布尔不得被降级


def test_sanitize_unlocatable_quote_fabricated_name_still_rejected() -> None:
    """重排引文 + 伪造案例名（回不到标书）→ 仍拒（不得靠编名字放行）。"""
    from app.services.scoring_service import _sanitize_case

    case = {
        "name": "华信集团某某系统集成项目",
        "work_proof": True,
        "quote": "担任华信集团某某系统集成项目的项目经理并通过验收",
    }
    out = _sanitize_case(case, ECHO_TEXT, bid_norm=_norm("完全无关的标书内容"))
    assert out["sanitized"] == "quote_not_in_bid"
    assert not out["work_proof"]


def test_sanitize_no_bid_norm_trusts_model() -> None:
    """未接 bid_norm 时：真证据引文 → ok（向后兼容，不误杀未接线的调用方）。"""
    out = _sanitize("2023年6月签订合同金额2208000元服务期三年", req=REQ_TEXT)
    assert out["sanitized"] == "ok"


def test_sanitize_empty_quote_uses_case_name_anchor() -> None:
    """模型给真实案例名+金额却 quote 留空（业绩表行）→ 以回得到标书的案例名作锚 → ok。

    真实回归（b13 / 运行 #35）：模型从业绩表列出 8 个真实案例名 + 金额，但把"实质
    材料"理解成合同正文、quote 全留空 → 旧逻辑全判 quote_too_short → 整项未定分。
    案例名回得到标书 = 案例真实存在，应放行计数（伪造名回不到标书，仍拒）。
    """
    from app.services.scoring_service import _sanitize_case

    name = "浙江中烟一体化综合门户平台建设项目"
    case = {"name": name, "amount_yuan": 96000000, "contract": True, "invoice": False,
            "acceptance": False, "quote": ""}
    out = _sanitize_case(case, ECHO_TEXT, bid_norm=_norm("某页" + name + "合同金额96,000,000元尾页"))
    assert out["sanitized"] == "ok"
    assert out.get("quote_source") == "case_name"
    assert out["quote"] == name


def test_sanitize_empty_quote_fabricated_name_rejected() -> None:
    """案例名回不到标书（伪造）→ 仍判 quote_too_short，不得靠编名字放行。"""
    from app.services.scoring_service import _sanitize_case

    case = {"name": "华信集团某某系统集成项目", "contract": True, "quote": ""}
    out = _sanitize_case(case, ECHO_TEXT, bid_norm=_norm("完全无关的标书内容"))
    assert out["sanitized"] == "quote_too_short"


# ---- 候选页剔除要求复述页 ---------------------------------------------------
def test_case_candidate_pages_drops_echo_page_keeps_table() -> None:
    from app.services.scoring_service import _case_candidate_pages

    echo_n = _norm(ECHO_TEXT)
    pages = [
        (1, "封面无关内容", "text"),
        (2, RULE_TEXT + REQ_TEXT, "text"),  # 详细评审索引表（要求复述页）
        (3, "业绩表 " + _REAL_QUOTE, "text"),  # 真业绩表页
    ]
    batches = _case_candidate_pages(pages, echo_n=echo_n)
    sent = {no for b in batches for no, _t in b}
    assert 2 not in sent, "要求复述页必须从送审上下文剔除"
    assert 3 in sent, "真业绩表页必须保留"


def test_is_echo_page_mixed_index_table_dropped() -> None:
    """混合索引页：内嵌 ≥60 字连续规则段（但整页多为其它内容，覆盖率低）→ 剔除。

    真实回归（b13 第 95 页「详细评审索引表」）：覆盖率仅 0.20（同页含 ISO 认证等
    其它评分项 + 封面噪声），但内嵌 68 字连续规则段。判活必须用"连续段"，不能只看
    覆盖率，否则漏掉最典型的污染源。
    """
    from app.services.scoring_service import _is_echo_page

    index_page = "详细评审索引表 序号 评审项目 评审内容 证明文件 商务部分见投标文件 " \
        + RULE_TEXT + REQ_TEXT \
        + " （1）投标人具有有效期内的ISO9001质量管理体系认证、ISO27001信息安全管理体系认证证书的，每项得1分"
    assert _is_echo_page(index_page, _norm(ECHO_TEXT))
    assert not _is_echo_page("业绩表 " + _REAL_QUOTE, _norm(ECHO_TEXT))


def test_case_candidate_pages_keeps_evidence_page_with_header() -> None:
    """真业绩页即便带一行【证明材料】头也不能被误删（两判据：高覆盖率 或 短页长连续段）。"""
    from app.services.scoring_service import _is_echo_page

    header = REQ_TEXT  # ≈40 字【证明材料】头
    table = "业绩表 " + _REAL_QUOTE * 20  # 长表体
    page = header + table
    assert not _is_echo_page(page, _norm(ECHO_TEXT)), "带材料头的真业绩页必须保留"


def test_llm_verify_cases_all_echo_returns_none() -> None:
    """整份文档候选页全是要求复述页 → 剔光 → 返回 None（调用方退回事实计数）。"""
    from app.services.scoring_service import _llm_verify_cases

    c = {"source_quote": RULE_TEXT, "name": "投标人业绩"}
    pages = [(1, RULE_TEXT + REQ_TEXT, "text"), (2, RULE_TEXT + REQ_TEXT + "合同", "text")]
    out = _llm_verify_cases(None, c, REQ_TEXT, ["合同复印件"], None, pages, "synthetic")
    assert out is None


# ---- score_count 三态 + reason 分解 -----------------------------------------
def _facts() -> BidFacts:
    return BidFacts(text="")


def test_score_count_mixed_reasons_scores_clean_case() -> None:
    """有一个 ok 案例 → 计数有依据出分；shadow/not_in_bid 被丢弃，不整体未定分。"""
    from app.engine.objective import score_count

    crit = _count_crit(verified_cases=[
        {"name": "A", "role": "member", "work_proof": True, "social_security": True,
         "experience_proof": True, "person_name": "张三", "quote": "项目团队成员：张三 三年运维经验 社保已缴", "sanitized": "ok"},
        {"name": "B", "role": "member", "work_proof": False, "social_security": False,
         "experience_proof": False, "quote": "须提供工作证明", "sanitized": "requirement_shadow"},
        {"name": "C", "role": "member", "work_proof": False, "social_security": False,
         "experience_proof": False, "quote": "长伪造引文内容在此", "sanitized": "quote_not_in_bid"},
    ])
    result = score_count(crit, crit["params"], _facts())
    assert result.unresolved is None
    assert result.score == Decimal("1.5")


def test_score_count_all_not_in_bid_unresolved_with_breakdown() -> None:
    """全部 quote_not_in_bid → cases_unverified，detail 带 reason 分解（不再写死"照抄"）。"""
    from app.engine.objective import score_count

    crit = _count_crit(verified_cases=[
        {"name": "A", "role": "member", "contract": False, "invoice": False,
         "acceptance": False, "quote": "长伪造引文甲", "sanitized": "quote_not_in_bid"},
        {"name": "B", "role": "member", "contract": False, "invoice": False,
         "acceptance": False, "quote": "长伪造引文乙", "sanitized": "quote_not_in_bid"},
    ])
    result = score_count(crit, crit["params"], _facts())
    assert result.unresolved == "cases_unverified"
    assert "quote_not_in_bid=2" in result.detail


def test_score_count_person_scope_overwrites_field_missing() -> None:
    """person_scope（无 field）：cases_unverified 必须覆盖 field_missing（顺序承重）。"""
    from app.engine.objective import score_count

    crit = _count_crit(
        params={"person_scope": "member", "per_item": "1.5", "max_items": "4"},  # 无 field
        verified_cases=[
            {"name": "A", "role": "member", "contract": False, "invoice": False,
             "acceptance": False, "quote": "长伪造引文", "sanitized": "quote_not_in_bid"},
        ],
    )
    result = score_count(crit, crit["params"], _facts())
    assert result.unresolved == "cases_unverified"


# ---- 三态材料计分（2026-10-21：null 不直接当 0） ---------------------------
def _perf_crit(**kw) -> dict:
    base = {
        "name": "投标人业绩",
        "max_score": Decimal("6"),
        "required_artifacts": "合同复印件、发票复印件、业主证明",
        "params": {"field": "performance", "per_item": "2", "max_items": "3"},
    }
    base.update(kw)
    return base


def test_score_count_null_with_contract_counts_and_flags() -> None:
    """发票不可读(None) + 有业主证明/验收 → 计分并标疑问（2026-10-27 语义变更）。

    训练集测试2 人工口径实证：有业主证明/验收的案例照计（发票 OCR 不可见≠缺失），
    旧「发票 null 硬闸门不计」把 5 家业绩几乎清零。确认缺失（False）仍硬不计。
    """
    from app.engine.objective import score_count

    crit = _perf_crit(verified_cases=[
        {"name": "A", "contract": True, "invoice": None, "acceptance": True,
         "case_source": "table", "sanitized": "ok"},
        {"name": "B", "contract": True, "invoice": True, "acceptance": True,
         "case_source": "table", "sanitized": "ok"},
    ])
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("4.00")  # A（发票不可读+有验收→计并标疑问）+ B
    assert res.unresolved is None
    assert res.conflict is True

    # 对照：发票**确认缺失**（False）仍硬不计
    crit2 = _perf_crit(verified_cases=[
        {"name": "A", "contract": True, "invoice": False, "acceptance": True,
         "case_source": "table", "sanitized": "ok"},
        {"name": "B", "contract": True, "invoice": True, "acceptance": True,
         "case_source": "table", "sanitized": "ok"},
    ])
    res2 = score_count(crit2, crit2["params"], BidFacts(text=""))
    assert res2.score == Decimal("2.00")


def test_score_count_null_without_contract_does_not_count() -> None:
    """合同都不可读 → 没有正向依据，不计（防通胀）。"""
    from app.engine.objective import score_count

    crit = _perf_crit(verified_cases=[
        {"name": "A", "contract": None, "invoice": None, "acceptance": None,
         "case_source": "table", "sanitized": "ok"},
    ])
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("0")
    assert res.unresolved is None  # 有 clean case（身份通过），不能整体未定分
    assert res.conflict is True


def test_score_count_false_material_does_not_count() -> None:
    """确认缺失（False）→ 该案例不计（规则"不全不得分"）。"""
    from app.engine.objective import score_count

    crit = _perf_crit(verified_cases=[
        {"name": "A", "contract": True, "invoice": False, "acceptance": True,
         "case_source": "table", "sanitized": "ok"},
        {"name": "B", "contract": True, "invoice": True, "acceptance": True,
         "case_source": "table", "sanitized": "ok"},
    ])
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("2.00")  # 只有 B


def test_score_count_table_cases_capped_at_max() -> None:
    from app.engine.objective import score_count

    cases = [
        {"name": f"C{i}", "contract": True, "invoice": True, "acceptance": True,
         "case_source": "table", "sanitized": "ok"}
        for i in range(6)
    ]
    crit = _perf_crit(verified_cases=cases)
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("6")  # 3 案封顶 × 2
    assert res.unresolved is None


# ---- 审计留痕：ai_raw_json 落库往返 -----------------------------------------
def test_upsert_score_persists_ai_raw_json(app_env) -> None:  # noqa: ANN001
    """count 路径把逐案例核验结果落 ai_raw_json，判"照抄/未在标书定位"后可复盘。"""
    from app.db.models import Bidder, Criterion, EvaluationRun, Project, RulePackage, ScoreRecord
    from app.db.session import session_scope
    from app.services.scoring_service import _upsert_score

    with session_scope() as s:
        s.add(Project(id="PRJ-AUDIT", name="审计留痕"))
        s.flush()  # 先落主表，外键子表才解析得到
        s.add(RulePackage(id=500, project_id="PRJ-AUDIT", version=1, status="frozen"))
        s.flush()
        s.add(Criterion(id=9000, package_id=500, code="T1.1", name="投标人业绩",
                        category="commercial", score_type="count", max_score=6))
        s.add(Bidder(id=900, project_id="PRJ-AUDIT", name="某公司"))
        s.add(EvaluationRun(id=9001, project_id="PRJ-AUDIT", package_id=500, package_version=1, status="running"))
        s.flush()

    audit = {"verified_cases": [{"name": "A", "sanitized": "requirement_shadow", "quote": "…"}],
             "meta": {"pages_dropped_echo": [95], "batches_sent": 2}}
    _upsert_score(9001, 900, {"package_id": 500, "code": "T1.1"},
                  machine_status="pass", ai_raw_json=audit)

    with session_scope() as s:
        row = s.query(ScoreRecord).filter_by(run_id=9001, bidder_id=900).one()
        assert row.ai_raw_json["verified_cases"][0]["sanitized"] == "requirement_shadow"
        assert row.ai_raw_json["meta"]["pages_dropped_echo"] == [95]


def test_sanitize_person_case_name_anchor_rescues_short_quote():
    """人员级案例（人名+证件）无逐字引文时，人名+证件词回标书即放行。

    2026-10-26 run 54 c60 实证：LLM 找到「顾爱军 电工证」但 quote 空 →
    quote_too_short 整案丢弃 → 全员 0 分。人证组合都在标书人员/证书表
    （"顾爱军 特种作业操作证（电工）"）应判真实存在。
    """
    from app.services.scoring_service import _sanitize_case

    case = {
        "name": "顾爱军 电工证", "person_name": "顾爱军",
        "quote": "", "work_proof": True, "social_security": True,
        "experience_proof": False, "contract": None, "invoice": None, "acceptance": None,
    }
    bid_norm = "拟派团队成员一览表顾爱军特种作业操作证电工高压电工证复印件"
    out = _sanitize_case(case, "须提供证书复印件", bid_norm=bid_norm, echo_n="须提供证书复印件")
    assert out["sanitized"] == "ok"
    assert out.get("quote_source") == "case_name"


def test_sanitize_person_case_fabricated_name_still_rejected():
    """人名回不到标书 → 仍判不可信（防模型编造持证人）。

    C1 语义（2026-10-29）：定位失败型（quote_too_short/quote_not_in_bid）降
    **None**（不可读≠确认缺失）；案例拒绝靠 sanitized != ok（进不了 clean_cases），
    不再依赖三件套 False。
    """
    from app.services.scoring_service import _sanitize_case

    case = {
        "name": "张三丰 电工证", "person_name": "张三丰",
        "quote": "", "work_proof": True,
    }
    bid_norm = "拟派团队成员一览表李四特种作业操作证"
    out = _sanitize_case(case, "须提供证书复印件", bid_norm=bid_norm, echo_n="须提供证书复印件")
    assert out["sanitized"] != "ok"
    assert out["work_proof"] is not True  # 不可信（不可读），不得保持 True


def test_sanitize_company_case_null_person_key_not_killed():
    """公司级业绩案例带 "person_name": null 空键 → 不得套人员校验（P0 回归）。

    2026-10-27 run 57 实证：LLM 输出的公司级案例常带 null 人员键，旧键存在判定
    误套人员校验 → 合同签署页 quote（甲方/乙方）触发 project_header_not_person
    → 整案材料强刷 False → 业绩全灭（人工业绩 23/30 vs 机 4/20）。
    """
    from app.services.scoring_service import _sanitize_case

    case = {
        "name": "国网陕西渭南供电局运维服务项目",
        "person_name": None, "work_proof": None,
        "quote": "甲方：国网陕西省电力有限公司 乙方：杭州致成电子科技有限公司 合同金额：258万元",
        "contract": True, "invoice": None, "acceptance": True,
    }
    out = _sanitize_case(case, "须提供合同复印件、发票复印件和业主证明", bid_norm=None,
                         person_scope="")
    assert out["sanitized"] != "project_header_not_person"
    # 材料不得被强刷（contract/acceptance 保持原判定）
    assert out.get("contract") is not False
    assert out.get("acceptance") is not False


def test_sanitize_person_case_contract_header_still_killed():
    """人员级案例（真 person_name 值）的合同头引文仍触发人员校验并降级人员键。"""
    from app.services.scoring_service import _sanitize_case

    case = {
        "name": "张三 项目经理业绩",
        "person_name": "张三", "work_proof": True,
        "quote": "甲方：某单位 乙方：某公司 合同金额：100万元",
    }
    out = _sanitize_case(case, "须提供人员经验证明", bid_norm=None, person_scope="leader")
    assert out["sanitized"] == "project_header_not_person"
    assert out.get("work_proof") is False


def test_sanitize_explicit_scope_wins_over_null_keys():
    """准则级 person_scope 显式传入时按人员级处理（即使案例内键全空）。"""
    from app.services.scoring_service import _sanitize_case

    case = {
        "name": "某项目", "person_name": None,
        "quote": "甲方：某单位 乙方：某公司 合同金额：100万元",
    }
    out = _sanitize_case(case, "人员经验证明", bid_norm=None, person_scope="member")
    assert out["sanitized"] == "project_header_not_person"
    # 公司级（scope 空）同样输入不触发
    out2 = _sanitize_case(dict(case), "人员经验证明", bid_norm=None, person_scope="")
    assert out2["sanitized"] != "project_header_not_person"


# ---- 零分理由 zero_reason（W1-C，2026-10-29）--------------------------------
# run 60 实证："5 个案例材料页不可读未计"仍记 0——分数维持规则硬口径（不改分
# 避免拟合），但 0 的性质必须可区分：机器看不见 ≠ 确认不符合。
def test_score_count_unreadable_drop_zero_reason() -> None:
    """点名材料页不可读(None)丢弃 → score=0 + zero_reason="unreadable"。

    与 confirmed_absent 分离：不可读可能是扫描件（机器看不见），
    不是"确认未提供"；落库侧强制 need_human + conflict_flagged。
    """
    from app.engine.objective import score_count

    crit = _perf_crit(verified_cases=[
        {"name": "A", "contract": None, "invoice": None, "acceptance": None,
         "case_source": "table", "sanitized": "ok"},
    ])
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("0")
    assert res.zero_reason == "unreadable"
    assert res.conflict is True
    assert "可能为扫描件" in res.detail


def test_score_count_confirmed_absent_zero_reason_no_doubt_forcing() -> None:
    """确认缺失（False）→ score=0 + zero_reason="confirmed_absent"（规则硬口径）。

    与 unreadable 的区别：confirmed_absent 是确定性评审结论（材料确认缺失），
    不需要 unreadable 那档强制转人工（conflict 仍标，但理由分类不同）。
    """
    from app.engine.objective import score_count

    crit = _perf_crit(verified_cases=[
        {"name": "A", "contract": True, "invoice": False, "acceptance": False,
         "case_source": "table", "sanitized": "ok"},
    ])
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("0")
    assert res.zero_reason == "confirmed_absent"


def test_score_count_no_facts_zero_reason() -> None:
    """没抽到事实 → score=0 + zero_reason="no_facts"（机器侧原因，非确认缺失）。"""
    from app.engine.objective import score_count

    crit = _count_crit()  # person_scope，无 verified_cases
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("0")
    assert res.unresolved == "no_facts"
    assert res.zero_reason == "no_facts"


def test_score_count_cases_unverified_zero_reason() -> None:
    """引文全毙（cases_unverified）：定位失败型 → unreadable 转人工（D2）。

    全部原因为 quote_not_in_bid/quote_too_short（OCR 串行噪声最常见）时，
    0 分归"机器不可见"而非 no_facts——机器没核上 ≠ 没有材料。
    """
    from app.engine.objective import score_count

    crit = _count_crit(verified_cases=[
        {"name": "A", "role": "member", "contract": None, "invoice": None,
         "acceptance": None, "quote": "长伪造引文", "sanitized": "quote_not_in_bid"},
    ])
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("0")
    assert res.unresolved == "cases_unverified"
    assert res.zero_reason == "unreadable"


def test_score_count_cases_unverified_cheat_type_stays_no_facts() -> None:
    """作弊型全毙（requirement_shadow 等）→ 维持 no_facts（D2 反向守门）。"""
    from app.engine.objective import score_count

    crit = _count_crit(verified_cases=[
        {"name": "A", "role": "member", "contract": False, "invoice": False,
         "acceptance": False, "quote": "须提供合同复印件发票复印件", "sanitized": "requirement_shadow"},
    ])
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("0")
    assert res.unresolved == "cases_unverified"
    assert res.zero_reason == "no_facts"


def test_score_count_positive_score_has_no_zero_reason() -> None:
    """约定：score>0 不携带 zero_reason（避免下游误读）。"""
    from app.engine.objective import score_count

    crit = _perf_crit(verified_cases=[
        {"name": "A", "contract": True, "invoice": True, "acceptance": True,
         "case_source": "table", "sanitized": "ok"},
    ])
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score > 0
    assert res.zero_reason is None


def test_score_count_iso_gate_unreadable_via_doc_warnings() -> None:
    """ISO 截图节门槛 + 本文档解析警告含 OCR 超限 → zero_reason="unreadable"。

    run 60 实证：航天拓扑"超限跳过页"→ 截图页没进文本层 →"未见截图节"0 分
    不能伪装成确认缺失。
    """
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    facts.certificates = ["ISO9001"]
    crit = {
        "name": "投标人认证及资质情况", "max_score": Decimal("3"),
        "required_artifacts": "查询结果截图", "cert_section_present": False,
        # 生产形态：Document.warnings_json 条目是 {"code","message"} dict
        "doc_parse_warnings": [{"code": "WARN", "message": "OCR 汇总：识别 10 页，超限跳过 92 页"}],
    }
    res = score_count(crit, {"field": "certificates", "per_item": "1", "max_items": "3"}, facts)
    assert res.score == Decimal("0")
    assert res.zero_reason == "unreadable"


def test_score_count_iso_gate_confirmed_absent_without_doc_warnings() -> None:
    """ISO 截图节门槛 + 无 OCR 警告 → zero_reason="confirmed_absent"（确认未提供）。"""
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    facts.certificates = ["ISO9001"]
    crit = {
        "name": "投标人认证及资质情况", "max_score": Decimal("3"),
        "required_artifacts": "查询结果截图", "cert_section_present": False,
        "doc_parse_warnings": [],
    }
    res = score_count(crit, {"field": "certificates", "per_item": "1", "max_items": "3"}, facts)
    assert res.score == Decimal("0")
    assert res.zero_reason == "confirmed_absent"


def test_score_count_iso_gate_unreadable_via_per_page_warning() -> None:
    """逐页 OCR 超限文案（无"超限跳过"字样）同样判 unreadable。

    2026-10-29 审查 P2：pdf_pymupdf 逐页警告是「已超出 OCR 页数上限（60），跳过」，
    只认"超限跳过"（汇总行才有）会漏匹配。
    """
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    facts.certificates = ["ISO9001"]
    crit = {
        "name": "投标人认证及资质情况", "max_score": Decimal("3"),
        "required_artifacts": "查询结果截图", "cert_section_present": False,
        "doc_parse_warnings": ["第 111 页疑似扫描件，已超出 OCR 页数上限（60），跳过"],
    }
    res = score_count(crit, {"field": "certificates", "per_item": "1", "max_items": "3"}, facts)
    assert res.score == Decimal("0")
    assert res.zero_reason == "unreadable"


def test_score_count_iso_gate_no_facts_not_overridden_to_confirmed() -> None:
    """证书本身没提取到（no_facts）时 ISO 门不覆盖为"确认未提供"。

    2026-10-29 审查 P2：facts.certificates 空 → unresolved="no_facts"（:624），
    ISO 截图门曾把它覆盖成 confirmed_absent——同一格同时带两个矛盾审计信号。
    """
    from app.engine.objective import score_count

    facts = BidFacts(text="")  # certificates 与 cert_claims 均空 → no_facts
    crit = {
        "name": "投标人认证及资质情况", "max_score": Decimal("3"),
        "required_artifacts": "查询结果截图", "cert_section_present": False,
        "doc_parse_warnings": [],
    }
    res = score_count(crit, {"field": "certificates", "per_item": "1", "max_items": "3"}, facts)
    assert res.score == Decimal("0")
    assert res.zero_reason == "no_facts"


def test_score_count_cmmi_field_counts_single_certificate() -> None:
    """CMMI 独立计分项（field=cmmi）：有 CMMI 证书 → 得 per_item 满分 2；
    无 → 0 且 confirmed_absent。真实细则「CMMI 二级以上得 2 分」独立于 ISO 项。

    2026-10-30 run 66 实证：LLM 曾把 CMMI 编译成 match 判断项不计分，
    四家 CMMI 2 分全丢；改 field=cmmi 独立计数。
    """
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    facts.certificates = ["ISO9001", "ISO27001", "ISO20000", "CMMI 五级"]
    crit = {
        "name": "投标人认证（CMMI）", "max_score": Decimal("2"),
        "required_artifacts": "证书复印件",
    }
    res = score_count(crit, {"field": "cmmi", "per_item": "2", "max_items": "1"}, facts)
    assert res.score == Decimal("2")  # 有 CMMI → 1 项 × 2 分
    assert res.zero_reason is None

    facts2 = BidFacts(text="")
    facts2.certificates = ["ISO9001", "ISO27001"]  # 有 ISO 但无 CMMI
    res2 = score_count(crit, {"field": "cmmi", "per_item": "2", "max_items": "1"}, facts2)
    assert res2.score == Decimal("0")
    assert res2.zero_reason == "confirmed_absent"


def test_score_count_certificates_merges_cmmi_only_when_text_names_it() -> None:
    """certificates 分支：细则文本点名 CMMI 才与 ISO 合并计数；纯 ISO 项不吞 CMMI。

    2026-10-30 run 66 实证：c47 箴文只列 ISO 三系（不含 CMMI 字样），旧逻辑
    count=len(iso_set)+has_cmmi=4 被 max_items=3 cap 吞掉 CMMI 那 1 项，
    四家 CMMI 2 分全丢。现在纯 ISO 项只数 ISO，CMMI 由独立 cmmi 条目计。
    """
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    facts.certificates = ["ISO9001", "ISO27001", "ISO20000", "CMMI 五级"]

    # 纯 ISO 项（文本不点名 CMMI）：只数 ISO 三系，CMMI 不被 cap 吞
    crit_iso = {
        "name": "投标人认证（管理体系认证）",
        "description": "投标人具有有效期内的ISO9001、ISO27001、ISO20000证书的，每项得1分。最高得3分。",
        "max_score": Decimal("3"),
        "required_artifacts": "查询结果截图", "cert_section_present": True,
    }
    res = score_count(crit_iso, {"field": "certificates", "per_item": "1", "max_items": "3"}, facts)
    assert res.score == Decimal("3")  # ISO 三系 3 项；CMMI 不进 cap

    # 合并口径（文本点名 CMMI）：ISO + CMMI 一起计，向后兼容
    crit_merged = {
        "name": "投标人资质认证情况",
        "description": "投标人具有ISO9001、ISO27001、ISO20000证书每项1分，CMMI认证二级以上1分。",
        "max_score": Decimal("4"),
        "required_artifacts": "证书复印件", "cert_section_present": True,
    }
    res2 = score_count(crit_merged, {"field": "certificates", "per_item": "1", "max_items": "4"}, facts)
    assert res2.score == Decimal("4")  # ISO 3 + CMMI 1 = 4 项（max_items=4 不 cap）


def test_score_type_for_row_cmmi_returns_count() -> None:
    """reconcile：CMMI 存在性计分行 → count/field=cmmi（不再被 LLM 编成 match 丢分）。"""
    from app.rules.reconcile import _score_type_for_row

    row = type("R", (), {
        "subjectivity": "objective",
        "text": "具有有效期内CMMI 认证二级及以上得 2 分。",
    })()
    score_type, params = _score_type_for_row(row)
    assert score_type == "count"
    assert params.get("field") == "cmmi"
    assert params.get("per_item") == "2"


def test_count_zero_warn_entry_unreadable_forces_need_human() -> None:
    """落库映射：unreadable 零分 → 警告文案"机器不可见"+ unreadable_zero=True。

    调用方（Phase2）据此强制 machine_status="need_human" +
    review_status="conflict_flagged"——0 分不得伪装成确定性评审结论。
    """
    from types import SimpleNamespace

    from app.services.scoring_service import _count_zero_warn_entry

    result = SimpleNamespace(
        score=Decimal("0"), detail="逐案例核验：提供案例 5 个，其中可计 0 个"
        "（5 个案例材料页不可读未计，可能为扫描件，建议人工核看）",
        unresolved=None,
    )
    entry, unreadable_zero = _count_zero_warn_entry(result, {"zero_reason": "unreadable"})
    assert unreadable_zero is True
    assert "机器不可见" in entry["message"]
    assert "非确认缺失" in entry["message"]
    assert entry["zero_reason"] == "unreadable"


def test_count_zero_warn_entry_confirmed_absent文案区分() -> None:
    """confirmed_absent 零分 → 文案"确认未提供"，不强制 unreadable 转人工。"""
    from types import SimpleNamespace

    from app.services.scoring_service import _count_zero_warn_entry

    result = SimpleNamespace(
        score=Decimal("0"), detail="逐案例核验：提供案例 2 个，其中可计 0 个（2 个缺证明材料不计分）",
        unresolved=None,
    )
    entry, unreadable_zero = _count_zero_warn_entry(result, {"zero_reason": "confirmed_absent"})
    assert unreadable_zero is False
    assert "确认未提供" in entry["message"]
    assert entry["zero_reason"] == "confirmed_absent"


# ---- 闸门丢弃分账（D1，2026-10-29）------------------------------------------
def test_score_count_null_drop_without_strict_marks_unreadable() -> None:
    """非严格键（未点名验收）上的 None 丢弃 → unreadable（旧口径混进
    incomplete → confirmed_absent/pass，OCR 没看见伪装成确认缺失）。"""
    from app.engine.objective import score_count

    crit = _perf_crit(
        required_artifacts="合同复印件、发票复印件",  # 未点名验收 → 无 strict 键
        verified_cases=[
            {"name": "A", "contract": None, "invoice": None, "acceptance": None,
             "sanitized": "ok"},
        ],
    )
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("0")
    assert res.zero_reason == "unreadable"
    assert "不可读" in res.detail


def test_score_count_false_drop_stays_confirmed_absent() -> None:
    """反向守门：纯 False 丢弃仍归 confirmed_absent（D1 不改计分语义）。"""
    from app.engine.objective import score_count

    crit = _perf_crit(verified_cases=[
        {"name": "A", "contract": False, "invoice": False, "acceptance": False,
         "sanitized": "ok"},
    ])
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("0")
    assert res.zero_reason == "confirmed_absent"
    assert "缺证明材料" in res.detail


def test_score_count_mixed_drop_reasons_prefers_unreadable() -> None:
    """False 与 None 混合丢弃且总分 0 → 优先 unreadable（不可读丢弃存在即转人工）。"""
    from app.engine.objective import score_count

    crit = _perf_crit(verified_cases=[
        {"name": "A", "contract": True, "invoice": True, "acceptance": True,
         "sanitized": "ok"},
        {"name": "B", "contract": False, "invoice": None, "acceptance": None,
         "sanitized": "ok"},
        {"name": "C", "contract": None, "invoice": None, "acceptance": None,
         "sanitized": "ok"},
    ])
    res = score_count(crit, crit["params"], BidFacts(text=""))
    # A 可计 2 分 → score>0 不带 zero_reason；改为只有 B/C（A 材料不全）
    crit2 = _perf_crit(verified_cases=[
        {"name": "B", "contract": False, "invoice": None, "acceptance": None,
         "sanitized": "ok"},
        {"name": "C", "contract": None, "invoice": None, "acceptance": None,
         "sanitized": "ok"},
    ])
    res2 = score_count(crit2, crit2["params"], BidFacts(text=""))
    assert res2.score == Decimal("0")
    assert res2.zero_reason == "unreadable"
    assert "1 个缺证明材料" in res2.detail and "1 个案例材料页不可读" in res2.detail


def test_score_count_type_gate_zero_reason() -> None:
    """类型门槛剔空 → zero_reason='type_gate'（类型判断 ≠ 未提供材料）。"""
    from app.engine.objective import score_count

    crit = _count_crit(
        name="项目负责人具有类似运维项目经验",
        params={"person_scope": "leader", "per_item": "2", "max_items": "2"},
        verified_cases=[
            {"name": "XX系统集成建设项目", "role": "leader", "contract": True,
             "invoice": True, "acceptance": True, "sanitized": "ok"},
            {"name": "YY网络建设项目", "role": "leader", "contract": True,
             "invoice": True, "acceptance": True, "sanitized": "ok"},
        ],
    )
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("0")
    assert res.zero_reason == "type_gate"
    assert "类型门槛" in res.detail


def test_count_zero_warn_entry_type_gate_marks_need_human() -> None:
    """落库映射：type_gate 零分 → unreadable_zero=True（转人工）+ 文案明示类型门槛。"""
    from types import SimpleNamespace

    from app.services.scoring_service import _count_zero_warn_entry

    result = SimpleNamespace(
        score=Decimal("0"),
        detail="逐案例核验：提供案例 0 个，其中可计 0 个（按'类似运维项目'类型门槛剔除 2 个集成/维保/建设类案例）",
        unresolved=None,
    )
    entry, unreadable_zero = _count_zero_warn_entry(result, {"zero_reason": "type_gate"})
    assert unreadable_zero is True
    assert "类型门槛" in entry["message"]
    assert entry["zero_reason"] == "type_gate"


# ---- _sanitize_case 降级分档（C1，2026-10-29）--------------------------------
def test_sanitize_quote_not_in_bid_sets_null_not_false() -> None:
    """定位失败型（quote_not_in_bid）→ 三件套 None + unverified 留痕（非作弊证据）。"""
    from app.services.scoring_service import _sanitize_case

    case = {
        "name": "某项目案例名称足够长", "contract": True, "invoice": True, "acceptance": True,
        "quote": "这段引文在标书里根本找不到内容A",
    }
    out = _sanitize_case(case, REQ_TEXT, bid_norm="标书全文里只有完全无关的内容")
    assert out["sanitized"] == "quote_not_in_bid"
    assert out["unverified"] == "quote_not_in_bid"
    assert all(out[k] is None for k in ("contract", "invoice", "acceptance"))


def test_sanitize_requirement_shadow_stays_false() -> None:
    """作弊型（requirement_shadow）维持三件套 False（C1 反向守门）。"""
    from app.services.scoring_service import _sanitize_case

    case = {
        "name": "某项目", "contract": True, "invoice": True, "acceptance": True,
        "quote": "须提供合同复印件、发票复印件和业主证明",
    }
    out = _sanitize_case(case, REQ_TEXT, bid_norm="无关全文")
    assert out["sanitized"] == "requirement_shadow"
    assert all(out[k] is False for k in ("contract", "invoice", "acceptance"))


# ---- 人员级候选页保留材料标题页（Fix1，2026-10-29）---------------------------
def test_case_candidate_pages_person_keeps_material_heading_pages() -> None:
    """人员级过滤不得丢弃材料标题页（发票/验收/中标通知书扫描页）。

    7.10 实证：旧词表把业绩证明材料页整页排除 → LLM 视野 0 张材料页 →
    逐案例判"缺证明材料"（航天拓扑 32 页仅 3 页送审、派克 0 页）。
    """
    from app.services.scoring_service import _case_candidate_pages

    pages = [
        (1, "拟派团队成员一览表 项目经理：张三 社保", "text"),          # 人员证据页
        (2, "普通正文" * 100, "text"),                                  # 无关页
        (3, "2. 发票扫描件 北京增值税专用发票", "ocr"),                 # 材料标题页
        (4, "4. 验收报告 验收结论 合格", "ocr"),                        # 材料标题页
        (5, "合同金额：100万元 甲方：某公司 乙方：某公司", "text"),      # 纯合同头页（仍排除）
    ]
    batches = _case_candidate_pages(pages, person_scope="member")
    sent = {no for b in batches for no, _t in b}
    assert 1 in sent and 3 in sent and 4 in sent
    assert 5 not in sent, "纯合同头页仍须排除（c59/c60 回归防线）"


def test_case_candidate_pages_section_includes_non_anchor_pages() -> None:
    """section 感知：段内无锚点词的页并入候选，锚点页仍排前（B1）。"""
    from app.services.scoring_service import _case_candidate_pages

    pages = [(i, f"无关内容第{i}页" * 20, "text") for i in range(1, 60)]
    pages[49] = (50, "合同名称：某运维项目 甲方：浙江中烟 合同金额：2,208,000元", "text")
    batches = _case_candidate_pages(pages, section=(45, 55))
    sent = [no for b in batches for no, _t in b]
    assert 50 in sent and 46 in sent and 54 in sent, "section 内页必须并入候选"
    assert sent.index(50) < sent.index(46), "锚点页应排在 section 非锚点页之前"
