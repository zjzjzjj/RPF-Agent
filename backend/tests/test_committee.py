"""评审委员会聚合单测：去尾平均语义 + 多 Agent 聚合降级规则（纯离线，假网关）。"""
from __future__ import annotations

import threading
from decimal import Decimal

from app.engine.subjective import (
    SubjectiveOutput,
    _trimmed_mean,
    suggest_score_committee,
)


def test_trimmed_mean_basic():
    assert _trimmed_mean([Decimal(v) for v in ("7", "8", "8", "9", "10")]) == Decimal("8.33")
    assert _trimmed_mean([Decimal(v) for v in ("1", "2", "3", "4", "5")]) == Decimal("3.00")


def test_trimmed_mean_ties_drop_one_each():
    """并列最高/最低各弃一个：[2,2,2,5,5] → 弃一个 2、一个 5 → (2+2+5)/3=3.00。"""
    assert _trimmed_mean([Decimal(v) for v in ("2", "2", "2", "5", "5")]) == Decimal("3.00")


def test_trimmed_mean_rounding_half_up():
    # 中间三个 8.00/8.00/8.01 → 8.0033… → 8.00；8.005 场景进位
    assert _trimmed_mean([Decimal("8"), Decimal("8"), Decimal("8.01"), Decimal("8"), Decimal("10")]) == Decimal("8.00")


class FakeGateway:
    """按预设输出逐次返回的假网关（线程安全计数）。"""

    def __init__(self, outputs: list[SubjectiveOutput]):
        self._outputs = outputs
        self._lock = threading.Lock()
        self._i = 0

    def chat_json(self, _system, _user, model, data_class="synthetic"):  # noqa: ANN001
        with self._lock:
            out = self._outputs[min(self._i, len(self._outputs) - 1)]
            self._i += 1
        return out, {}


def _ok(score: str, label: str = "得2-3分") -> SubjectiveOutput:
    return SubjectiveOutput(
        level_label=label, score=score, reasoning="依据原文",
        evidence=[{"quote": "投标文件中载明相关方案内容的原文段落", "page_hint": 12}],
    )


def _fail() -> SubjectiveOutput:
    return SubjectiveOutput(insufficient_evidence=True, reasoning="证据不足")


CRITERION = {"code": "c1", "name": "测试项", "max_score": "10", "score_type": "subjective", "levels": []}


def test_committee_happy_path_5_judges():
    gw = FakeGateway([_ok("7"), _ok("8"), _ok("8"), _ok("9"), _ok("10")])
    suggestion, committee = suggest_score_committee(gw, CRITERION, "投标文本", size=5)
    assert suggestion.status == "ai_suggested"
    assert suggestion.score == Decimal("8.33")  # 去 10 与 7 → (8+8+9)/3
    dropped = [c for c in committee if c.get("dropped")]
    assert len(dropped) == 2
    assert {c["score"] for c in dropped} == {"7", "10"}
    assert len(committee) == 5
    assert any("去最高" in w and "取平均" in w for w in suggestion.warnings)


def test_committee_too_few_valid_scores():
    """仅 2 位评委有效 → 整项 need_human（<3 无法去尾）。"""
    gw = FakeGateway([_ok("8"), _ok("9"), _fail(), _fail(), _fail()])
    suggestion, committee = suggest_score_committee(gw, CRITERION, "投标文本", size=5)
    assert suggestion.status == "need_human"
    assert suggestion.score is None
    assert len(committee) == 5


def test_committee_exactly_three_valid():
    """恰好 3 个有效分：弃最高最低后剩 1 个（中位数语义）。"""
    gw = FakeGateway([_ok("5"), _ok("8"), _ok("9"), _fail(), _fail()])
    suggestion, _committee = suggest_score_committee(gw, CRITERION, "投标文本", size=5)
    assert suggestion.status == "ai_suggested"
    assert suggestion.score == Decimal("8.00")


def test_committee_partial_valid_warns():
    """4/5 有效仍聚合，但带警告。"""
    gw = FakeGateway([_ok("7"), _ok("8"), _ok("9"), _ok("10"), _fail()])
    suggestion, _committee = suggest_score_committee(gw, CRITERION, "投标文本", size=5)
    assert suggestion.status == "ai_suggested"
    assert suggestion.score == Decimal("8.50")  # 去 10 与 7 → (8+9)/2
    assert any("未给出有效分" in w for w in suggestion.warnings)


def test_committee_single_judge_short_circuit(monkeypatch):
    """单评委模式（size=1）：一次调用直接采纳，不去尾、无聚合门槛。"""
    monkeypatch.setenv("BID_EVAL_COMMITTEE_SIZE", "1")
    gw = FakeGateway([_ok("8.5")])
    suggestion, committee = suggest_score_committee(gw, CRITERION, "投标文本")
    assert suggestion.status == "ai_suggested"
    assert suggestion.score == Decimal("8.5")
    assert len(committee) == 1 and committee[0]["dropped"] is False
    assert not any("去最高" in w for w in suggestion.warnings)


def test_committee_single_judge_invalid(monkeypatch):
    monkeypatch.setenv("BID_EVAL_COMMITTEE_SIZE", "1")
    gw = FakeGateway([_fail()])
    suggestion, committee = suggest_score_committee(gw, CRITERION, "投标文本")
    assert suggestion.status == "need_human"
    assert suggestion.score is None
    assert any("单评委" in w for w in suggestion.warnings)


def test_default_committee_size_is_one(monkeypatch):
    monkeypatch.delenv("BID_EVAL_COMMITTEE_SIZE", raising=False)
    from app import config

    assert config.committee_size() == 1
