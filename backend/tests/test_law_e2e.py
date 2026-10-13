"""法律规则包端到端：导入 → 确认 → 冻结 → 评分执行（match 第五桶 + 串通检测）。

主链用合成包（6 条：4 veto + 2 match）保证快速与 hermetic；
固化包 87 条用例（law_packs/general-law）只验导入到冻结，不跑评分，避免 87×N 记录拖慢。

核心回归护栏（对应计划 §八·2）：
- ranking warnings == []（改动前 match 会按"未定分"逐家报警）；
- 报告 open_questions 无 no_score 类（match 绝不落入"尚未定分"）；
- match 记录 final/machine_score 为 NULL、三态合法、可 confirm 不可 override；
- 串通检测命中也只到 conflict_flagged（绝不自动否决）。
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from .conftest import upload_pdf, wait_job
from .test_law_import import _doc  # 合成 6 条法律包（与导入测试同源）
from app.rules import law_kb as lk
from app.rules.library.law_pack_loader import load_law_pack, reset_law_pack_cache

REPO = Path(__file__).resolve().parents[2]
REPO_KB = REPO / "data" / "law_kb.json"


@pytest.fixture()
def law_kb(tmp_path):
    """小法条知识库（app_env 已把 data_dir 指向 tmp_path）。"""
    import app.rules.law_kb as lk

    kb = [
        {"lawName": "中华人民共和国招标投标法实施条例（2019年修订）",
         "articles": [
             {"articleNo": "第五十一条",
              "text": "有下列情形之一的，评标委员会应当否决其投标：（一）投标文件未经投标单位盖章和单位负责人签字。"},
             {"articleNo": "第四十条",
              "text": "有下列情形之一的，视为投标人相互串通投标：（一）不同投标人的投标文件由同一单位或者个人编制。"},
         ]},
        {"lawName": "中华人民共和国招标投标法（2017年修正本）",
         "articles": [{"articleNo": "第二十六条", "text": "投标人应当具备承担招标项目的能力。"}]},
    ]
    (tmp_path / "law_kb.json").write_text(json.dumps(kb, ensure_ascii=False), encoding="utf-8")
    lk.reset_cache()
    yield tmp_path / "law_kb.json"
    lk.reset_cache()


@pytest.fixture()
def law_chain(client, law_kb, samples_dir):  # noqa: ANN001
    """冻结的法律包 + 5 家样例投标人报价已确认。返回 (pid, package_id, bidder_ids)。"""
    pid = client.post("/api/projects", json={"name": "法律端到端"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/rules/import-law", json=_doc())
    assert r.status_code == 200, r.text
    package_id = r.json()["package_id"]
    assert all(v["severity"] != "error" for v in r.json()["validations"])

    assert client.post(f"/api/rules/packages/{package_id}/confirm").status_code == 200
    r = client.post(f"/api/rules/packages/{package_id}/freeze")
    assert r.status_code == 200, r.text

    bid_pdfs = sorted((samples_dir / "bids").glob("*_投标文件.pdf"))
    assert len(bid_pdfs) == 5
    bidder_ids: list[int] = []
    for pdf in bid_pdfs:
        b = client.post(f"/api/projects/{pid}/bidders",
                        json={"name": pdf.stem.split("_")[1]}).json()
        up = upload_pdf(client, pid, pdf, "bid", bidder_id=b["id"])
        job = wait_job(client, up["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        price = client.get(f"/api/bidders/{b['id']}/price").json()  # GET 触发报价抽取
        assert price["bid_price"], price
        r = client.post(f"/api/bidders/{b['id']}/price/confirm",
                        json={"reason": "与开标一览表核对一致"})
        assert r.status_code == 200, r.text
        bidder_ids.append(b["id"])
    return pid, package_id, bidder_ids


def _run(client, pid: str, package_id: int, bidder_ids: list[int]) -> dict:  # noqa: ANN001
    resp = client.post(
        f"/api/projects/{pid}/runs",
        json={"package_id": package_id, "bidder_ids": bidder_ids, "stages": ["objective"]},
    )
    assert resp.status_code == 200, resp.text
    job = wait_job(client, resp.json()["job_id"], timeout=180)
    assert job["status"] == "succeeded", job.get("error")
    return job["result_json"]


def test_full_chain_import_confirm_freeze_score(client, law_chain) -> None:  # noqa: ANN001
    pid, package_id, bidder_ids = law_chain
    summary = _run(client, pid, package_id, bidder_ids)

    # ---- 桶统计：4 veto × 5 + 2 match × 5，无价格/主观 ----
    assert summary["objective"] == 4 * 5
    assert summary["match"] == 2 * 5
    assert summary["price"] == 0
    assert summary["subjective"] == 0

    # ---- 矩阵：match 单元格存在、final/machine 为 NULL、三态合法 ----
    matrix = client.get(f"/api/runs/{summary['run_id']}/matrix").json()
    cells = [c for cols in matrix["cells"].values() for c in cols.values()]
    assert len(cells) == 6 * 5
    match_cells = [c for c in cells if c["score_type"] == "match"]
    assert len(match_cells) == 2 * 5
    for c in match_cells:
        assert c["final_score"] is None
        assert c["machine_status"] in ("need_human", "pass", "conflict_flagged")
        assert c["review_status"] in ("pending", "conflict_flagged")

    # 串通检测（REJ-009）：只到 conflict_flagged/need_human，**绝不自动否决**
    collusion = [c for c in cells if "串通" in c["criterion_name"]]
    assert len(collusion) == 5
    for c in collusion:
        assert c["machine_status"] in ("need_human", "conflict_flagged"), c
        assert c["machine_status"] != "recommend_reject"

    # ---- 排名护栏：match 不产生"未定分"警告、不计总分 ----
    ranking = client.get(f"/api/projects/{pid}/ranking").json()
    assert ranking["warnings"] == [], ranking["warnings"]
    for row in ranking["rows"]:
        assert Decimal(row["total"]) == 0  # 本包无任何分值项
        assert row["missing_final"] == []
        # breakdown 含 match 项（否则判断项在评分矩阵中彻底消失）
        assert any(b["code"].startswith("MATCH") for b in row["breakdown"])
        assert all(b["final_score"] is None for b in row["breakdown"])

    # ---- 报告：无 no_score；match 归 needs_judgment ----
    report = client.get(f"/api/projects/{pid}/report").json()
    assert report["open_questions"], "法律包全量待裁决是特性，疑问清单必然非空"
    kinds = {q.get("kind") for q in report["open_questions"]}
    assert "no_score" not in kinds, kinds  # 核心护栏：match 绝不落入"尚未定分"
    assert "needs_judgment" in kinds, kinds

    # ---- 复核动作：match confirm=结论确认（final 仍 NULL）；override=422 ----
    sid = match_cells[0]["score_record_id"]
    r = client.post(f"/api/scores/{sid}/review",
                    json={"action": "override", "score": "1", "reason": "尝试给判断项打分"})
    assert r.status_code == 422, r.text
    assert "判断项不打分" in r.json()["message"]
    r = client.post(f"/api/scores/{sid}/review",
                    json={"action": "confirm", "reason": "逐项核对投标文件后确认"})
    assert r.status_code == 200, r.text
    assert r.json()["review_status"] == "human_confirmed"
    assert r.json()["final_score"] is None


def test_import_packed_general_law(client, monkeypatch) -> None:
    """固化包 87 条：导入 → 校验 0 error → 确认 → 冻结（不跑评分，防拖慢）。"""
    monkeypatch.setenv("BID_EVAL_LAW_KB_PATH", str(REPO_KB))
    lk.reset_cache()
    reset_law_pack_cache()
    try:
        pid = client.post("/api/projects", json={"name": "固定规则包 87 条"}).json()["id"]
        doc = load_law_pack()
        r = client.post(f"/api/projects/{pid}/rules/import-law", json=doc)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["imported"] == 87
        assert len(out["dropped_nodes"]) == 5
        assert all(v["severity"] != "error" for v in out["validations"]), out["validations"]

        assert client.post(f"/api/rules/packages/{out['package_id']}/confirm").status_code == 200
        r = client.post(f"/api/rules/packages/{out['package_id']}/freeze")
        assert r.status_code == 200, r.text
        assert r.json()["content_hash"]

        cov = client.get(f"/api/rules/packages/{out['package_id']}/coverage").json()
        assert cov["law"] == 87
        assert cov["template_fallback_pending"] == 0
    finally:
        lk.reset_cache()
        reset_law_pack_cache()
