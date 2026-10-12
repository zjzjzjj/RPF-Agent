"""报告草稿/定稿与疑问清单的单元测试。

核心约定：报告**不再等人工复核完成**才可生成——先出草稿、把疑问逐条标出，
复核后从同一入口刷新即为定稿。这里钉住"疑问定性必须准确"这条底线：
误报会把真实问题淹没，漏报会把能力缺失当成评分结论。
"""
from __future__ import annotations

from decimal import Decimal

from app.engine.objective import _infer_count_field
from app.services.report_service import (
    QUESTION_KINDS,
    _build_open_questions,
    _classify_question,
)


class _Crit:
    def __init__(self, code="c1", name="某项", score_type="count", category="commercial",
                 max_score=Decimal("4")):
        self.code = code
        self.name = name
        self.score_type = score_type
        self.category = category
        self.max_score = max_score
        self.source_page = 1
        self.source_quote = "原文"


class _Bidder:
    id = 1
    name = "甲公司"


class _Score:
    def __init__(self, *, review_status="pending", machine_status="pass", final_score=Decimal("4"),
                 evidence_status="none", warnings=None, veto_flag=False,
                 ai_suggested_score=None):
        self.id = 1
        self.review_status = review_status
        self.machine_status = machine_status
        self.final_score = final_score
        self.evidence_status = evidence_status
        self.warnings_json = warnings
        self.veto_flag = veto_flag
        self.ai_suggested_score = ai_suggested_score


def _rows(score, crit):
    return [(score, crit, _Bidder())]


# ---- 疑问定性 ----------------------------------------------------------------

def test_settled_item_is_not_a_question():
    """人工已处置的项不再是疑问。"""
    for st in ("human_confirmed", "human_overridden"):
        s = _Score(review_status=st)
        assert _classify_question(s, _Crit()) is None


def test_machine_pass_is_low_severity_not_high():
    """机器判定通过 + 有分值 → 只是按设计走复核流，不该报成高危。"""
    kind = _classify_question(_Score(), _Crit())
    assert kind == "batch_confirmable"
    assert QUESTION_KINDS[kind]["severity"] == "low"


def test_not_located_material_still_reports_veto_recommendation():
    """机器"没找到材料"不得被降级成"只是定位失败"。

    document_present 按招标文件载明的完整材料名逐字匹配，真实标书极少逐字复现
    （浙江中烟项目 5 家全部命中），但**样例中故意缺保证金的真阳性报的是同一句话**。
    机器无法区分两者，替它下"这是定位问题"的结论就是猜测——违背"宁缺勿错"。
    正确做法：如实报为建议否决，措辞里说明两种可能，由人翻阅原件裁决。
    """
    crit = _Crit(score_type="veto", name="投标保证金")
    s = _Score(machine_status="recommend_reject", final_score=None,
               review_status="conflict_flagged",
               warnings=[{"message": "投标文件中未找到投标保证金缴纳凭证"}])
    assert _classify_question(s, crit) == "veto_recommended"


def test_veto_action_text_covers_both_possibilities():
    """建议否决的处置说明必须同时说清"材料存在"与"确属缺失"两种走向。"""
    action = QUESTION_KINDS["veto_recommended"]["action"]
    assert "绝不自动否决" in action
    assert "通过" in action and "否决事由" in action


def test_genuine_veto_recommendation_stays_high():
    """真正的"实质不符"仍应报为建议否决。"""
    crit = _Crit(score_type="veto", name="投标有效期")
    s = _Score(machine_status="recommend_reject", final_score=None,
               review_status="conflict_flagged",
               warnings=[{"message": "投标有效期 90 天 < 要求 120 天"}])
    assert _classify_question(s, crit) == "veto_recommended"


def test_veto_pass_is_low_severity_not_settled():
    """机器判定通过的资格/符合性项按设计仍走复核流，归低危而非"已处置"。

    回归：曾直接返回 None（视为已处置），于是报告宣称"定稿：全部 N 项均已人工处置"，
    而工作台与复核队列仍把这批列为待复核——同一时刻两处结论相反。
    """
    crit = _Crit(score_type="veto", name="独立法人资格")
    kind = _classify_question(_Score(machine_status="pass", final_score=None), crit)
    assert kind == "batch_confirmable"
    assert QUESTION_KINDS[kind]["severity"] == "low"


def test_veto_need_human_must_stay_a_question():
    """机器判不了的否决项（密封/签字/盖章）必须留在疑问清单里。

    回归：曾把它们当作"不是疑问"，于是人工处置完其余项后 progress.open==0，
    报告宣称"定稿：全部 N 项均已人工处置"——而这些否决事由从未被裁决。
    """
    crit = _Crit(score_type="veto", name="投标文件签章")
    s = _Score(machine_status="need_human", final_score=None,
               warnings=[{"message": "该项需人工判定（如密封、签字、盖章）"}])
    kind = _classify_question(s, crit)
    assert kind == "veto_needs_judgment"
    assert QUESTION_KINDS[kind]["severity"] == "high"


def test_report_never_claims_final_while_veto_unadjudicated():
    """只要还有未裁决的否决项，progress.open 就不能归零。"""
    crit = _Crit(score_type="veto", name="投标文件签章")
    rows = [
        (_Score(review_status="human_confirmed"), _Crit(score_type="count"), _Bidder()),
        (_Score(machine_status="need_human", final_score=None), crit, _Bidder()),
    ]
    questions, progress, _by_crit = _build_open_questions(rows, 1)
    assert progress["open"] == 1
    assert progress["settled"] == 1


def test_machine_computed_score_does_not_require_citation():
    """formula/count 的证据是计算过程本身，不该报"证据未核验"。

    evidence_status 对这类分值恒为 none；若不区分，所有机器计算分都会被误报。
    """
    for st in ("count", "formula", "match"):
        kind = _classify_question(_Score(evidence_status="none"), _Crit(score_type=st))
        assert kind == "batch_confirmable", st


def test_subjective_without_verified_evidence_is_high():
    """主观分必须带已核验证据——这是"无证据不评分"的红线。"""
    crit = _Crit(score_type="subjective", name="运维方案", max_score=Decimal("9"))
    kind = _classify_question(_Score(evidence_status="unverified"), crit)
    assert kind == "evidence_unverified"
    assert QUESTION_KINDS[kind]["severity"] == "high"


def test_count_field_unresolved_is_high_not_batch_confirmable():
    """计数口径未确定时的 0 分是能力缺失，不是评分结论，必须显性标出。"""
    crit = _Crit(score_type="count", name="项目负责人经验")
    s = _Score(final_score=Decimal("0"),
               warnings=[{"message": "…得 0.00 分", "unresolved": "field_missing"}])
    kind = _classify_question(s, crit)
    assert kind == "count_field_unresolved"
    assert QUESTION_KINDS[kind]["severity"] == "high"


def test_count_no_facts_is_medium():
    crit = _Crit(score_type="count", name="投标人业绩")
    s = _Score(final_score=Decimal("0"),
               warnings=[{"message": "…", "unresolved": "no_facts"}])
    assert _classify_question(s, crit) == "count_no_facts"


def test_score_count_marks_unextracted_facts_distinctly():
    """一条都没抽到 ≠ 一条都不合格。

    前者是抽取能力问题，必须与"确实 0 项"区分——否则 0 分被当成评分结论
    进入总分，下游还会把该项归为"无实质疑问"允许批量确认。
    """
    from app.engine.facts import extract_bid_facts
    from app.engine.objective import score_count

    crit = {"name": "投标人业绩", "description": "", "max_score": Decimal("4")}
    facts = extract_bid_facts("本投标文件不含任何业绩表格。")
    r1 = score_count(crit, {"field": "performance", "per_item": "2"}, facts)
    assert "未提取到业绩清单" in r1.detail
    assert r1.unresolved == "no_facts"

    cert_crit = {"name": "投标人认证及资质情况", "description": "", "max_score": Decimal("5")}
    r2 = score_count(cert_crit, {"field": "certificates", "per_item": "1"}, facts)
    assert "未提取到证书清单" in r2.detail
    assert r2.unresolved == "no_facts"


# ---- 进度与排序 --------------------------------------------------------------

def test_progress_counts_and_ordering():
    """疑问按严重度排序，进度把已处置/待处理分清。"""
    crit = _Crit()
    rows = [
        (_Score(review_status="human_confirmed"), crit, _Bidder()),          # 已处置
        (_Score(), crit, _Bidder()),                                          # 低
        (_Score(final_score=None, review_status="conflict_flagged"), crit, _Bidder()),  # 中(no_score)
    ]
    questions, progress, _by_crit = _build_open_questions(rows, 1)
    assert progress["total"] == 3
    assert progress["settled"] == 1
    assert progress["open"] == 2
    severities = [q["severity"] for q in questions]
    assert severities == sorted(severities, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s])


def test_all_settled_means_no_questions():
    rows = [(_Score(review_status="human_confirmed"), _Crit(), _Bidder())]
    questions, progress, _by_crit = _build_open_questions(rows, 1)
    assert questions == []
    assert progress["open"] == 0


# ---- 计数字段推断 ------------------------------------------------------------

def test_infer_count_field_company_level_only():
    """只认公司级的两类；人员级不猜——facts 模型里没有个人业绩桶，
    猜错会产出静默错误的数字，比可见的 0 分更危险。"""
    assert _infer_count_field({"name": "投标人业绩", "description": "合同金额达到60万元"}) == "performance"
    assert _infer_count_field({"name": "投标人认证及资质情况", "description": "ISO9001"}) == "certificates"
    # 人员级：不推断
    assert _infer_count_field({"name": "项目负责人经验", "description": "每个案例得2分"}) is None
    assert _infer_count_field({"name": "项目驻场运维工程师经验", "description": ""}) is None
    assert _infer_count_field({"name": "增值服务", "description": "每项1分"}) is None

