# -*- coding: utf-8 -*-
"""eval_suite --recompute-count：count 格从审计案例重放 score_count 对照 final。

A4（2026-11-04）：客观分可复算工具——服务验收与事后审计，复用 score_count
引擎本身，不引新判定逻辑。全离线 sqlite fixture。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from eval_suite import _recompute_count_check  # noqa: E402


def _mk_db(tmp_path, *, final_score: str, per_item: str = "2", max_items: str = "3",
           cases: list | None = None, batches_failed: int = 0) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE rule_packages (id INTEGER PRIMARY KEY, snapshot_json TEXT);
        CREATE TABLE evaluation_runs (id INTEGER PRIMARY KEY, package_id INTEGER);
        CREATE TABLE bidders (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE criteria (
            id INTEGER PRIMARY KEY, package_id INTEGER, code TEXT, name TEXT,
            score_type TEXT, max_score TEXT
        );
        CREATE TABLE score_records (
            id INTEGER PRIMARY KEY, run_id INTEGER, bidder_id INTEGER,
            criterion_id INTEGER, final_score TEXT, ai_raw_json TEXT
        );
        """
    )
    snap = {
        "criteria": [
            {
                "code": "c46", "name": "投标人业绩", "score_type": "count",
                "max_score": "4",
                "params": {"field": "performance", "per_item": per_item, "max_items": max_items},
                "required_evidence": "合同复印件、发票复印件和业主证明（或验收材料）",
                "source_quote": "每提供一个得2分，最高得4分",
            }
        ]
    }
    cur.execute("INSERT INTO rule_packages VALUES (1, ?)", (json.dumps(snap, ensure_ascii=False),))
    cur.execute("INSERT INTO evaluation_runs VALUES (79, 1)")
    cur.execute("INSERT INTO bidders VALUES (44, '某投标人')")
    cur.execute("INSERT INTO criteria VALUES (2126, 1, 'c46', '投标人业绩', 'count', '4')")
    if cases is None:
        cases = [
            {"name": f"A{i}", "contract": True, "invoice": True, "acceptance": True,
             "quote": f"项目{i} 合同", "sanitized": "ok", "case_source": "table"}
            for i in range(2)
        ]
    audit = {"verified_cases": cases, "meta": {"batches_sent": 1}}
    if batches_failed:
        audit["meta"]["batches_failed"] = batches_failed
        audit["meta"]["batches_total"] = 3
    cur.execute(
        "INSERT INTO score_records VALUES (1, 79, 44, 2126, ?, ?)",
        (final_score, json.dumps(audit, ensure_ascii=False)),
    )
    con.commit()
    return con


def test_recompute_count_match_passes(tmp_path) -> None:
    """2 个全链案例 × per_item=2 → final=4 应一致。"""
    con = _mk_db(tmp_path, final_score="4")
    ok, bad, errs = _recompute_count_check(con.cursor(), 79)
    assert ok == 1 and bad == 0, errs


def test_recompute_count_mismatch_flagged(tmp_path) -> None:
    """final 与重放不一致 → bad + diff 明细。"""
    con = _mk_db(tmp_path, final_score="0")
    ok, bad, errs = _recompute_count_check(con.cursor(), 79)
    assert ok == 0 and bad == 1
    assert "score#1" in errs[0] and "重放" in errs[0]


def test_recompute_count_amount_tier_regression_shape(tmp_path) -> None:
    """run79 回归形态：金额档 tiers 时重放仍走 per_item 线性（与 score_count 同步）。

    旧 bug 下重放也会得 0——工具与引擎必须一起修（本测试锁双侧一致）。
    """
    con = _mk_db(tmp_path, final_score="4", per_item="1", max_items="4",
                 cases=[{"name": f"A{i}", "contract": True, "invoice": True,
                         "acceptance": True, "quote": f"项目{i}", "sanitized": "ok",
                         "case_source": "table"} for i in range(4)])
    # 改 snapshot params 加金额档 tiers
    cur = con.cursor()
    row = cur.execute("SELECT snapshot_json FROM rule_packages").fetchone()
    snap = json.loads(row[0])
    snap["criteria"][0]["params"]["tiers"] = [
        {"min_amount_yuan": "500000", "max_amount_yuan": "1000000", "per_item": "0.5"},
        {"min_amount_yuan": "1000000", "per_item": "1"},
    ]
    cur.execute("UPDATE rule_packages SET snapshot_json=? WHERE id=1",
                (json.dumps(snap, ensure_ascii=False),))
    con.commit()
    ok, bad, errs = _recompute_count_check(cur, 79)
    assert ok == 1 and bad == 0, f"金额档 tiers 不得让重放归零：{errs}"


def test_recompute_count_batch_failure_injected(tmp_path) -> None:
    """批失败注入重放：surviving 案例照计（1×2=2），与落库一致则通过。"""
    con = _mk_db(tmp_path, final_score="2", cases=[
        {"name": "A0", "contract": True, "invoice": True, "acceptance": True,
         "quote": "项目0", "sanitized": "ok", "case_source": "llm"},
    ], batches_failed=2)
    ok, bad, errs = _recompute_count_check(con.cursor(), 79)
    assert ok == 1 and bad == 0, errs


def test_recompute_count_skips_without_cases(tmp_path) -> None:
    """无案例明细/无参数的格不猜输入——skip 不计 bad。"""
    con = _mk_db(tmp_path, final_score="0", cases=[])
    ok, bad, errs = _recompute_count_check(con.cursor(), 79)
    assert ok == 0 and bad == 0, errs
