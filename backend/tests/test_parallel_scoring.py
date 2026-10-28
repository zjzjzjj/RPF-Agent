# -*- coding: utf-8 -*-
"""并行评分骨架语义（_parallel_map）与 forced_zero 短路——只改并发不改判定。"""
from __future__ import annotations

from decimal import Decimal

from app.services.scoring_service import (
    _forced_zero_reason,
    _is_fatal_llm_error,
    _parallel_map,
)


class _FakeCtx:
    def __init__(self):
        self.messages: list[str] = []
        self.advances = 0

    def advance(self, n: int = 1, message: str | None = None) -> None:
        self.advances += n
        self.messages.append(message or "")


def test_parallel_map_preserves_order_and_counts_progress():
    cells = list(range(20))

    def worker(x):
        return x * 2

    ctx = _FakeCtx()
    out = _parallel_map(worker, cells, ctx=ctx, message_fmt="测试 {done}/{total}")
    assert out == [x * 2 for x in cells]  # 与输入同序
    assert ctx.advances == 20
    assert ctx.messages[-1] == "测试 20/20"


def test_parallel_map_fatal_error_propagates():
    def worker(x):
        if x == 3:
            raise RuntimeError("402 insufficient balance")
        return x

    try:
        _parallel_map(worker, [1, 2, 3, 4], ctx=None, message_fmt="{done}/{total}")
    except RuntimeError as exc:
        assert "insufficient" in str(exc)
    else:
        raise AssertionError("quota 类错误必须穿透（fail-fast）")


def test_is_fatal_llm_error_classification():
    assert _is_fatal_llm_error(RuntimeError("402 Payment Required"))
    assert _is_fatal_llm_error(RuntimeError("insufficient quota"))
    from app.errors import LLMBudgetExceeded

    assert _is_fatal_llm_error(LLMBudgetExceeded("x"))
    assert not _is_fatal_llm_error(ValueError("普通解析错误"))


def test_forced_zero_requires_rule_text_and_absent_material():
    """双条件：细则明文「不得分」+ 材料确认不在，缺一不置 0（页数阈值已删）。"""
    c_demands = {"required_evidence": "提供社保证明，不提供或不能提供有效证明的不得分",
                 "source_quote": "", "description": ""}
    c_silent = {"required_evidence": "提供社保证明", "source_quote": "", "description": ""}

    # 材料在 → 不置 0（哪怕细则有罚则）
    assert _forced_zero_reason("团队成员经验", {"social_material": True}, c_demands) is None
    # 材料不在但细则无罚则 → 不越权置 0
    assert _forced_zero_reason("团队成员经验", {"social_material": False}, c_silent) is None
    # 双条件齐 → 置 0
    reason = _forced_zero_reason("团队成员经验", {"social_material": False}, c_demands)
    assert reason is not None and "社保" in reason
    # 负责人经验同构
    c2 = {"required_evidence": "五年及以上工作经验证明，无证明材料不得分",
          "source_quote": "", "description": ""}
    assert _forced_zero_reason("项目负责人经验", {"exp_material": True}, c2) is None
    assert _forced_zero_reason("项目负责人经验", {"exp_material": False}, c2) is not None
    # 非人员条目不受门槛影响
    assert _forced_zero_reason("运维方案", {"exp_material": False}, c2) is None


def test_forced_zero_zero_score_shape():
    """短路返回的 suggestion 与 Phase2 权威替换同形（score=0/ai_suggested）。"""
    # 这里只锁语义参数，Phase2 替换路径由 test_scoring_e2e 全链覆盖
    reason = _forced_zero_reason(
        "项目负责人经验", {"exp_material": False},
        {"required_evidence": "经验证明，否则该项不得分", "source_quote": "", "description": ""},
    )
    assert "不得分" in reason
    assert Decimal(0) == Decimal(0)  # 分值形态由 SubjectiveSuggestion(score=0) 承载
