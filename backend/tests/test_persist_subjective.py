# -*- coding: utf-8 -*-
"""_persist_subjective 落库语义（W1-D，2026-10-29）。

2026-10-29 run 60 实证的两个静默缺陷：
① 分层评审的 provisional 聚合分（suggestion.score 非 None 但 status 非
   ai_suggested，如 insufficient_evidence 带分）被"非 ai_suggested 一律最低档
   兜底"丢弃——有效簇的分数被地板分静默清零；
② parse_failed 兜底 0 分在部分路径 machine_status="pass"，把"模型输出坏了"
   伪装成"投标人做得差"（2026-10-15 诚道运维方案 0 分同型）。

语义统一（全自动模式）：
- suggestion.score 非 None（含 provisional/分层聚合/insufficient 带分）→
  直接进 final_score；status != ai_suggested 或 doubt → need_human +
  conflict_flagged（分照进，复核态可见）；
- suggestion.score is None（parse_failed/llm_failed/全簇失败/insufficient 无分）
  → 最低档 fallback + need_human + conflict_flagged，警告明示"兜底分非评审结论"。
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace


def _seed(s) -> None:
    """落最小可写库：Project/RulePackage/Criterion/Bidder/EvaluationRun。

    levels 不是 Criterion 表列——档位来自评分包快照 dict（_CRIT），
    _persist_subjective 从 criterion.get("levels") 读最低档兜底。
    """
    from app.db.models import Bidder, Criterion, EvaluationRun, Project, RulePackage

    s.add(Project(id="PRJ-PSUB", name="主观落库语义"))
    s.flush()
    s.add(RulePackage(id=600, project_id="PRJ-PSUB", version=1, status="frozen"))
    s.flush()
    s.add(Criterion(
        id=9100, package_id=600, code="S1.1", name="运维方案",
        category="technical", score_type="subjective", max_score=5,
    ))
    s.add(Bidder(id=910, project_id="PRJ-PSUB", name="某公司"))
    s.add(EvaluationRun(id=9101, project_id="PRJ-PSUB", package_id=600,
                        package_version=1, status="running"))
    s.flush()


def _suggestion(**kw) -> SimpleNamespace:
    """模拟 SubjectiveSuggestion（含 doubt 可选字段——用 getattr 消费，
    不依赖 subjective.py 的并发改动落地顺序）。"""
    base = dict(
        status="ai_suggested", score=Decimal("4.0"), level_label="良",
        reasoning="按细则", confidence=0.8, evidence=[], warnings=[],
        artifacts_check=[], sub_scores=[], quote_gate_executed=True,
        aggregation=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


_CRIT = {"package_id": 600, "code": "S1.1", "levels": [
    {"label": "差", "min": 0, "max": 1.5},
    {"label": "中", "min": 1.6, "max": 3.5},
    {"label": "良", "min": 3.6, "max": 4.5},
    {"label": "优", "min": 4.6, "max": 5},
]}


def _persist(app_env, monkeypatch, suggestion):  # noqa: ANN001
    """在隔离库上以全自动模式跑一次 _persist_subjective，返回 ScoreRecord 快照。"""
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    from app.db.session import session_scope
    from app.db.models import ScoreRecord
    from app.services.scoring_service import _persist_subjective

    with session_scope() as s:
        _seed(s)
    stats = {"citations_total": 0, "citations_verified": 0}
    _persist_subjective(9101, 910, dict(_CRIT), suggestion, None, stats)
    with session_scope() as s:
        row = s.query(ScoreRecord).filter_by(run_id=9101, bidder_id=910).one()
        return {
            "final_score": row.final_score,
            "machine_status": row.machine_status,
            "review_status": row.review_status,
            "warnings": row.warnings_json,
        }


def test_insufficient_with_score_not_dropped_to_floor(app_env, monkeypatch) -> None:  # noqa: ANN001
    """insufficient_evidence 带 provisional 分 → final=该分（不落最低档地板）。"""
    got = _persist(app_env, monkeypatch, _suggestion(
        status="insufficient_evidence", score=Decimal("3.2"),
        warnings=["证据薄弱，按 provisional 聚合分给出"],
    ))
    assert got["final_score"] == Decimal("3.2"), "provisional 聚合分不得被地板分丢弃"
    assert got["machine_status"] == "need_human"
    assert got["review_status"] == "conflict_flagged"
    assert any("建议人工复核" in str(w.get("message")) for w in got["warnings"])


def test_ai_suggested_normal_path_unchanged(app_env, monkeypatch) -> None:  # noqa: ANN001
    """status=ai_suggested 正常路径：pass + auto_confirmed，分数直接生效。"""
    got = _persist(app_env, monkeypatch, _suggestion(status="ai_suggested", score=Decimal("4.0")))
    assert got["final_score"] == Decimal("4.0")
    assert got["machine_status"] == "pass"
    assert got["review_status"] == "auto_confirmed"


def test_score_none_fallback_lowest_level(app_env, monkeypatch) -> None:  # noqa: ANN001
    """score=None（llm_failed/parse_failed/全簇失败）→ 最低档兜底 + need_human
    + conflict_flagged，警告明示兜底分非评审结论。"""
    got = _persist(app_env, monkeypatch, _suggestion(
        status="need_human", score=None, warnings=["llm_failed"],
    ))
    assert got["final_score"] == Decimal("0"), "最低档兜底（levels min=0）"
    assert got["machine_status"] == "need_human"
    assert got["review_status"] == "conflict_flagged"
    assert any("兜底分非评审结论" in str(w.get("message")) for w in got["warnings"])


def test_doubt_flag_forces_review_even_when_ai_suggested(app_env, monkeypatch) -> None:  # noqa: ANN001
    """聚合层 doubt=True（all-abstain/闸门未核验）→ 分进总分但强制复核态。"""
    got = _persist(app_env, monkeypatch, _suggestion(
        status="ai_suggested", score=Decimal("2.5"), doubt=True,
    ))
    assert got["final_score"] == Decimal("2.5"), "doubt 不丢分，只标复核"
    assert got["machine_status"] == "need_human"
    assert got["review_status"] == "conflict_flagged"
