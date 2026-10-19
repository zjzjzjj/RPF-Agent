"""评分结果聚合端点（GET /runs/{id}/score-summary）的契约与口径测试。

全程 LLM-free：用模板实例化的冻结包 + 人工录价 + objective/price 两阶段运行，
主观分记录按需直接种库（与 test_scoring_e2e 的防锚定测试同款手法）。

核心护栏：
- 总分/名次/各桶分值与 `GET /projects/{pid}/ranking` **逐字相等**；
- match / veto 不计分，不能被误判为「未定分」；
- 无 score_record 的项显式以 not_run 出现，不静默消失；
- AI 建议分与理由遵守 settled_or_revealed 门控。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from .conftest import wait_job

SUBJECTIVE_CODE = "T4.1"  # comprehensive-goods 模板里的主观分项


@pytest.fixture()
def summary_chain(client):  # noqa: ANN001
    """轻量链路：冻结模板包 + 3 家已录价投标人 + objective/price 运行（不解析 PDF）。"""
    pid = client.post("/api/projects", json={"name": "评分结果聚合测试"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/rules/instantiate",
                    json={"template_id": "comprehensive-goods"})
    assert r.status_code == 200, r.text
    package_id = r.json()["package_id"]
    pkg = client.get(f"/api/rules/packages/{package_id}").json()
    for c in pkg["criteria"]:
        rr = client.post(f"/api/rules/criteria/{c['id']}/signoff",
                         json={"reason": "测试基线启用"})
        assert rr.status_code == 200, rr.text
    assert client.post(f"/api/rules/packages/{package_id}/confirm").status_code == 200
    assert client.post(f"/api/rules/packages/{package_id}/freeze").status_code == 200

    bidder_ids: list[int] = []
    for name, price in (("甲公司", "1000000"), ("乙公司", "1100000"), ("丙公司", "1200000")):
        b = client.post(f"/api/projects/{pid}/bidders", json={"name": name}).json()
        rr = client.post(f"/api/bidders/{b['id']}/price/set",
                         json={"price_yuan": price, "reason": "测试录入报价"})
        assert rr.status_code == 200, rr.text
        bidder_ids.append(b["id"])

    resp = client.post(
        f"/api/projects/{pid}/runs",
        json={"package_id": package_id, "bidder_ids": bidder_ids,
              "stages": ["objective", "price"]},
    )
    assert resp.status_code == 200, resp.text
    job = wait_job(client, resp.json()["job_id"], timeout=180)
    assert job["status"] == "succeeded", job.get("error")
    return {
        "pid": pid,
        "package_id": package_id,
        "bidder_ids": bidder_ids,
        "run_id": job["result_json"]["run_id"],
        "criteria": pkg["criteria"],
    }


def _get(client, chain, **params):  # noqa: ANN001
    return client.get(f"/api/runs/{chain['run_id']}/score-summary",
                      params={"project_id": chain["pid"], **params})


def _all_items(bidder: dict) -> list[dict]:
    return [item for bucket in bidder["buckets"] for item in bucket["items"]]


def _by_code(bidder: dict, code: str) -> dict:
    return next(i for i in _all_items(bidder) if i["code"] == code)


def _seed_subjective(chain, bidder_id, **fields):  # noqa: ANN001
    from app.db.models import Criterion, ScoreRecord
    from app.db.session import session_scope

    with session_scope() as s:
        crit = s.execute(
            select(Criterion).where(
                Criterion.package_id == chain["package_id"],
                Criterion.code == SUBJECTIVE_CODE,
            )
        ).scalars().first()
        assert crit is not None
        row = ScoreRecord(
            run_id=chain["run_id"],
            bidder_id=bidder_id,
            criterion_id=crit.id,
            max_score=Decimal("20"),
            score_type="subjective",
            **fields,
        )
        s.add(row)
        s.flush()
        return row.id


# ---------------------------------------------------------------------------
# 契约与口径
# ---------------------------------------------------------------------------
def test_contract_fields_complete(client, summary_chain):  # noqa: ANN001
    r = _get(client, summary_chain)
    assert r.status_code == 200, r.text
    d = r.json()

    assert d["run"]["id"] == summary_chain["run_id"]
    assert d["run"]["ready"] is True
    assert d["criteria_count"] == len(summary_chain["criteria"])
    assert len(d["bidders"]) == 3
    assert d["ai_gating"]["rule"] == "settled_or_revealed"

    for b in d["bidders"]:
        assert b["state"] == "scored"
        assert b["summary"] is not None
        assert {x["key"] for x in b["buckets"]} == {
            "price", "technical", "commercial", "qualification_compliance",
        }
        assert len(_all_items(b)) == d["criteria_count"]
        assert all(i["score_record_id"] is not None or i["state"] == "not_run"
                   for i in _all_items(b))


def test_totals_equal_ranking(client, summary_chain):  # noqa: ANN001
    """结果页的四个分数必须与排名端点逐字相等（同一 run）。"""
    d = _get(client, summary_chain).json()
    ranking = client.get(
        f"/api/projects/{summary_chain['pid']}/ranking",
        params={"run_id": summary_chain["run_id"]},
    ).json()
    rank = {row["bidder_id"]: row for row in ranking["rows"]}
    for b in d["bidders"]:
        row = rank[b["bidder_id"]]
        assert b["summary"]["total"] == row["total"]
        assert b["summary"]["price_score"] == row["price_score"]
        assert b["summary"]["tech_score"] == row["tech_total"]
        assert b["summary"]["commercial_score"] == row["commercial_total"]
        assert b["rank"] == row["rank"]
        assert b["summary"]["missing_criteria"] == row["missing_final"]


def test_missing_final_excluded_from_total(client, summary_chain):  # noqa: ANN001
    bidder_id = summary_chain["bidder_ids"][0]
    _seed_subjective(summary_chain, bidder_id, final_score=None, review_status="pending")

    d = _get(client, summary_chain).json()
    b = next(x for x in d["bidders"] if x["bidder_id"] == bidder_id)
    item = _by_code(b, SUBJECTIVE_CODE)
    assert item["state"] == "missing_final"
    assert item["final"] is None
    assert item["deduction"]["kind"] == "undetermined"
    assert item["name"] in b["summary"]["missing_criteria"]
    assert b["summary"]["missing_count"] >= 1
    assert any("未定分" in w for w in d["warnings"])


def test_veto_and_match_are_never_missing_final(client, summary_chain):  # noqa: ANN001
    d = _get(client, summary_chain).json()
    for b in d["bidders"]:
        for item in _all_items(b):
            if item["score_type"] == "veto":
                assert item["state"] == "veto"
                assert item["final"] is None
                assert item["deduction"]["kind"] == "n_a"
                assert item["name"] not in b["summary"]["missing_criteria"]
            if item["score_type"] == "match":
                assert item["state"] == "match"
                assert item["name"] not in b["summary"]["missing_criteria"]


def test_not_run_items_are_explicit(client, summary_chain):  # noqa: ANN001
    """只跑 objective+price 时，主观分项应以 not_run 出现而不是消失。"""
    d = _get(client, summary_chain).json()
    subjective = [i for b in d["bidders"] for i in _all_items(b)
                  if i["score_type"] == "subjective"]
    assert subjective, "模板应包含主观分项"
    assert all(i["state"] == "not_run" for i in subjective)
    assert all(i["score_record_id"] is None for i in subjective)
    assert all(i["deduction"]["kind"] == "n_a" for i in subjective)


# ---------------------------------------------------------------------------
# 防锚定与理由
# ---------------------------------------------------------------------------
def test_anti_anchoring_gate(client, summary_chain):  # noqa: ANN001
    bidder_id = summary_chain["bidder_ids"][1]
    score_id = _seed_subjective(
        summary_chain, bidder_id,
        final_score=None,
        ai_suggested_score=Decimal("18"),
        ai_level_label="优",
        ai_reasoning="（测试种）建议 18 分",
        ai_confidence=0.8,
        ai_raw_json={
            "committee": [{"judge_no": 1, "score": "18", "status": "ai_suggested"}],
            "sub_scores": [{"name": "子项", "score": 9, "max": 10}],
            "artifacts_check": [{"artifact": "合同", "found": True, "quote": "见附件"}],
        },
        review_status="ai_suggested",
    )

    d = _get(client, summary_chain).json()
    item = _by_code(next(x for x in d["bidders"] if x["bidder_id"] == bidder_id),
                    SUBJECTIVE_CODE)
    assert item["ai_locked"] is True
    assert item["ai_suggested"] is None
    assert item["reason"]["ai_reasoning"] is None
    assert item["reason"]["ai_reasoning_chars"] == 0
    assert item["committee"] == []
    assert item["sub_scores"] == []
    assert item["artifacts_check"] == []
    assert score_id is not None

    # 解锁后可见（跳过盲评需理由）
    r = client.post(f"/api/scores/{score_id}/reveal",
                    json={"blind_skip": True, "reason": "测试解锁"})
    assert r.status_code == 200, r.text
    d = _get(client, summary_chain).json()
    item = _by_code(next(x for x in d["bidders"] if x["bidder_id"] == bidder_id),
                    SUBJECTIVE_CODE)
    assert item["ai_locked"] is False
    assert item["ai_suggested"] == "18.00" or Decimal(item["ai_suggested"]) == Decimal("18")
    assert item["reason"]["ai_reasoning"].startswith("（测试种）")
    assert len(item["committee"]) == 1


def test_ai_visible_when_settled(client, summary_chain):  # noqa: ANN001
    """auto_confirmed 是终态：即使从未「解锁」，理由也应可见（审计面）。"""
    bidder_id = summary_chain["bidder_ids"][2]
    _seed_subjective(
        summary_chain, bidder_id,
        final_score=Decimal("15"),
        ai_suggested_score=Decimal("15"),
        ai_reasoning="（测试种）自动裁决理由",
        review_status="auto_confirmed",
    )
    d = _get(client, summary_chain).json()
    item = _by_code(next(x for x in d["bidders"] if x["bidder_id"] == bidder_id),
                    SUBJECTIVE_CODE)
    assert item["ai_locked"] is False
    assert item["reason"]["ai_reasoning"] == "（测试种）自动裁决理由"


def test_long_reason_truncation(client, summary_chain):  # noqa: ANN001
    bidder_id = summary_chain["bidder_ids"][0]
    _seed_subjective(
        summary_chain, bidder_id,
        final_score=Decimal("12"),
        ai_reasoning="字" * 3000,
        review_status="auto_confirmed",
    )
    d = _get(client, summary_chain).json()
    item = _by_code(next(x for x in d["bidders"] if x["bidder_id"] == bidder_id),
                    SUBJECTIVE_CODE)
    assert item["reason"]["ai_reasoning_chars"] == 3000
    assert item["reason"]["truncated"] is True
    assert len(item["reason"]["ai_reasoning"]) == 1201  # 1200 + 省略号

    d2 = _get(client, summary_chain, max_reason_chars=200).json()
    item2 = _by_code(next(x for x in d2["bidders"] if x["bidder_id"] == bidder_id),
                     SUBJECTIVE_CODE)
    assert len(item2["reason"]["ai_reasoning"]) == 201

    assert _get(client, summary_chain, max_reason_chars=99999).status_code == 422


def test_human_reason_from_review_records(client, summary_chain):  # noqa: ANN001
    bidder_id = summary_chain["bidder_ids"][0]
    score_id = _seed_subjective(
        summary_chain, bidder_id, final_score=None, review_status="pending",
    )
    r = client.post(
        f"/api/scores/{score_id}/review",
        json={"action": "override", "score": "16.5", "reason": "人工复核：方案完整但缺量化指标"},
        headers={"X-Actor": "expert-zhang"},
    )
    assert r.status_code == 200, r.text

    d = _get(client, summary_chain).json()
    item = _by_code(next(x for x in d["bidders"] if x["bidder_id"] == bidder_id),
                    SUBJECTIVE_CODE)
    assert item["state"] == "scored"
    assert item["final"] == "16.50"
    assert item["reason"]["human_reason"] == "人工复核：方案完整但缺量化指标"
    assert item["reason"]["human_actor"] == "expert-zhang"
    assert item["reason"]["human_action"] == "override"
    assert item["reason"]["human_at"]


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------
def test_ownership_and_not_found(client, summary_chain):  # noqa: ANN001
    other_pid = client.post("/api/projects", json={"name": "另一个项目"}).json()["id"]
    r = client.get(f"/api/runs/{summary_chain['run_id']}/score-summary",
                   params={"project_id": other_pid})
    assert r.status_code == 404
    r = client.get("/api/runs/999999/score-summary",
                   params={"project_id": summary_chain["pid"]})
    assert r.status_code == 404


def test_run_not_ready(client, summary_chain):  # noqa: ANN001
    from app.db.models import EvaluationRun
    from app.db.session import session_scope

    with session_scope() as s:
        run = s.get(EvaluationRun, summary_chain["run_id"])
        run.status = "running"
    d = _get(client, summary_chain).json()
    assert d["run"]["ready"] is False
    assert d["run"]["incomplete_reason"]
    assert any("不完整" in w for w in d["warnings"])


def test_excluded_bidder(client, summary_chain):  # noqa: ANN001
    from app.db.models import Bidder, EvaluationRun
    from app.db.session import session_scope

    target = summary_chain["bidder_ids"][2]
    with session_scope() as s:
        name = s.get(Bidder, target).name
        run = s.get(EvaluationRun, summary_chain["run_id"])
        run.config_json = {**(run.config_json or {}),
                           "auto_dropped_bidders": [name]}

    d = _get(client, summary_chain).json()
    b = next(x for x in d["bidders"] if x["bidder_id"] == target)
    assert b["state"] == "excluded"
    assert b["rank"] is None
    assert b["buckets"] == []
    assert "自动剔除" in b["excluded_reason"]
    assert b["name"] in d["warnings"][-1] or any(b["name"] in w for w in d["warnings"])


def test_multiple_price_items_reconciliation(client, summary_chain):  # noqa: ANN001
    """多个价格项时：price_score 沿用 ranking 口径，差额显式进 other_score。"""
    from app.db.models import Criterion, ScoreRecord
    from app.db.session import session_scope

    bidder_id = summary_chain["bidder_ids"][0]
    with session_scope() as s:
        extra = Criterion(
            package_id=summary_chain["package_id"],
            code="px1", category="price", name="价格分（二）",
            max_score=Decimal("10"), score_type="formula",
            status="verified", sort_order=999,
        )
        s.add(extra)
        s.flush()
        s.add(ScoreRecord(
            run_id=summary_chain["run_id"], bidder_id=bidder_id,
            criterion_id=extra.id, max_score=Decimal("10"), score_type="formula",
            machine_score=Decimal("7"), final_score=Decimal("7"),
            machine_status="pass", review_status="pending",
        ))

    d = _get(client, summary_chain).json()
    b = next(x for x in d["bidders"] if x["bidder_id"] == bidder_id)
    price_bucket = next(x for x in b["buckets"] if x["key"] == "price")
    assert price_bucket["item_count"] == 2
    assert Decimal(b["summary"]["other_score"]) != 0
    assert b["summary"]["consistent"] is False
    assert any("不一致" in w for w in d["warnings"])


def test_retired_criterion_hidden(client, summary_chain):  # noqa: ANN001
    from app.db.models import Criterion
    from app.db.session import session_scope

    with session_scope() as s:
        crit = s.execute(
            select(Criterion).where(
                Criterion.package_id == summary_chain["package_id"],
                Criterion.code == "T5.1",
            )
        ).scalars().first()
        assert crit is not None
        crit.status = "retired"

    d = _get(client, summary_chain).json()
    assert all(i["code"] != "T5.1" for b in d["bidders"] for i in _all_items(b))
