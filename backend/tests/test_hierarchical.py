# -*- coding: utf-8 -*-
"""分层证据评审单测：证据地图聚类 + 分层聚合 + 稳定性语义。"""
from __future__ import annotations

import threading
from decimal import Decimal

from app.engine.selection import build_evidence_map, split_text_chunks
from app.engine.subjective import SubjectiveOutput, suggest_score_hierarchical


def _chunks(n: int, fill: str = "无关填充内容") -> list:
    return [(i, f"{fill}{i}") for i in range(n)]


def test_evidence_map_clusters_disjoint(monkeypatch):
    """相邻高分组块合并成簇，间隔块分隔出独立簇，簇互不相交。"""
    from app.evidence import embed_client
    from app.engine import selection

    # chunk 3-5 与 chunk 12-13 各为一个主题（查询向量与前者更近）
    bodies = []
    for i in range(20):
        if 3 <= i <= 5:
            bodies.append("运维服务方案质量保障措施详细内容" + str(i))
        elif 12 <= i <= 13:
            bodies.append("运维服务方案质量保障措施补充说明" + str(i))
        else:
            bodies.append("无关填充" + str(i))
    chunks = [(i, bodies[i]) for i in range(20)]

    def fake_embed_batch(texts):
        out = []
        for t in texts:
            if "质量保障" in t:
                out.append([1.0] + [0.0] * 1023)
            else:
                out.append([0.0] * 1024)
        return out

    monkeypatch.setattr(embed_client, "embed_one", lambda t: [1.0] + [0.0] * 1023)
    monkeypatch.setattr(embed_client, "embed_batch", fake_embed_batch)

    emap = selection.build_evidence_map(chunks, {"name": "质量保障"})
    assert emap is not None
    clusters, _q = emap
    all_ids = [i for cl in clusters for i in cl["ids"]]
    assert len(all_ids) == len(set(all_ids))  # 不相交
    assert set(all_ids) >= {3, 4, 5}  # 高分连续块被聚为一簇


def test_evidence_map_unavailable_returns_none(monkeypatch):
    from app.evidence import embed_client
    from app.engine import selection

    def boom(texts):
        raise RuntimeError("嵌入服务调用失败")

    monkeypatch.setattr(embed_client, "embed_batch", boom)
    assert selection.build_evidence_map(_chunks(5), {"name": "x"}) is None


# ---------------------------------------------------------------------------
# 分层评审聚合
# ---------------------------------------------------------------------------
class SeqGateway:
    """按预设输出逐次返回的假网关（线程安全计数）。"""

    def __init__(self, outputs):
        self._outputs = outputs
        self._lock = threading.Lock()
        self._i = 0

    def chat_json(self, _system, _user, model, data_class="synthetic"):  # noqa: ANN001
        with self._lock:
            out = self._outputs[min(self._i, len(self._outputs) - 1)]
            self._i += 1
        return out, {}


def _ok(score) -> SubjectiveOutput:
    return SubjectiveOutput(
        level_label="优", score=score, reasoning="依据原文",
        evidence=[{"quote": "投标文件中载明相关内容实质段落原文", "page_hint": 12}],
    )


def _fail() -> SubjectiveOutput:
    return SubjectiveOutput(insufficient_evidence=True, reasoning="证据不足")


CRITERION = {"name": "测试项", "max_score": "10", "score_type": "subjective", "levels": []}


def test_hierarchical_takes_best_cluster(monkeypatch):
    monkeypatch.setenv("BID_EVAL_REVIEW_CLUSTERS", "3")
    from app.engine import selection
    from app.evidence import embed_client
    from app.engine.subjective import suggest_score_hierarchical

    def fake_embed_batch(texts):
        # 9 块中 0-2 与 5-7 为相关证据，3/4/8 无关（制造两个分离簇）
        flags = [1, 1, 1, 0, 0, 1, 1, 1, 0]
        return [[1.0 if flags[i] else 0.0] + [0.0] * 1023 for i, _t in enumerate(texts)]

    monkeypatch.setattr(embed_client, "embed_one", lambda t: [1.0] + [0.0] * 1023)
    monkeypatch.setattr(embed_client, "embed_batch", fake_embed_batch)
    monkeypatch.setattr(
        selection, "split_text_chunks",
        lambda text, chunk_size=2000: [(i, f"块{i}") for i in range(9)],
    )

    gw = SeqGateway([_ok(6), _ok(8), _ok(5)])
    suggestion, committee = suggest_score_hierarchical(gw, CRITERION, "投标文本", k=3)
    assert suggestion.status == "ai_suggested"
    assert suggestion.score == Decimal("8")  # 取最高分簇为主评
    assert len(committee) == 2  # 数据只有 2 个分离证据簇（k=3 为上限，实评按实有簇数）
    assert any("分层证据评审" in w for w in suggestion.warnings)


def test_hierarchical_all_clusters_fail(monkeypatch):
    monkeypatch.setenv("BID_EVAL_REVIEW_CLUSTERS", "2")
    from app.engine import selection
    from app.evidence import embed_client
    from app.engine.subjective import suggest_score_hierarchical

    monkeypatch.setattr(embed_client, "embed_one", lambda t: [1.0] + [0.0] * 1023)
    monkeypatch.setattr(embed_client, "embed_batch", lambda texts: [[0.5] * 1024 for _t in texts])
    monkeypatch.setattr(
        selection, "split_text_chunks",
        lambda text, chunk_size=2000: [(i, f"块{i}") for i in range(4)],
    )
    gw = SeqGateway([_fail()])
    suggestion, committee = suggest_score_hierarchical(gw, CRITERION, "投标文本", k=2)
    assert suggestion.status == "need_human"
    assert any("llm_failed" in w or "均未给出有效分" in w for w in suggestion.warnings)
