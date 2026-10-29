# -*- coding: utf-8 -*-
"""弃权语义聚合（2026-10-29）：缺席者不投票。

run 60 实证根因：分层评审把"弃权簇"（无证据 0 票）计入平均——
(0+5.6+0)/3=1.87、(1.8+0+0)/3=0.60，技术分被簇数整除、普遍压到满分 0-30%。

三态（子项×证据簇条目）：
- abstain 弃权：合成缺失 / score=0 且无引文 / 引文闸门击杀 → 退出均值不进分母
- real_zero 真实 0：score=0 且引文非空且核验通过 → 计入均值（0 票）
- vote 有效票：score>0（含 provisional 正分）→ 计入均值

不变式 I1：final_score 可由 aggregation_v2 重算（新格式 + 旧格式无 state 兼容）。
"""
from __future__ import annotations

from decimal import Decimal

from app.engine.subjective import (
    SubjectiveSuggestion,
    apply_quote_gate,
    suggest_score_hierarchical,
)


def _cluster(score, *, sub_scores=None, status="ai_suggested", evidence=None, gate=None):
    """构造单簇评审建议（evidence 传 [] 表示"无任何证据"）。"""
    return SubjectiveSuggestion(
        status=status,
        score=score,
        sub_scores=sub_scores if sub_scores is not None else [],
        evidence=(
            [{"quote": "投标文件中相关证明内容原文段落", "page_hint": 3}]
            if evidence is None
            else evidence
        ),
        quote_gate_executed=gate,
        warnings=[],
    )


def _run_hier(monkeypatch, criterion, clusters, verify_quote=None):  # noqa: ANN001
    """monkeypatch suggest_score 与证据地图，驱动 suggest_score_hierarchical。"""
    import app.engine.selection as sel
    import app.engine.subjective as subj

    class _Cl:
        def __init__(self, i):
            self.d = {"ids": [i], "chars": 100, "score": 0.5}

        def get(self, k, default=None):
            return self.d.get(k, default)

        def __getitem__(self, k):
            return self.d[k]

    n = len(clusters)
    calls = {"i": 0}

    def _fake_suggest(gateway, c, bid_text, **kw):  # noqa: ANN001
        s = clusters[calls["i"]]
        calls["i"] += 1
        return s

    monkeypatch.setattr(subj, "suggest_score", _fake_suggest)
    monkeypatch.setattr(
        sel, "split_text_chunks",
        lambda text, chunk_size=2000: [(i, f"块{i}") for i in range(n)],
        raising=False,
    )
    monkeypatch.setattr(
        sel, "build_evidence_map",
        lambda chunks, criterion, **_kw: ([_Cl(i) for i in range(n)], "q"),
        raising=False,
    )
    return suggest_score_hierarchical(
        None, criterion, "全文", k=n, verify_quote=verify_quote
    )


# ---------------------------------------------------------------------------
# c49/b31 数值复刻：votes [1.8(vote), missing(abstain), missing(abstain)]
# ---------------------------------------------------------------------------
def test_c49_missing_abstains_exit_mean(monkeypatch):  # noqa: ANN001
    crit = {
        "name": "运维服务内容",
        "max_score": "5",
        "description": "系统主要功能：订单管理（0-5 分）",
        "levels": [],
    }
    clusters = [
        _cluster(Decimal("1.8"), sub_scores=[
            {"name": "订单管理", "score": "1.8", "max": "5",
             "quote": "订单管理模块支持全生命周期管理"},
        ]),
        _cluster(Decimal("0"), sub_scores=[
            {"name": "订单管理", "score": "0", "max": "5",
             "quote": "", "evidence_state": "missing"},
        ]),
        _cluster(Decimal("0"), sub_scores=[
            {"name": "订单管理", "score": "0", "max": "5",
             "quote": "", "evidence_state": "missing"},
        ]),
    ]
    merged, _committee = _run_hier(monkeypatch, crit, clusters)
    assert merged.status == "ai_suggested"
    assert merged.score == Decimal("1.80")  # 旧语义 (1.8+0+0)/3=0.60
    agg = merged.aggregation
    assert agg["rule"] == "subitem_cluster_mean"
    votes = agg["inputs"][0]["votes"]
    assert [v["state"] for v in votes] == ["vote", "abstain", "abstain"]
    # 可读算式记录弃权簇
    assert "弃权" in agg["arithmetic"]


# ---------------------------------------------------------------------------
# c52/b34 数值复刻：equal_mean [0弃权, 5.6, 0弃权] → 5.60 而非 1.87
# ---------------------------------------------------------------------------
def test_c52_equal_mean_abstains_exit_denominator(monkeypatch):  # noqa: ANN001
    crit = {"name": "实施方案", "max_score": "6", "description": "", "levels": []}
    clusters = [
        _cluster(Decimal("0"), evidence=[]),
        _cluster(Decimal("5.6")),
        _cluster(Decimal("0"), evidence=[]),
    ]
    merged, _committee = _run_hier(monkeypatch, crit, clusters)
    assert merged.score == Decimal("5.60")  # 旧语义 (0+5.6+0)/3=1.87
    agg = merged.aggregation
    assert agg["rule"] == "cluster_equal_mean"
    states = [i["state"] for i in agg["inputs"]]
    assert states == ["abstain", "vote", "abstain"]  # 弃权簇保留记录
    counted = [Decimal(i["score"]) for i in agg["inputs"] if i["state"] != "abstain"]
    assert (sum(counted) / Decimal(len(counted))).quantize(Decimal("0.01")) == merged.score


def test_real_zero_restatement_quote_abstains(monkeypatch):  # noqa: ANN001
    """0 分引文是细则复述（点对点应答表照抄）→ 弃权，不计真实 0 票。

    2026-10-29 审查 P2：real_zero 曾被应答表复述骗入均值——复述文本在标书
    里逐字可定位，但不构成"未提供"的证明（_sanitize_case 同反模式镜像）。
    """
    desc = "订单管理模块提供订单全生命周期管理功能，含状态同步与异常回执。"
    crit = {
        "name": "订单管理",
        "max_score": "5",
        "description": f"订单管理（0-5 分）：{desc}",
        "levels": [],
    }
    clusters = [
        _cluster(Decimal("5.6"), sub_scores=[
            {"name": "订单管理", "score": "5.6", "max": "5",
             "quote": "订单管理模块已实现全生命周期管理，测试环境实测通过"},
        ]),
        _cluster(Decimal("0"), sub_scores=[
            {"name": "订单管理", "score": "0", "max": "5", "quote": desc},  # 复述
        ]),
    ]
    merged, _committee = _run_hier(
        monkeypatch, crit, clusters, verify_quote=lambda q: True
    )
    # 旧语义：复述引文可定位 → real_zero → (5+0)/2=2.50（5.6 被子项封顶 5）
    assert merged.score == Decimal("5.00")
    assert merged.aggregation["rule"] == "subitem_cluster_mean"
    votes = merged.aggregation["inputs"][0]["votes"]
    assert [v["state"] for v in votes] == ["vote", "abstain"]


def test_all_subitem_entries_gated_fallback_keeps_doubt(monkeypatch):  # noqa: ANN001
    """全部子项条目弃权回退 equal_mean 时保留 doubt（0 分不得伪装成评审结论）。

    2026-10-29 审查 P2：闸门全击杀/子项全缺证据时回退簇平均曾丢失 doubt，
    0 分被 auto_confirmed 直接定稿。
    """
    crit = {
        "name": "订单管理",
        "max_score": "5",
        "description": "订单管理（0-5 分）：提供订单全生命周期管理功能。",
        "levels": [],
    }
    clusters = [
        _cluster(Decimal("0"), sub_scores=[
            {"name": "订单管理", "score": "0", "max": "5", "quote": ""},
        ]),
        _cluster(Decimal("0"), sub_scores=[
            {"name": "订单管理", "score": "0", "max": "5", "quote": ""},
        ]),
    ]
    merged, _committee = _run_hier(
        monkeypatch, crit, clusters, verify_quote=lambda q: True
    )
    assert merged.score == Decimal("0.00")  # 簇级证据可定位 → real_zero 均值
    assert merged.doubt is True  # 子项全部弃权的疑问不得丢失


def test_failed_cluster_numbering_stable(monkeypatch):  # noqa: ANN001
    """失败簇（score=None）不与后继簇共享编号：inputs 用物理簇序号。

    2026-10-29 审查 P2：judge_no 只数有效簇——簇1 失败时簇2 的 judge_no 也是 1，
    inputs 曾出现两个 "cluster1"，审计里"簇1 弃权"会指向失败簇而非真实弃权簇。
    """
    crit = {"name": "实施方案", "max_score": "6", "description": "", "levels": []}
    clusters = [
        _cluster(None, status="need_human", evidence=[]),  # 簇1：LLM 失败 score=None
        _cluster(Decimal("5.6")),                          # 簇2：计票
        _cluster(Decimal("0"), evidence=[]),               # 簇3：弃权
    ]
    merged, _committee = _run_hier(monkeypatch, crit, clusters)
    assert merged.score == Decimal("5.60")
    agg = merged.aggregation
    assert [i["source"] for i in agg["inputs"]] == ["cluster1", "cluster2", "cluster3"]
    assert [i["state"] for i in agg["inputs"]] == ["abstain", "vote", "abstain"]


# ---------------------------------------------------------------------------
# real_zero 计票：[1.8 vote, 0 real_zero(verified quote)] → 0.90
# ---------------------------------------------------------------------------
def test_real_zero_counts_in_mean(monkeypatch):  # noqa: ANN001
    crit = {
        "name": "订单管理",
        "max_score": "5",
        "description": "系统主要功能：订单管理（0-5 分）",
        "levels": [],
    }
    clusters = [
        _cluster(Decimal("1.8"), sub_scores=[
            {"name": "订单管理", "score": "1.8", "quote": "订单管理模块支持管理"},
        ]),
        _cluster(Decimal("0"), sub_scores=[
            {"name": "订单管理", "score": "0", "quote": "本节未涉及订单管理功能"},
        ]),
    ]
    merged, _committee = _run_hier(monkeypatch, crit, clusters, verify_quote=lambda q: bool(q))
    assert merged.score == Decimal("0.90")  # (1.8+0)/2；真实 0 是投票
    votes = merged.aggregation["inputs"][0]["votes"]
    assert [v["state"] for v in votes] == ["vote", "real_zero"]


# ---------------------------------------------------------------------------
# all-abstain 子项 → 0 + zero_reason + warnings；整项全弃权 → need_human
# ---------------------------------------------------------------------------
def test_all_abstain_subitem_zero_with_reason(monkeypatch):  # noqa: ANN001
    crit = {
        "name": "综合评分",
        "max_score": "5",
        "description": "系统主要功能：订单管理（0-3 分）、协同办公（0-2 分）",
        "levels": [],
    }
    clusters = [
        _cluster(Decimal("2.5"), sub_scores=[
            {"name": "订单管理", "score": "2.0", "quote": "订单管理相关原文证据"},
            {"name": "协同办公", "score": "0", "quote": "", "evidence_state": "missing"},
        ]),
        _cluster(Decimal("0"), sub_scores=[
            {"name": "订单管理", "score": "0", "quote": "", "evidence_state": "missing"},
            {"name": "协同办公", "score": "0", "quote": "", "evidence_state": "missing"},
        ]),
    ]
    merged, _committee = _run_hier(monkeypatch, crit, clusters)
    assert merged.score == Decimal("2.00")
    assert merged.doubt is True
    subs = {s["name"]: s for s in merged.sub_scores}
    assert subs["订单管理"]["score"] == "2.00"
    assert subs["协同办公"]["score"] == "0"
    assert subs["协同办公"].get("zero_reason") == "all_abstain"
    assert any("协同办公" in w and "弃权" in w for w in merged.warnings)


def test_all_clusters_abstain_need_human(monkeypatch):  # noqa: ANN001
    crit = {"name": "实施方案", "max_score": "6", "description": "", "levels": []}
    clusters = [
        _cluster(Decimal("0"), evidence=[]),
        _cluster(Decimal("0"), evidence=[]),
    ]
    merged, committee = _run_hier(monkeypatch, crit, clusters)
    assert merged.status == "need_human"
    assert merged.score is None  # 0 分不得伪装成评审结论
    assert merged.doubt is True
    assert any("弃权" in w and "转人工" in w for w in merged.warnings)
    assert len(committee) == 2  # 弃权簇明细保留


# ---------------------------------------------------------------------------
# provisional 正分计票（防回退"清零机制"）
# ---------------------------------------------------------------------------
def test_provisional_positive_counts_in_mean(monkeypatch):  # noqa: ANN001
    crit = {"name": "实施方案", "max_score": "6", "description": "", "levels": []}
    clusters = [
        _cluster(Decimal("4.0")),
        _cluster(Decimal("2.0"), status="need_human"),  # provisional：给了数字但证据不足
    ]
    merged, _committee = _run_hier(monkeypatch, crit, clusters)
    assert merged.score == Decimal("3.00")  # (4+2)/2，provisional 不被清零
    states = [i["state"] for i in merged.aggregation["inputs"]]
    assert states == ["vote", "vote"]


# ---------------------------------------------------------------------------
# 闸门击杀条目退出均值 + apply_quote_gate 补标
# ---------------------------------------------------------------------------
def test_gate_killed_entry_exits_mean(monkeypatch):  # noqa: ANN001
    crit = {
        "name": "订单管理",
        "max_score": "5",
        "description": "系统主要功能：订单管理（0-5 分）",
        "levels": [],
    }
    clusters = [
        _cluster(Decimal("0"), sub_scores=[
            {"name": "订单管理", "score": "0", "quote": "定位不到的引文",
             "gate_killed": True, "pre_gate_score": "2.0"},
        ]),
        _cluster(Decimal("1.8"), sub_scores=[
            {"name": "订单管理", "score": "1.8", "quote": "订单管理模块原文证据"},
        ]),
    ]
    merged, _committee = _run_hier(monkeypatch, crit, clusters)
    assert merged.score == Decimal("1.80")  # 击杀簇不进分母（旧语义 0.90）
    votes = merged.aggregation["inputs"][0]["votes"]
    assert [v["state"] for v in votes] == ["abstain", "vote"]
    assert votes[0]["reason"] == "quote_gate_killed"


def test_apply_quote_gate_marks_zero_score_unlocatable():
    subs = [
        {"name": "甲", "score": "2", "quote": "有效引文"},
        {"name": "乙", "score": "3", "quote": "无效引文"},
        {"name": "丙", "score": "0", "quote": "定位不到的引文"},
        {"name": "丁", "score": "0", "quote": ""},  # 规则 (b) 弃权，不打 gate_killed
    ]
    total, warnings, executed = apply_quote_gate(subs, lambda q: q == "有效引文")
    assert executed is True
    assert total == Decimal("2")
    assert subs[1].get("gate_killed") is True and subs[1].get("pre_gate_score") == "3"
    # 补现缺标记：0 分+引文定位不到 → 同样打标，合并层才能与真实 0 区分
    assert subs[2].get("gate_killed") is True and subs[2].get("pre_gate_score") == "0"
    assert subs[2]["score"] == "0"
    assert "gate_killed" not in subs[3]
    assert any("乙" in w for w in warnings)


def test_doubt_set_when_gate_not_executed(monkeypatch):  # noqa: ANN001
    crit = {"name": "实施方案", "max_score": "6", "description": "", "levels": []}
    clusters = [
        _cluster(Decimal("4.0"), gate=False),  # 模型没给引文，闸门未核验
        _cluster(Decimal("5.0")),
    ]
    merged, _committee = _run_hier(monkeypatch, crit, clusters)
    assert merged.score == Decimal("4.50")
    assert merged.doubt is True


# ---------------------------------------------------------------------------
# 不变式 I1：新格式重算 + max cap
# ---------------------------------------------------------------------------
def test_i1_subitem_new_format_recomputable_with_cap(monkeypatch):  # noqa: ANN001
    crit = {
        "name": "综合",
        "max_score": "5",
        "description": "功能：订单管理（0-4 分）、协同办公（0-3 分）",
        "levels": [],
    }
    clusters = [
        _cluster(Decimal("6.5"), sub_scores=[
            {"name": "订单管理", "score": "3.5", "quote": "订单管理原文证据充足"},
            {"name": "协同办公", "score": "4.0", "quote": "协同办公原文证据充足"},  # 夹取到 3
        ]),
    ]
    merged, _committee = _run_hier(monkeypatch, crit, clusters)
    # 3.5 + min(4,3)=3.0 → 6.5 → min(·, max 5) = 5.00
    assert merged.score == Decimal("5.00")
    agg = merged.aggregation
    assert agg["rule"] == "subitem_cluster_mean"
    assert agg["max_score"] == "5"
    recomputed = min(
        sum(Decimal(i["score"]) for i in agg["inputs"]),
        Decimal(str(agg["max_score"])),
    )
    assert recomputed == merged.score


def test_eval_suite_recomputability_new_and_old_formats():
    """_recomputability_check：新格式（state/max_score）与旧格式（无 state）兼容。"""
    import importlib.util
    import json as _json
    import sqlite3
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "eval_suite_under_test",
        Path(__file__).resolve().parents[2] / "scripts" / "eval_suite.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    con = sqlite3.connect(":memory:")
    cur = con.cursor()
    cur.execute(
        """CREATE TABLE score_records (
               id INTEGER PRIMARY KEY, run_id INTEGER, bidder_id INTEGER,
               criterion_id INTEGER, final_score REAL, ai_raw_json TEXT)"""
    )
    rows = [
        # 新格式 equal_mean：弃权簇保留但不进分母 → 5.6
        (1, 5.6, {
            "rule": "cluster_equal_mean", "max_score": "6", "arithmetic": "",
            "inputs": [
                {"source": "cluster1", "score": "0", "state": "abstain", "reason": "no_evidence"},
                {"source": "cluster2", "score": "5.6", "state": "vote", "reason": ""},
                {"source": "cluster3", "score": "0", "state": "abstain", "reason": "no_evidence"},
            ],
        }),
        # 旧格式 equal_mean（无 state，向后兼容）：全部计票 → (5+3)/2=4
        (2, 4.0, {
            "rule": "cluster_equal_mean",
            "inputs": [
                {"source": "cluster1", "score": "5"},
                {"source": "cluster2", "score": "3"},
            ],
        }),
        # 新格式 subitem：Σ 逐子项合并分 = 4.5（cap 5 不触发）
        (3, 4.5, {
            "rule": "subitem_cluster_mean", "max_score": "5",
            "inputs": [
                {"source": "sub:甲", "score": "1.8",
                 "votes": [{"cluster": 1, "score": "1.8", "state": "vote", "reason": ""}]},
                {"source": "sub:乙", "score": "1.7", "votes": []},
                {"source": "sub:丙", "score": "1.0", "votes": []},
            ],
        }),
        # 委员会 trimmed：(2+3+4)/3=3（路径不变）
        (4, 3.0, {
            "rule": "trimmed_mean_drop_high_low",
            "inputs": [
                {"source": "judge1", "score": "2"},
                {"source": "judge2", "score": "3"},
                {"source": "judge3", "score": "4"},
            ],
        }),
        # 坏样例：final 还是旧语义的 1.87 → 应报不可复算
        (5, 1.87, {
            "rule": "cluster_equal_mean", "max_score": "6",
            "inputs": [
                {"source": "cluster1", "score": "0", "state": "abstain"},
                {"source": "cluster2", "score": "5.6", "state": "vote"},
                {"source": "cluster3", "score": "0", "state": "abstain"},
            ],
        }),
    ]
    for sid, final, agg in rows:
        cur.execute(
            "INSERT INTO score_records"
            " (id, run_id, bidder_id, criterion_id, final_score, ai_raw_json)"
            " VALUES (?,?,?,?,?,?)",
            (sid, 1, 1, sid, final,
             _json.dumps({"aggregation_v2": agg}, ensure_ascii=False)),
        )
    ok, bad, errs = mod._recomputability_check(cur, 1)
    assert ok == 4, errs
    assert bad == 1
    con.close()


# ---------------------------------------------------------------------------
# _match_subitem 对"名称（2 分）"后缀容错（c52 型污染）
# ---------------------------------------------------------------------------
def test_match_subitem_strips_score_suffix():
    from app.engine.subjective import _match_subitem

    keys = ["订单管理", "协同办公"]
    assert _match_subitem("订单管理（2 分）", keys) == "订单管理"
    assert _match_subitem("协同办公(1分)", keys) == "协同办公"
    assert _match_subitem("订单管理（0-2 分）", keys) == "订单管理"
    # 剥离后仍按最长命中优选
    assert _match_subitem("订单管理（2 分）", ["管理", "订单管理"]) == "订单管理"
