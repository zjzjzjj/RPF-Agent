# -*- coding: utf-8 -*-
"""veto/match 的 LLM 裁决逐字引文落库（2026-11-05，补 evidence 死字段）。

背景：auto_judge 裁决抽到的逐字 quote 此前只进结果 dict 的 "evidence" 键，
落库循环只取 message——quote 全库无消费者（死字段），判定类条目恒 0 引文。

口径镜像 _persist_subjective 证据段（quote <8 字符跳过、每格 ≤5 条、
create_citation 立即核验），差异：重跑**先删后建**（限本 role + created_by='llm'），
人工终态行（human_confirmed/overridden）不动。纯机器判定（run_check）不产
引文——过程即证据，evidence_status 保持 none。
"""
from __future__ import annotations


def _seed(s) -> None:
    from app.db.models import Bidder, Criterion, EvaluationRun, Project, RulePackage

    s.add(Project(id="PRJ-VCIT", name="裁决引文落库"))
    s.flush()
    s.add(RulePackage(id=610, project_id="PRJ-VCIT", version=1, status="frozen"))
    s.flush()
    s.add(Criterion(
        id=9200, package_id=610, code="V1", name="投标人自投标截止日前三年工商变更信息记录",
        category="technical", score_type="veto",
    ))
    s.add(Criterion(
        id=9201, package_id=610, code="M1", name="投标文件密封及份数",
        category="technical", score_type="match",
    ))
    s.add(Bidder(id=920, project_id="PRJ-VCIT", name="某公司"))
    s.add(EvaluationRun(id=9201, project_id="PRJ-VCIT", package_id=610,
                        package_version=1, status="running"))
    s.flush()


def _make_row(code: str = "V1", score_type: str = "veto", review_status: str = "auto_confirmed"):
    from app.services.scoring_service import _upsert_score

    return _upsert_score(
        9201, 920, {"package_id": 610, "code": code, "score_type": score_type},
        machine_status="pass", review_status=review_status,
    )


def _patch_verify(monkeypatch, *, verified: bool, method: str | None):  # noqa: ANN001
    """替换 verifier.verify_citation（verified 唯一写入点）的核验行为。

    真实 locate 依赖文档文件，单测里用假核验控制 verified/verify_method，
    以覆盖 evidence_status 三态回写口径。
    """
    from app.db.models import EvidenceCitation
    from app.db.session import session_scope
    from app.evidence import verifier

    def fake_verify(citation_id: int):
        with session_scope() as s:
            cit = s.get(EvidenceCitation, citation_id)
            cit.verified = verified
            cit.verify_method = method
            s.flush()
            s.expunge(cit)
            return cit

    monkeypatch.setattr(verifier, "verify_citation", fake_verify)


def _citations(row_id: int):
    from app.db.models import EvidenceCitation
    from app.db.session import session_scope

    with session_scope() as s:
        return [
            {
                "quote": c.quote, "role": c.citation_role, "created_by": c.created_by,
                "page_no": c.page_no, "verified": c.verified,
            }
            for c in s.query(EvidenceCitation).filter_by(score_record_id=row_id).all()
        ]


def _evidence_status(row_id: int) -> str | None:
    from app.db.models import ScoreRecord
    from app.db.session import session_scope

    with session_scope() as s:
        return s.get(ScoreRecord, row_id).evidence_status


# ---------------------------------------------------------------------------
# 1. LLM 裁决 quote 落库
# ---------------------------------------------------------------------------
def test_llm_adjudication_quotes_persisted(app_env, monkeypatch):  # noqa: ANN001
    """LLM 裁决逐字引文 → EvidenceCitation（role=veto, created_by=llm），
    page_hint 透传、evidence_status 回写 verified、stats 累加。"""
    from app.db.session import session_scope
    from app.services.scoring_service import _persist_verdict_citations

    _patch_verify(monkeypatch, verified=True, method="exact")
    with session_scope() as s:
        _seed(s)
    row = _make_row()
    stats = {"citations_total": 0, "citations_verified": 0}
    _persist_verdict_citations(
        row,
        {
            "verdict": "pass",
            "message": "自动裁决（通过）：已提供工商变更信息记录",
            "evidence_items": [
                {"quote": "投标人自投标截止日前三年工商变更信息记录", "page_hint": 3},
            ],
            "verdict_source": "llm",
        },
        main_doc_id=None, role="veto", stats=stats,
    )
    cits = _citations(row.id)
    assert len(cits) == 1
    assert cits[0]["role"] == "veto"
    assert cits[0]["created_by"] == "llm"
    assert cits[0]["page_no"] == 3
    assert cits[0]["verified"] is True
    assert _evidence_status(row.id) == "verified"
    assert stats == {"citations_total": 1, "citations_verified": 1}


# ---------------------------------------------------------------------------
# 2. 过滤与封顶
# ---------------------------------------------------------------------------
def test_short_quotes_filtered(app_env, monkeypatch):  # noqa: ANN001
    """quote <8 字符跳过（与 _persist_subjective 落库门槛逐字一致）。"""
    from app.db.session import session_scope
    from app.services.scoring_service import _persist_verdict_citations

    _patch_verify(monkeypatch, verified=False, method="prefix_fallback")
    with session_scope() as s:
        _seed(s)
    row = _make_row()
    stats = {"citations_total": 0, "citations_verified": 0}
    _persist_verdict_citations(
        row,
        {"evidence_items": [
            {"quote": "四个字"},          # <8 → 跳过
            {"quote": "  六个字符  "},     # strip 后 6 → 跳过
            {"quote": "这条引文足够长可以落库一"},
            {"quote": "这条引文足够长可以落库二"},
            {"quote": "这条引文足够长可以落库三"},
            {"quote": "这条引文足够长可以落库四"},  # 第 6 条：[:5] 封顶外
        ], "verdict_source": "llm"},
        main_doc_id=None, role="veto", stats=stats,
    )
    cits = _citations(row.id)
    assert len(cits) == 3, "前 5 条中 2 条 <8 字符被跳过，第 6 条被封顶"
    assert stats["citations_total"] == 3


def test_items_capped_at_five(app_env, monkeypatch):  # noqa: ANN001
    from app.db.session import session_scope
    from app.services.scoring_service import _persist_verdict_citations

    _patch_verify(monkeypatch, verified=False, method="prefix_fallback")
    with session_scope() as s:
        _seed(s)
    row = _make_row()
    stats = {"citations_total": 0, "citations_verified": 0}
    _persist_verdict_citations(
        row,
        {"evidence_items": [{"quote": f"第{i}条引文的逐字原文内容"} for i in range(1, 9)],
         "verdict_source": "llm"},
        main_doc_id=None, role="veto", stats=stats,
    )
    assert len(_citations(row.id)) == 5


# ---------------------------------------------------------------------------
# 3. 纯机器判定不落库（c3 场景）
# ---------------------------------------------------------------------------
def test_machine_verdict_creates_no_citations(app_env, monkeypatch):  # noqa: ANN001
    """evidence_items=[] 的确定性判定 → 0 引文、evidence_status 保持 none。
    （run_check 判定如 c3 条目不强行造引文——过程即证据。）"""
    from app.db.session import session_scope
    from app.services.scoring_service import _persist_verdict_citations

    with session_scope() as s:
        _seed(s)
    row = _make_row()
    stats = {"citations_total": 0, "citations_verified": 0}
    _persist_verdict_citations(
        row,
        {"verdict": "pass", "message": "已找到「工商变更信息记录」相关内容",
         "evidence_items": [], "verdict_source": "machine"},
        main_doc_id=None, role="veto", stats=stats,
    )
    assert _citations(row.id) == []
    assert _evidence_status(row.id) == "none"
    assert stats == {"citations_total": 0, "citations_verified": 0}


# ---------------------------------------------------------------------------
# 4. match 角色
# ---------------------------------------------------------------------------
def test_match_role(app_env, monkeypatch):  # noqa: ANN001
    from app.db.session import session_scope
    from app.services.scoring_service import _persist_verdict_citations

    _patch_verify(monkeypatch, verified=False, method="prefix_fallback")
    with session_scope() as s:
        _seed(s)
    row = _make_row(code="M1", score_type="match")
    stats = {"citations_total": 0, "citations_verified": 0}
    _persist_verdict_citations(
        row,
        {"evidence_items": [{"quote": "投标文件密封及份数符合要求的逐字原文"}],
         "verdict_source": "llm"},
        main_doc_id=None, role="match", stats=stats,
    )
    cits = _citations(row.id)
    assert len(cits) == 1
    assert cits[0]["role"] == "match"
    assert _evidence_status(row.id) == "unverified"


# ---------------------------------------------------------------------------
# 5. 幂等 / 重跑 / 守卫
# ---------------------------------------------------------------------------
def test_rerun_replaces_llm_citations_only(app_env, monkeypatch):  # noqa: ANN001
    """先删后建（限本 role + created_by='llm'）：重跑不翻倍、旧 quote 被替换；
    人工补录（human）与其它角色（score）引文存活。"""
    from app.db.models import EvidenceCitation
    from app.db.session import session_scope
    from app.evidence.verifier import create_citation
    from app.services.scoring_service import _persist_verdict_citations

    _patch_verify(monkeypatch, verified=False, method="prefix_fallback")
    with session_scope() as s:
        _seed(s)
    row = _make_row()
    # 预置：人工补录的 veto 引文 + 一条 score 角色引文（都不得被清）
    create_citation(None, "人工补录的引文不得被机器重跑清掉", role="veto",
                    score_record_id=row.id, created_by="human")
    create_citation(None, "其它角色的引文同样不受影响", role="score",
                    score_record_id=row.id, created_by="llm")
    stats = {"citations_total": 0, "citations_verified": 0}
    _persist_verdict_citations(
        row,
        {"evidence_items": [{"quote": "第一次落库的旧引文内容"}], "verdict_source": "llm"},
        main_doc_id=None, role="veto", stats=stats,
    )
    assert len(_citations(row.id)) == 3
    _persist_verdict_citations(
        row,
        {"evidence_items": [{"quote": "重跑后替换的新引文内容"}], "verdict_source": "llm"},
        main_doc_id=None, role="veto", stats=stats,
    )
    cits = _citations(row.id)
    quotes = {c["quote"] for c in cits}
    assert len(cits) == 3, "LLM veto 引文替换而非累加"
    assert "重跑后替换的新引文内容" in quotes
    assert "第一次落库的旧引文内容" not in quotes
    assert "人工补录的引文不得被机器重跑清掉" in quotes
    assert "其它角色的引文同样不受影响" in quotes
    with session_scope() as s:
        n = s.query(EvidenceCitation).filter_by(
            score_record_id=row.id, citation_role="veto", created_by="llm").count()
    assert n == 1


def test_human_terminal_row_untouched(app_env, monkeypatch):  # noqa: ANN001
    """人工已确认/改分的行：引文与 evidence_status 都不动。"""
    from app.db.session import session_scope
    from app.evidence.verifier import create_citation
    from app.services.scoring_service import _persist_verdict_citations

    _patch_verify(monkeypatch, verified=False, method="prefix_fallback")
    with session_scope() as s:
        _seed(s)
    row = _make_row(review_status="human_confirmed")
    create_citation(None, "人工确认前留下的旧引文内容", role="veto",
                    score_record_id=row.id, created_by="llm")
    stats = {"citations_total": 0, "citations_verified": 0}
    _persist_verdict_citations(
        row,
        {"evidence_items": [{"quote": "重跑想写入的新引文内容"}], "verdict_source": "llm"},
        main_doc_id=None, role="veto", stats=stats,
    )
    cits = _citations(row.id)
    assert len(cits) == 1
    assert cits[0]["quote"] == "人工确认前留下的旧引文内容"
    assert stats == {"citations_total": 0, "citations_verified": 0}


# ---------------------------------------------------------------------------
# 6. 单元格 dict 契约：evidence_items + verdict_source
# ---------------------------------------------------------------------------
def test_eval_veto_llm_branch_carries_items(monkeypatch):  # noqa: ANN001
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    monkeypatch.setenv("BID_EVAL_VETO_LLM", "1")
    from app.engine import auto_judge
    from app.engine.facts import extract_bid_facts
    from app.engine.models import PrimitiveResult
    from app.services import scoring_service as ss

    def fake_refine(gateway, c, facts, text, machine_result, **kw):  # noqa: ANN001
        return PrimitiveResult(
            "pass", "自动裁决（通过）：有对应材料",
            evidence_quote="首条逐字引文内容",
            evidence_items=[{"quote": "首条逐字引文内容"}, {"quote": "次条逐字引文内容"}],
        )

    monkeypatch.setattr(auto_judge, "refine_with_llm", fake_refine)
    c = {"code": "V1", "name": "工商变更信息记录", "check_type": "human_only", "params": {}}
    facts = extract_bid_facts("投标文件正文。")
    out = ss._eval_veto(c, facts, text=facts.text, gateway=object())
    assert out["verdict"] == "pass"
    assert out["verdict_source"] == "llm"
    assert [e["quote"] for e in out["evidence_items"]] == ["首条逐字引文内容", "次条逐字引文内容"]


def test_eval_veto_machine_path_carries_no_items(monkeypatch):  # noqa: ANN001
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    from app.engine.facts import extract_bid_facts
    from app.services import scoring_service as ss

    c = {"code": "V1", "name": "工商变更信息记录", "check_type": "document_present",
         "params": {"document": "工商变更信息记录"}}
    facts = extract_bid_facts("投标文件附工商变更信息记录一份。")
    out = ss._eval_veto(c, facts, text=facts.text, gateway=object())
    assert out["verdict"] == "pass"
    assert "已找到" in out["message"]
    assert out["verdict_source"] == "machine"
    assert out["evidence_items"] == []


def test_batch_cell_from_judgement_carries_items():  # noqa: ANN001
    from app.engine.auto_judge import BatchJudgementItem
    from app.services.scoring_service import _batch_cell_from_judgement

    j = BatchJudgementItem(
        code="V1", verdict="pass", reasoning="有材料",
        evidence=[{"quote": "逐字引文内容甲"}, {"quote": "逐字引文内容乙"}],
    )
    out = _batch_cell_from_judgement(j)
    assert out["verdict"] == "pass"
    assert out["verdict_source"] == "llm"
    assert [e["quote"] for e in out["evidence_items"]] == ["逐字引文内容甲", "逐字引文内容乙"]


def test_verdict_warning_entry_defaults_machine():  # noqa: ANN001
    from app.services.scoring_service import _verdict_warning_entry

    assert _verdict_warning_entry({"verdict": "pass", "message": "已找到"}) == {
        "message": "已找到", "verdict_source": "machine",
    }
    assert _verdict_warning_entry({"verdict": "pass"}) == {"verdict_source": "machine"}
    entry = _verdict_warning_entry(
        {"verdict": "need_human", "message": "AI 存疑", "verdict_source": "llm"})
    assert entry["verdict_source"] == "llm"


# ---------------------------------------------------------------------------
# 7. refine_with_llm 多条 quote
# ---------------------------------------------------------------------------
def test_refine_with_llm_keeps_multiple_quotes():  # noqa: ANN001
    """refine_with_llm 不再只留首条：evidence_items 全量（≤5），evidence_quote
    仍是首条（兼容旧读者）。"""
    from app.engine.auto_judge import AutoJudgement, refine_with_llm
    from app.engine.facts import extract_bid_facts
    from app.engine.models import PrimitiveResult

    class G:
        def chat_json(self, system, user, model, data_class="synthetic"):  # noqa: ANN001
            return AutoJudgement(
                verdict="pass", reasoning="材料齐全",
                evidence=[{"quote": "引文一内容示例"}, {"quote": "引文二内容示例"},
                          {"quote": "引文三内容示例"}],
                confidence=0.9,
            ), {}

    facts = extract_bid_facts("投标文件正文。")
    r = refine_with_llm(
        G(), {"code": "V1", "name": "工商变更信息记录", "description": ""},
        facts, "投标文件正文。", PrimitiveResult("unknown", "human_only", need_human=True),
    )
    assert r.evidence_quote == "引文一内容示例"
    assert [e["quote"] for e in r.evidence_items] == [
        "引文一内容示例", "引文二内容示例", "引文三内容示例",
    ]
