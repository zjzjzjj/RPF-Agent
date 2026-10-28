# -*- coding: utf-8 -*-
"""主观聚合链可复算与 provisional 语义（2026-10-27 训练集校准，PR-3）。

不变式 I1：final_score 可由 ai_raw_json.aggregation_v2 的 inputs 按 rule 重算。
清零机制修复：证据不足但给分（provisional）不再被丢弃/清零。
"""
from __future__ import annotations

from decimal import Decimal

from app.engine.subjective import (
    SubjectiveSuggestion,
    _trimmed_mean,
    suggest_score_committee,
    suggest_score_hierarchical,
)


class _StubGateway:
    """按脚本返回结果的假网关（suggest_score 内部走 chat_json——这里直接
    monkeypatch suggest_score 层更稳，故本类仅占位）。"""


def test_trimmed_mean_keeps_median_for_three():
    assert _trimmed_mean([Decimal("2"), Decimal("0"), Decimal("0")]) == Decimal("0.00")
    assert _trimmed_mean([Decimal("1"), Decimal("2"), Decimal("3")]) == Decimal("2.00")
    assert _trimmed_mean([Decimal("0.5"), Decimal("0.5"), Decimal("0.5")]) == Decimal("0.50")


def test_provisional_participates_in_committee(monkeypatch):
    """给分但证据不足（provisional）的评委分参与去尾聚合，不被清零。

    测试1 实证：龙即 c54 三个 0.5（insufficient）被「均未给出有效分」清零。
    """
    import app.engine.subjective as subj

    def _fake_suggest(gateway, criterion, bid_text, **kw):  # noqa: ANN001
        # 三个评委都"证据不足但给分"（status=need_human + score）
        return SubjectiveSuggestion(
            status="need_human", score=Decimal("0.5"),
            warnings=["insufficient_evidence"],
        )

    monkeypatch.setattr(subj, "suggest_score", _fake_suggest)
    s, committee = suggest_score_committee(
        None, {"name": "c54", "max_score": Decimal("4")}, "文本", size=3)
    # 旧语义：valid 只收 ai_suggested → 0 个 → need_human 整项转人工
    # 新语义：provisional 参与 → 去尾(0.5,0.5,0.5)=0.5
    assert s.status == "ai_suggested"
    assert s.score == Decimal("0.50")
    assert all(row.get("provisional") for row in committee)
    # 聚合算式落档（I1 可复算）
    assert s.aggregation is not None
    assert s.aggregation["rule"] == "trimmed_mean_drop_high_low"
    assert len(s.aggregation["inputs"]) == 3


def test_committee_aggregation_recomputable(monkeypatch):
    """aggregation_v2 的 inputs 可重算出 final（I1）。"""
    import app.engine.subjective as subj

    scores = [Decimal("1"), Decimal("3"), Decimal("5"), Decimal("4"), Decimal("2")]

    def _fake_suggest(gateway, criterion, bid_text, **kw):  # noqa: ANN001
        return SubjectiveSuggestion(status="ai_suggested", score=0, warnings=[])

    calls = {"n": 0}

    def _fake_suggest2(gateway, criterion, bid_text, **kw):  # noqa: ANN001
        v = scores[calls["n"]]
        calls["n"] += 1
        return SubjectiveSuggestion(status="ai_suggested", score=v, warnings=[])

    monkeypatch.setattr(subj, "suggest_score", _fake_suggest2)
    s, committee = suggest_score_committee(
        None, {"name": "x", "max_score": Decimal("10")}, "文本", size=5)
    # 去尾：去 5 去 1 → (2+3+4)/3 = 3.00
    assert s.score == Decimal("3.00")
    # 重算
    agg = s.aggregation
    assert agg["rule"] == "trimmed_mean_drop_high_low"
    vals = sorted(Decimal(inp["score"]) for inp in agg["inputs"])
    recomputed = _trimmed_mean(vals)
    assert recomputed == s.score


def test_hierarchical_equal_mean_and_recomputable(monkeypatch):
    """无子项：簇等权平均（删相关性加权）；inputs 可重算。"""
    import app.engine.subjective as subj

    cluster_scores = [Decimal("5.0"), Decimal("3.0")]

    class _Cl:
        def __init__(self, i):
            self.d = {"ids": [i], "chars": 100, "score": 0.9 if i == 0 else 0.1}

        def get(self, k, default=None):
            return self.d.get(k, default)

        def __getitem__(self, k):
            return self.d[k]

    def _fake_split(text):
        return [("块0", "内容0"), ("块1", "内容1")]

    def _fake_emap(chunks, criterion, **_kw):
        return [_Cl(0), _Cl(1)], "query"

    calls = {"n": 0}

    def _fake_suggest(gateway, criterion, bid_text, **kw):  # noqa: ANN001
        v = cluster_scores[calls["n"]]
        calls["n"] += 1
        return SubjectiveSuggestion(status="ai_suggested", score=v, evidence=[])

    monkeypatch.setattr(subj, "suggest_score", _fake_suggest)
    import app.engine.selection as sel
    monkeypatch.setattr(sel, "split_text_chunks", _fake_split, raising=False)
    monkeypatch.setattr(sel, "build_evidence_map", _fake_emap, raising=False)
    # suggest_score_hierarchical 内部 from app.engine.selection import ... —— 需 patch 模块属性
    s = suggest_score_hierarchical(
        None, {"name": "y", "max_score": Decimal("10"), "description": ""}, "全文")
    assert s is not None
    merged, committee = s
    # 等权：(5.0+3.0)/2 = 4.00（旧相关性加权会是 5*0.9+3*0.1≈4.8）
    assert merged.score == Decimal("4.00")
    assert merged.aggregation["rule"] == "cluster_equal_mean"
    vals = [Decimal(inp["score"]) for inp in merged.aggregation["inputs"]]
    assert (sum(vals) / len(vals)).quantize(Decimal("0.01")) == merged.score


def test_hierarchical_message_counts_evaluated_only(monkeypatch):
    """「均未给出有效分」只数实际评过的簇（不是全部候选簇）。"""
    import app.engine.subjective as subj

    class _Cl:
        def __init__(self, i):
            self.d = {"ids": [i], "chars": 10, "score": 0.5}

        def get(self, k, default=None):
            return self.d.get(k, default)

        def __getitem__(self, k):
            return self.d[k]

    def _fake_split(text):
        return [(f"c{i}", f"t{i}") for i in range(10)]

    def _fake_emap(chunks, criterion, **_kw):
        return [_Cl(i) for i in range(10)], "q"

    def _fake_suggest(gateway, criterion, bid_text, **kw):  # noqa: ANN001
        return SubjectiveSuggestion(status="need_human", score=None, warnings=[])

    monkeypatch.setattr(subj, "suggest_score", _fake_suggest)
    import app.engine.selection as sel
    monkeypatch.setattr(sel, "split_text_chunks", _fake_split, raising=False)
    monkeypatch.setattr(sel, "build_evidence_map", _fake_emap, raising=False)
    result = suggest_score_hierarchical(
        None, {"name": "z", "max_score": Decimal("10"), "description": ""}, "全文", k=3)
    assert result is not None
    merged, _committee = result
    assert merged.status == "need_human"
    # 10 个候选簇但只评 3 个 → 消息应是 3
    assert "3 个证据簇均未给出有效分" in merged.warnings[0]
