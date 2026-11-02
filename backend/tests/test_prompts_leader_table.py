# -*- coding: utf-8 -*-
"""负责人情况表宽松口径的 prompt 契约（2026-11-02）。

锁 LEADER_TABLE_PROOF_CLAUSE 与主 prompt 负面条款的边界：④形态（正式情况表）
被认作 true，纯简历自述仍 false——两边关键词都锁，防误删/误松。
"""
from __future__ import annotations

from app.rules import prompts


def test_clause_has_leader_table_true_form() -> None:
    """CLAUSE 含第④形态：项目负责人情况表 + 正式表格 + 所有 leader 案例 true。"""
    c = prompts.LEADER_TABLE_PROOF_CLAUSE
    assert "项目负责人情况表" in c
    assert "正式表格" in c
    assert "所有" in c and "true" in c


def test_clause_boundary_not_resume() -> None:
    """CLAUSE 与负面条款划界：负责人情况表不是简历自述；纯简历仍按原条款 false。"""
    c = prompts.LEADER_TABLE_PROOF_CLAUSE
    assert "不是简历自述" in c
    assert "纯简历表" in c  # 负面条款边界保留


def test_main_prompt_keeps_resume_negative_clause() -> None:
    """主 prompt 的简历自述负面条款未被误删（④形态豁免不等于删条款）。"""
    p = prompts.MATERIAL_COUNT_SYSTEM_PROMPT
    assert "简历自述不计分" in p
    assert "wp=false" in p


def test_clause_not_in_main_prompt_by_default() -> None:
    """CLAUSE 是独立常量，不默认并入主 prompt（拼接由 scoring_service 按开关控制）。"""
    assert "项目负责人情况表" not in prompts.MATERIAL_COUNT_SYSTEM_PROMPT
