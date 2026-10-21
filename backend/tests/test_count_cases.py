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
        {"name": "A", "role": "团队成员", "contract": True, "invoice": True,
         "acceptance": True, "quote": "2023年合同，金额800000元", "sanitized": "ok"},
    ])
    result = score_count(crit, crit["params"], facts)
    assert result.score == Decimal("1.5")


def test_score_count_no_cases_stays_unresolved() -> None:
    from app.engine.objective import score_count

    facts = BidFacts(text="")
    crit = _count_crit()
    result = score_count(crit, crit["params"], facts)
    assert result.unresolved == "field_missing"


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


def test_sanitize_no_bid_norm_trusts_model() -> None:
    """未接 bid_norm 时：真证据引文 → ok（向后兼容，不误杀未接线的调用方）。"""
    out = _sanitize("2023年6月签订合同金额2208000元服务期三年", req=REQ_TEXT)
    assert out["sanitized"] == "ok"


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
        {"name": "A", "role": "member", "contract": True, "invoice": True,
         "acceptance": True, "quote": "2023年合同金额80万元", "sanitized": "ok"},
        {"name": "B", "role": "member", "contract": False, "invoice": False,
         "acceptance": False, "quote": "须提供合同复印件", "sanitized": "requirement_shadow"},
        {"name": "C", "role": "member", "contract": False, "invoice": False,
         "acceptance": False, "quote": "长伪造引文内容在此", "sanitized": "quote_not_in_bid"},
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
