"""_price_stage 门槛读数口径（run 119 诊断修复，2026-11-16）。

价格门槛（如"技术得分≥70%才进最佳报价值计算"）必须读**已定格**的上游分数
（含收尾暂定兜底分）；只有真正没有分数（未评/读取故障未定分）才按"未剔除"
暂定处理。旧实现按 worker 完成集判断，收尾兜底格被误判"分数未完成"→ 整体
放弃门槛、基准价算错（run 119 实证）。
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

from sqlalchemy import select

from app.db.models import (
    Bidder, Criterion, Document, DocumentPage, EvaluationRun, Project,
    RulePackage, ScoreRecord,
)
from app.db.session import session_scope
from app.services import automatic_scoring_service as scoring


def _fallback_row(run_id, bid_id, crit_id, value="0"):
    fallback = {"kind": "provisional_zero", "value": value, "basis": "machine_fallback",
                "reason": "自动评审未产生有效结论", "score_type": "subjective"}
    return ScoreRecord(
        run_id=run_id, bidder_id=bid_id, criterion_id=crit_id,
        final_score=Decimal(value), machine_score=Decimal(value),
        machine_status="concern", review_status="auto_confirmed",
        ai_reasoning="自动评审未产生有效结论，按暂定 0 分兜底（非证据结论）",
        ai_raw_json={"auto_review": {"state": "completed", "basis": "fallback",
                                     "reasoning": "暂定兜底", "fallback": fallback}},
        warnings_json=[{"message": "暂定兜底", "fallback_score": fallback}],
    )


def _real_row(run_id, bid_id, crit_id, value):
    return ScoreRecord(
        run_id=run_id, bidder_id=bid_id, criterion_id=crit_id,
        final_score=Decimal(value), machine_score=Decimal(value),
        machine_status="pass", review_status="auto_confirmed",
        ai_reasoning="证据充分",
        ai_raw_json={"auto_review": {"state": "completed", "basis": "evidence",
                                     "reasoning": "证据充分"}},
    )


def _seed(app_env, *, mode: str):
    """两家投标人：甲=收尾兜底暂定 0 分（mode="fallback"）/真实 10 分（"real"）/
    无任何记录（"missing"）；乙=真实 40 分。"""
    content = "投标文件内容"
    source = app_env / "bid-gate.txt"
    source.write_text(content, encoding="utf-8")
    with session_scope() as s:
        s.add(Project(id="price-gate", name="门槛读数测试"))
        s.flush()
        package = RulePackage(project_id="price-gate", version=1, status="frozen",
                              content_hash="frozen")
        s.add(package)
        s.flush()
        tech = Criterion(package_id=package.id, code="T1", name="技术得分",
                         category="technical", score_type="subjective",
                         max_score=Decimal("46"), status="verified")
        price = Criterion(package_id=package.id, code="P1", name="商务报价得分",
                          category="price", score_type="formula",
                          max_score=Decimal("50"), status="verified")
        s.add_all([tech, price])
        s.flush()
        ids: dict[str, int] = {}
        for name in ("甲", "乙"):
            bidder = Bidder(project_id="price-gate", name=name,
                            bid_price=Decimal("1"), price_status="confirmed")
            s.add(bidder)
            s.flush()
            ids[name] = bidder.id
            doc = Document(project_id="price-gate", bidder_id=bidder.id, kind="bid",
                           parse_status="parsed", original_filename="bid.txt",
                           stored_path=str(source),
                           sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                           page_count=1)
            s.add(doc)
            s.flush()
            s.add(DocumentPage(document_id=doc.id, page_no=1, text=content, source="text"))
        tech_crit = {"id": tech.id, "code": "T1", "name": "技术得分", "category": "technical",
                     "score_type": "subjective", "max_score": "46"}
        formula_crit = {
            "id": price.id, "code": "P1", "name": "商务报价得分", "category": "price",
            "score_type": "formula", "max_score": "50",
            "formula": {
                "formula_type": "deviation_deduct", "benchmark_method": "average",
                "k": 1, "max_score": "50", "deduct_rate": "0.5",
                "params_json": {"benchmark_gate": {
                    "gate_on": "tech", "min_ratio": "70", "ratio_kind": "percent",
                    "on_fail": "exclude"}},
            },
        }
        manifest = {"bidders": [{"id": ids["甲"], "name": "甲"}, {"id": ids["乙"], "name": "乙"}],
                    "criteria": [tech_crit, formula_crit]}
        run = EvaluationRun(project_id="price-gate", package_id=package.id, package_version=1,
                            package_content_hash="frozen", status="running",
                            stages_json=["price"],
                            config_json={"automation_version": 1,
                                         "automation_manifest": manifest,
                                         "bidder_ids": [ids["甲"], ids["乙"]]})
        s.add(run)
        s.flush()
        run_id = run.id
        if mode == "fallback":
            s.add(_fallback_row(run_id, ids["甲"], tech.id))
        elif mode == "real":
            s.add(_real_row(run_id, ids["甲"], tech.id, "10"))
        s.add(_real_row(run_id, ids["乙"], tech.id, "40"))
    return run_id, ids, tech_crit, formula_crit


def _price_cells(run_id):
    with session_scope() as s:
        rows = list(s.execute(select(ScoreRecord).where(
            ScoreRecord.run_id == run_id, ScoreRecord.criterion_id.in_(
                select(Criterion.id).where(Criterion.score_type == "formula")))).scalars())
        return {r.bidder_id: (r.ai_reasoning or "", r.ai_raw_json or {}) for r in rows}


def test_gate_reads_settled_fallback_scores(app_env):
    """收尾兜底暂定分（已定格）必须进门槛：甲暂定 0 < 32.2 → 剔出基准；不再按'未剔除'跳过门槛。"""
    run_id, ids, tech_crit, formula_crit = _seed(app_env, mode="fallback")
    scoring._price_stage(
        run_id,
        {"bidders": [{"id": ids["甲"], "name": "甲"}, {"id": ids["乙"], "name": "乙"}],
         "criteria": [tech_crit, formula_crit]},
        [tech_crit, formula_crit],
        {ids["甲"]: Decimal("100"), ids["乙"]: Decimal("120")},
        completed=set(),  # 模拟旧 bug：worker 完成集不含收尾兜底格
    )
    cells = _price_cells(run_id)
    reason_a, raw_a = cells[ids["甲"]]
    reason_b, _ = cells[ids["乙"]]
    # 门槛已执行：甲未达门槛被剔出基准，基准=乙 120（不是 (100+120)/2=110）
    assert "未达基准价门槛" in reason_a
    assert "按未剔除处理" not in reason_a
    assert "门槛输入含暂定分" in reason_a
    assert "未达基准价门槛" not in reason_b
    assert raw_a.get("benchmark") == "120.00"
    # 门槛输入含暂定分 → 价格分标记暂定（人工复核提示）
    assert raw_a.get("price_provisional") is True
    assert raw_a.get("gate_fallback_criteria") == ["T1"]


def test_gate_missing_record_keeps_partial_run_safety(app_env):
    """真正没有分数（无记录）时保持'未剔除+暂定'语义（partial 运行安全阀不变）。"""
    run_id, ids, tech_crit, formula_crit = _seed(app_env, mode="missing")
    scoring._price_stage(
        run_id,
        {"bidders": [{"id": ids["甲"], "name": "甲"}, {"id": ids["乙"], "name": "乙"}],
         "criteria": [tech_crit, formula_crit]},
        [tech_crit, formula_crit],
        {ids["甲"]: Decimal("100"), ids["乙"]: Decimal("120")},
        completed=set(),
        allow_partial_gate=True,
    )
    cells = _price_cells(run_id)
    reason_a, raw_a = cells[ids["甲"]]
    assert "门槛所需评分尚未完成" in reason_a and "按未剔除处理" in reason_a
    # 门槛未执行 → 两家全量进基准：(100+120)/2=110
    assert raw_a.get("benchmark") == "110.00"
