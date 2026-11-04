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


def test_score_count_legit_case_missing_material_scores_zero(monkeypatch) -> None:  # noqa: ANN001
    """回退模式：合法案例但材料缺失 → 0 分是真实评审结论（先数业绩 0）。

    窄版默认开时 None 计入+标注；此用例验证关闭开关（BID_EVAL_CASE_UNVERIFIED_COUNT=0）
    的回退路径仍 None 不计 → 0。
    """
    from app.engine.objective import score_count

    monkeypatch.setenv("BID_EVAL_CASE_UNVERIFIED_COUNT", "0")
    facts = BidFacts(text="")
    crit = _count_crit(verified_cases=[
        {"name": "A", "role": "member", "contract": True, "invoice": False,
         "acceptance": False, "quote": "2023年6月签订合同，金额80万元", "sanitized": "ok"},
    ])
    result = score_count(crit, crit["params"], facts)
    assert result.unresolved is None
    assert result.score == Decimal("0")
    assert result.conflict is True


def test_score_count_unverified_none_counted_and_marked(monkeypatch) -> None:  # noqa: ANN001
    """窄版（默认开）：材料 None（不可读/未绑上）→ 计入 + unverified 标注 + conflict。

    2026-10-30 用友 c54/c55 实证：材料在、机器核不上却判 0，人工给 4。窄版把
    管线故障（OCR 没读出/没绑上/页没送审）与"确认缺失"区分开——None 计入+标注，
    只有 False（可读文本确认缺失）才维持 0。
    """
    from app.engine.objective import score_count

    crit = _perf_crit(verified_cases=[
        {"name": "A", "contract": True, "invoice": None, "acceptance": None,
         "sanitized": "ok"},
    ])
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("2.00")  # None 计入（旧口径 0）
    assert res.conflict is True
    assert res.zero_reason is None  # score>0 不携带零分理由
    assert "未核验计入" in res.detail


def test_score_count_false_stays_zero_in_narrow() -> None:
    """窄版下 any False（确认缺失）仍 →0（规则"不全不得分"硬口径不变）。"""
    from app.engine.objective import score_count

    crit = _perf_crit(verified_cases=[
        {"name": "A", "contract": True, "invoice": False, "acceptance": True,
         "sanitized": "ok"},
    ])
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("0")
    assert res.zero_reason == "confirmed_absent"


def test_score_count_max_items_cuts_unverified_first() -> None:
    """排序收紧（2026-11-02 run77 c41）：max_items 封顶先砍未核验案例——
    verified（材料全 True）优先计满，unverified（材料 None）排尾被截。

    run77 实证：德勤 11 个不可读案例全部计入被 max_score 封顶得 4/4 满分，
    人工只认 3 个有效业绩。双防（用友/德勤）：verified 案例不受影响，未核验
    案例不再挤占 verified 的名额。
    """
    from app.engine.objective import score_count

    cases = (
        # 3 个未核验（排尾）
        [{"name": f"U{i}", "contract": True, "invoice": None, "acceptance": None,
          "sanitized": "ok"} for i in range(3)]
        +  # 2 个 verified（排前）
        [{"name": f"V{i}", "contract": True, "invoice": True, "acceptance": True,
          "sanitized": "ok"} for i in range(2)]
    )
    # max_items=3：verified 2 个 + unverified 1 个 = 3 × 2 = 6；剔除未核验 = 4
    crit = _perf_crit(verified_cases=cases)
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("6.00")
    assert res.score_verified_only == Decimal("4.00")
    assert res.unverified_counted == 1  # 只有 1 个未核验挤进名额
    assert "剔除后为 4.00 分" in res.detail


def test_score_count_unverified_dual_score_recomputable() -> None:
    """双分值可复算：score（含未核验）与 score_verified_only（剔除后）并存。"""
    from app.engine.objective import score_count

    crit = _perf_crit(verified_cases=[
        {"name": "V", "contract": True, "invoice": True, "acceptance": True,
         "sanitized": "ok"},
        {"name": "U", "contract": True, "invoice": None, "acceptance": None,
         "sanitized": "ok"},
    ])
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("4.00")           # 2 个可计 × 2
    assert res.score_verified_only == Decimal("2.00")  # 剔除未核验后
    assert res.unverified_counted == 1
    assert res.conflict is True


def test_score_count_all_verified_no_dual_score() -> None:
    """全 verified 时 score_verified_only=None（score 即结论，不制造噪音）。"""
    from app.engine.objective import score_count

    crit = _perf_crit(verified_cases=[
        {"name": "V", "contract": True, "invoice": True, "acceptance": True,
         "sanitized": "ok"},
    ])
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("2.00")
    assert res.score_verified_only is None
    assert res.unverified_counted == 0


def test_llm_verify_truncation_downgrades_false(monkeypatch):  # noqa: ANN001
    """截断降级：候选页超硬顶被截断时，sanitized=="ok" 的 False→None（未核验）。

    模型没看到省略页，"确认缺失"(False) 不可信——run 72 实证：87 页只送审 71、
    16 页没看，模型"没找到材料"实为没看到 → 误判 0。防作弊拒绝（sanitized!=ok）
    的 False 是"案例造假"不是"材料缺失"，不降级。
    """
    from types import SimpleNamespace

    from app.services import scoring_service as ss

    # fake gateway：返回2 个 contract=False 的案例，一个 ok、一个 requirement_shadow
    class _GW:
        def chat_json(self, system, user, out_model, data_class="synthetic"):  # noqa: ANN001
            cases = [
                {"name": "甲项目", "contract": False, "invoice": False, "acceptance": False,
                 "quote": "我司承接该项目，合同金额捌拾万元整，服务期一年。"},
                {"name": "乙项目", "contract": False, "invoice": False, "acceptance": False,
                 "quote": REQ_TEXT},  # 照抄要求 → 防作弊降级
            ]
            return SimpleNamespace(cases=cases), None

    # monkeypatch 切批：强制截断（pages_omitted=1），返回1 个批次（(页号,文本) 二元组）
    def _fake_cand(pages, **kw):  # noqa: ANN001
        if kw.get("stats_out") is not None:
            kw["stats_out"]["pages_omitted"] = 1
        return [[(1, "第1页正文合同项目名称甲方乙方")]]

    monkeypatch.setattr(ss, "_case_candidate_pages", _fake_cand)
    crit = _perf_crit()
    stats = {}
    out = ss._llm_verify_cases(
        _GW(), crit, REQ_TEXT, ["合同复印件"], None,
        [(1, "第1页正文合同项目名称甲方乙方", "text")],
        "synthetic", bid_norm=None, stats_out=stats,
    )
    assert stats["pages_omitted"] == 1
    by_name = {c["name"]: c for c in out}
    # ok 案例：False→None + truncated_review
    assert by_name["甲项目"]["contract"] is None
    assert by_name["甲项目"].get("truncated_review") is True
    # 防作弊案例：False 不降级
    assert by_name["乙项目"]["contract"] is False


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


def _leader_table_pages() -> list[tuple[int, str, str]]:
    """用友 c73 形态：表头 + 明细列 + 姓名（三条件齐）。"""
    return [
        (73, "项目负责人情况表\n业绩及承担的主要工作情况，曾担任项目经理的项目应列明细\n"
             "吴丽君 页码 姓名 2018.07-2019.12 供应链协", "text"),
        (99, "个人简历\n宋宏 男 硕士 高级工程师", "ocr"),  # 简历页（无表头）
    ]


def test_overlay_leader_table_proof_upgrades() -> None:
    """leader 表证宽松口径：表头+明细列+姓名 三条件齐 → False/None 升 True，已是 True 保持。"""
    from app.services.scoring_service import _overlay_leader_table_proof

    cases = [
        {"person_name": "吴丽君", "work_proof": False},  # LLM 严判 → 升 True
        {"person_name": "吴丽君", "work_proof": None},   # None → 升 True
        {"person_name": "吴丽君", "work_proof": True},   # 已 True → 保持
    ]
    _overlay_leader_table_proof(cases, _leader_table_pages())
    assert cases[0]["work_proof"] is True
    assert cases[1]["work_proof"] is True
    assert cases[2]["work_proof"] is True


def test_overlay_leader_table_proof_narrow_no_false_positive() -> None:
    """窄词表：只有姓名无表头（简历页）/无明细列/无该人名 → 不升（保持原判）。"""
    from app.services.scoring_service import _overlay_leader_table_proof

    pages = [
        (1, "个人简历 宋宏 男 硕士 高级工程师", "ocr"),          # 姓名在，无表头
        (2, "人员情况表\n姓名 部门 职务", "text"),                # 表头在，无明细列
        (3, "项目负责人情况表\n曾担任项目经理的项目\n张三 2020项目", "text"),  # 三条件齐但人名是张三
    ]
    cases = [
        {"person_name": "宋宏", "work_proof": False},   # 简历页 → 不升
        {"person_name": "张三", "work_proof": False},   # 人名在表页 → 升 True
        {"person_name": "李四", "work_proof": None},    # 表页无此人 → 不改写 None
    ]
    _overlay_leader_table_proof(cases, pages)
    assert cases[0]["work_proof"] is False  # 无表头
    assert cases[1]["work_proof"] is True   # 三条件齐（张三在明细表页）
    assert cases[2]["work_proof"] is None   # 无该人名 → 保持原判不强 False


def test_overlay_leader_table_proof_only_upgrades_and_switch(monkeypatch) -> None:  # noqa: ANN001
    """只升不降：无表页时 True 不被改；开关关 → 全程不动。"""
    from app.services.scoring_service import _overlay_leader_table_proof

    # 无表页 → 直接 return，True 保持、False 保持
    cases = [{"person_name": "吴丽君", "work_proof": True},
             {"person_name": "吴丽君", "work_proof": False}]
    _overlay_leader_table_proof(cases, [(1, "纯正文无表", "text")])
    assert cases[0]["work_proof"] is True
    assert cases[1]["work_proof"] is False

    # 开关关 → 即使表页命中也不动
    monkeypatch.setenv("BID_EVAL_LEADER_TABLE_PROOF", "0")
    cases2 = [{"person_name": "吴丽君", "work_proof": False}]
    _overlay_leader_table_proof(cases2, _leader_table_pages())
    assert cases2[0]["work_proof"] is False


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


def test_score_count_null_without_contract_does_not_count(monkeypatch) -> None:  # noqa: ANN001
    """回退模式：合同都不可读 → 没有正向依据，不计（防通胀）。"""
    from app.engine.objective import score_count

    monkeypatch.setenv("BID_EVAL_CASE_UNVERIFIED_COUNT", "0")
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
def test_score_count_unreadable_drop_zero_reason(monkeypatch) -> None:  # noqa: ANN001
    """回退模式：点名材料页不可读(None)丢弃 → score=0 + zero_reason="unreadable"。

    与 confirmed_absent 分离：不可读可能是扫描件（机器看不见），
    不是"确认未提供"；落库侧强制 need_human + conflict_flagged。
    窄版默认开时 None 计入+标注；此用例验证关闭开关的回退路径。
    """
    from app.engine.objective import score_count

    monkeypatch.setenv("BID_EVAL_CASE_UNVERIFIED_COUNT", "0")
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


def test_score_count_merged_cmmi_uses_text_points() -> None:
    """合并项（ISO+CMMI 同一 count）：CMMI 按文本分值计，不被 per_item=1 拉平。

    run 66 实证 c41：真实「ISO 三系各1分 + CMMI 二级以上得2分」=5，旧逻辑
    count=4×per_item=4 少 1 分（CMMI 被算成 1 分）。文本解析到"得2分"→
    ISO 3×1 + CMMI 2 = 5，对齐人工。
    """
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    facts.certificates = ["ISO9001", "ISO27001", "ISO20000", "CMMI 五级"]
    crit = {
        "name": "投标人认证及资质情况",
        "description": (
            "投标人具有有效期内的ISO9001、ISO27001、ISO20000证书的，每项得1分。"
            "最高得3分。具有有效期内CMMI认证二级及以上得2分。"
        ),
        "max_score": Decimal("5"),
        "required_artifacts": "查询结果截图", "cert_section_present": True,
    }
    res = score_count(crit, {"field": "certificates", "per_item": "1", "max_items": "5"}, facts)
    assert res.score == Decimal("5")  # ISO 3×1 + CMMI 2 = 5（旧逻辑 4×1=4）

    # 有 ISO 无 CMMI：只 ISO 3 分，CMMI 分值不误加
    facts2 = BidFacts(text="")
    facts2.certificates = ["ISO9001", "ISO27001", "ISO20000"]
    res2 = score_count(crit, {"field": "certificates", "per_item": "1", "max_items": "5"}, facts2)
    assert res2.score == Decimal("3")


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
def test_score_count_null_drop_without_strict_marks_unreadable(monkeypatch) -> None:  # noqa: ANN001
    """回退模式：非严格键（未点名验收）上的 None 丢弃 → unreadable（旧口径混进
    incomplete → confirmed_absent/pass，OCR 没看见伪装成确认缺失）。"""
    from app.engine.objective import score_count

    monkeypatch.setenv("BID_EVAL_CASE_UNVERIFIED_COUNT", "0")
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


def test_score_count_mixed_drop_reasons_prefers_unreadable(monkeypatch) -> None:  # noqa: ANN001
    """回退模式：False 与 None 混合丢弃且总分 0 → 优先 unreadable（不可读丢弃存在即转人工）。"""
    from app.engine.objective import score_count

    monkeypatch.setenv("BID_EVAL_CASE_UNVERIFIED_COUNT", "0")
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


def test_audit_whitelist_keeps_tristate_keys() -> None:
    """审计白名单必须保留三态键（2026-11-02）：unverified/unverified_reason/
    truncated_review 曾被丢弃，落库后只能从 warnings 中文文案反读。"""
    from app.services.scoring_service import _AUDIT_CASE_KEYS

    for key in ("unverified", "unverified_reason", "truncated_review",
                "sanitized", "material_reasons", "quote", "name"):
        assert key in _AUDIT_CASE_KEYS, key


# --- R0-3 人员经验档表（2026-11-03 run78）+ R3-1 flags + R3-2 prompt 契约 ----

def test_score_count_person_tiers_by_min_count() -> None:
    """用友式：「2个经验得1分；4个得2分」档表——2 个 verified 案例→1 分、4 个→2 分。"""
    from app.engine.objective import score_count

    def _cases(n):
        return [{"name": f"P{i}", "role": "leader", "work_proof": True,
                 "experience_proof": True, "social_security": True,
                 "quote": f"项目{i} 运维负责人：某人", "sanitized": "ok"}
                for i in range(n)]

    crit = _count_crit(
        name="项目负责人",
        params={"person_scope": "leader", "field": "performance",
                "tiers": [{"min_count": 2, "score": "1"}, {"min_count": 4, "score": "2"}]},
        required_artifacts="社保证明、业绩证明材料",
    )
    r2 = score_count({**crit, "verified_cases": _cases(2)}, crit["params"], BidFacts(text=""))
    assert r2.score == Decimal("1")
    r4 = score_count({**crit, "verified_cases": _cases(4)}, crit["params"], BidFacts(text=""))
    assert r4.score == Decimal("2")
    r1 = score_count({**crit, "verified_cases": _cases(1)}, crit["params"], BidFacts(text=""))
    assert r1.score == Decimal("0")  # 1 个不满足任何档


def test_score_count_person_tiers_unverified_double_value() -> None:
    """德勤式：案例材料不可读（None）→ 计入+疑问+双分值（剔除后口径可复算）。"""
    from app.engine.objective import score_count

    cases = [
        {"name": "A", "role": "leader", "work_proof": True,
         "experience_proof": None, "social_security": True,
         "quote": "项目A 运维负责人：某人", "sanitized": "ok"},
        {"name": "B", "role": "leader", "work_proof": True,
         "experience_proof": None, "social_security": True,
         "quote": "项目B 运维负责人：某人", "sanitized": "ok"},
    ]
    crit = _count_crit(
        name="项目负责人",
        params={"person_scope": "leader", "field": "performance",
                "tiers": [{"min_count": 2, "score": "1"}, {"min_count": 4, "score": "2"}]},
        required_artifacts="社保证明、业绩证明材料",
    )
    r = score_count({**crit, "verified_cases": cases}, crit["params"], BidFacts(text=""))
    assert r.score == Decimal("1")          # 2 个计入命中「≥2」档
    assert r.unverified_counted == 2        # 全部计入的都是未核验 → need_human
    assert r.score_verified_only == Decimal("0")  # 剔除后 0 分可复算


def test_required_flags_experience_three_years_and_cert() -> None:
    """R3-1：「3年以上经验」触发 experience_proof（旧只认五年/负责人）；
    细则点名证书（认证/职称/PMP）触发 cert_proof；无点名材料不越权。"""
    from app.engine.objective import _required_flags

    flags = _required_flags(
        "近6 个月社保证明、相关业绩证明材料",
        person_scope="member",
        criterion_name="驻厂运维人员",
        rule_text="驻厂运维人员具有本项目运维所需云平台的产品认证或培训证书，"
                  "并有3 年以上信息系统项目实施经验，每1 人得1 分",
    )
    assert "cert_proof" in flags
    assert "experience_proof" in flags
    assert "social_security" in flags
    assert "work_proof" in flags

    plain = _required_flags("", person_scope="member", criterion_name="团队成员",
                            rule_text="有类似项目经验的成员每1 人得1 分")
    assert "cert_proof" not in plain
    assert "experience_proof" not in plain


def test_material_count_prompt_cert_proof_contract() -> None:
    """R3-2 prompt 契约：cert_proof 字段 + 逐人四链句必须在提示词里。"""
    from app.rules.prompts import MATERIAL_COUNT_SYSTEM_PROMPT as P

    assert '"cert_proof":布尔|null' in P
    assert "逐人核验四链" in P
    assert "证书名+人名同现" in P


# --- A1 tier 形状守卫 + A2 零分诚实化（2026-11-04 run79 业绩回归） ------------

def test_score_count_amount_tiers_fall_back_to_per_item() -> None:
    """run79 回归钉死：金额档 tiers（min_amount_yuan 形状）不是计数档——
    查表无命中强制 0 分曾让 4 家「投标人业绩」4→0；必须回退 per_item 线性。"""
    from app.engine.objective import score_count

    crit = _perf_crit(
        per_item="1",
        max_items="4",
        params={"field": "performance", "per_item": "1", "max_items": "4",
                "tiers": [{"min_amount_yuan": "500000", "max_amount_yuan": "1000000",
                           "per_item": "0.5"},
                          {"min_amount_yuan": "1000000", "per_item": "1"}],
                "min_amount_yuan": "1000000"},
    )
    # 4 个全链 True 案例（对照 run79 云锦形态：3 全链 + 1 unlinked）
    cases = [
        {"name": f"A{i}", "contract": True, "invoice": True, "acceptance": True,
         "case_source": "table", "sanitized": "ok"} for i in range(3)
    ] + [{"name": "B", "contract": None, "invoice": None, "acceptance": None,
          "case_source": "table", "sanitized": "ok"}]
    res = score_count({**crit, "verified_cases": cases}, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("4"), f"金额档不得查计数档表：{res.score} {res.detail}"
    assert res.zero_reason is None
    assert res.unverified_counted == 1  # unlinked 案例计入并标疑问
    assert res.score_verified_only == Decimal("3")


def test_score_count_count_tiers_below_lowest_stays_zero() -> None:
    """反向守门：计数档（min_count 形状）低于最低档仍 0 分，不回退 per_item。"""
    from app.engine.objective import score_count

    crit = _count_crit(
        name="项目负责人",
        params={"person_scope": "leader", "field": "performance",
                "tiers": [{"min_count": 2, "score": "1"}, {"min_count": 4, "score": "2"}]},
        required_artifacts="社保证明、业绩证明材料",
    )
    cases = [{"name": "P0", "role": "leader", "work_proof": True,
              "experience_proof": True, "social_security": True,
              "quote": "项目0 运维负责人：某人", "sanitized": "ok"}]
    res = score_count({**crit, "verified_cases": cases}, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("0"), "1 个不满足任何计数档（≥2）→ 0 分"
    assert res.zero_reason == "confirmed_absent"  # 计数有依据的 0


def test_score_count_batch_failure_marks_cases_unverified() -> None:
    """A2：LLM 案例核验批失败（机器故障）→ cases_unverified，禁止伪装
    no_facts/confirmed_absent；surviving 案例照计+疑问。"""
    from app.engine.objective import score_count

    crit = _perf_crit(verified_cases=[
        {"name": "A", "contract": True, "invoice": True, "acceptance": True,
         "case_source": "llm", "sanitized": "ok"},
    ])
    crit["case_batches_failed"] = 2
    crit["case_batches_total"] = 7
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.score == Decimal("2"), "surviving 案例照计"
    assert res.conflict is True
    assert res.unresolved == "cases_unverified"
    assert "批失败" in res.detail
    # score>0 不带 zero_reason（既有约定）
    assert res.zero_reason is None


def test_score_count_batch_failure_zero_not_confirmed_absent() -> None:
    """全批失败（无 surviving 案例）：0 分落 cases_unverified，不是确认未提供。"""
    from app.engine.objective import score_count

    crit = _perf_crit(verified_cases=[])
    crit["case_batches_failed"] = 3
    crit["case_batches_total"] = 3
    # verified_cases 空 + 无 facts → 走 no_facts 路径；批失败存在时必须改判
    # cases_unverified（诚实细分）。
    res = score_count(crit, crit["params"], BidFacts(text=""))
    assert res.zero_reason == "cases_unverified", res.zero_reason
    assert res.zero_reason != "no_facts"
    assert res.zero_reason != "confirmed_absent"


def test_score_count_counted_but_zero_not_confirmed_absent() -> None:
    """A2：案例已计入却算出 0（per_item=0 的机器口径问题）——诚实标
    cases_unverified 转人工，绝不伪装"确认未提供"。"""
    from app.engine.objective import score_count

    crit = _perf_crit(verified_cases=[
        {"name": "A", "contract": True, "invoice": True, "acceptance": True,
         "case_source": "table", "sanitized": "ok"},
    ])
    # per_item=0：案例在、计入、但算不出分值
    params = {"field": "performance", "per_item": "0", "max_items": "3"}
    res = score_count({**crit, "verified_cases": crit["verified_cases"]}, params, BidFacts(text=""))
    assert res.score == Decimal("0")
    assert res.zero_reason == "cases_unverified"
    assert res.conflict is True
    assert "非确认未提供" in res.detail
