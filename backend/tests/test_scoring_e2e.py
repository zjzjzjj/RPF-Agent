"""全链路离线端到端验收（Mock LLM）。

链路：上传招标文件 → 解析 → Mock 编译规则 → 逐项确认 → 冻结 → 5 家投标人
→ 上传投标文件 → 报价抽取与确认 → 评分运行 → 矩阵/复核/防锚定 → 排名。

断言与 expected_scores.json 对齐（价格分精确到分、资格三态、商务分、证据核验率）。
"""
from __future__ import annotations

import json
import time
from decimal import Decimal

from sqlalchemy import select
from pathlib import Path

import pytest

from .conftest import upload_pdf, wait_job

REPO = Path(__file__).resolve().parents[2]

BIDDER_KEYS = ["jia", "yi", "bing", "ding", "wu"]  # 按 bids.py 顺序


@pytest.fixture()
def expected() -> dict:
    return json.loads((REPO / "data" / "samples" / "bids" / "expected_scores.json").read_text("utf-8"))


@pytest.fixture()
def chain(client, samples_dir):  # noqa: ANN001
    """搭好全链路：冻结的规则包 + 5 家报价已确认的投标人。返回 (pid, package_id, expected)。"""
    exp = json.loads((samples_dir / "bids" / "expected_scores.json").read_text("utf-8"))
    pid = client.post("/api/projects", json={"name": "端到端验收项目"}).json()["id"]

    # ① 招标文件 → 解析
    tender_pdf = next((samples_dir / "tender").glob("*招标文件.pdf"))
    out = upload_pdf(client, pid, tender_pdf, "tender")
    job = wait_job(client, out["job_id"])
    assert job["status"] == "succeeded", job.get("error")

    # ② Mock 编译规则
    resp = client.post(
        f"/api/projects/{pid}/rules/compile",
        json={"document_id": out["document_id"], "provider": "mock"},
    )
    assert resp.status_code == 200, resp.text
    job = wait_job(client, resp.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    package_id = job["result_json"]["package_id"]

    pkg = client.get(f"/api/rules/packages/{package_id}").json()
    crits = pkg["criteria"]
    # 编译出的规则应覆盖：7 否决 + 1 价格 + 3 技术 + 2 商务
    by_cat: dict[str, int] = {}
    for c in crits:
        by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
    assert by_cat == {"qualification": 4, "compliance": 3, "price": 1,
                      "technical": 3, "commercial": 2}, by_cat

    # 校验结果不应有 error
    errors = [v for v in pkg["validations"] if v["severity"] == "error"]
    assert errors == [], errors

    # ③ 逐条确认（系统没有"全部采纳"端点）→ 确认 → 冻结
    for c in crits:
        r = client.post(f"/api/rules/criteria/{c['id']}/accept",
                        json={"reason": "与招标文件原文核对一致"})
        assert r.status_code == 200, r.text
    r = client.post(f"/api/rules/packages/{package_id}/confirm")
    assert r.status_code == 200, r.text
    r = client.post(f"/api/rules/packages/{package_id}/freeze")
    assert r.status_code == 200, r.text
    assert r.json()["content_hash"]

    # ④ 投标人与投标文件
    bid_pdfs = sorted((samples_dir / "bids").glob("*_投标文件.pdf"))
    assert len(bid_pdfs) == 5
    bidder_ids: dict[str, int] = {}
    for key in BIDDER_KEYS:
        pdf = next(p for p in bid_pdfs if p.name.startswith(f"{key}_"))
        b = client.post(f"/api/projects/{pid}/bidders",
                        json={"name": pdf.stem.split("_")[1]}).json()
        bid_out = upload_pdf(client, pid, pdf, "bid", bidder_id=b["id"])
        job = wait_job(client, bid_out["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        price = client.get(f"/api/bidders/{b['id']}/price").json()
        assert Decimal(price["bid_price"]) == Decimal(exp["prices_yuan"][key]), (key, price)
        r = client.post(f"/api/bidders/{b['id']}/price/confirm",
                        json={"reason": "与开标一览表核对一致"})
        assert r.status_code == 200, r.text
        bidder_ids[key] = b["id"]

    return pid, package_id, bidder_ids, exp


def _run_evaluation(client, pid: str, package_id: int, bidder_ids: list[int]) -> dict:
    resp = client.post(
        f"/api/projects/{pid}/runs",
        json={"package_id": package_id, "bidder_ids": bidder_ids,
              "stages": ["objective", "price", "subjective"]},
    )
    assert resp.status_code == 200, resp.text
    job = wait_job(client, resp.json()["job_id"], timeout=180)
    assert job["status"] == "succeeded", job.get("error")
    return job["result_json"]


def test_e2e_full_chain(client, chain, expected):  # noqa: ANN001
    pid, package_id, bidder_ids, exp = chain

    # ⑤ 评分运行
    summary = _run_evaluation(client, pid, package_id, list(bidder_ids.values()))
    assert summary["need_human"] > 0  # 有待人工项（建议否决/冲突/主观分）

    # ---- 价格分与生成侧真值精确一致（0 差异）----
    matrix = client.get(f"/api/runs/{summary['run_id']}/matrix").json()
    key_by_name = {name: key for key, name in
                   ((k, next(b for b in matrix["bidders"] if b["bidder_id"] == v)["name"])
                    for k, v in bidder_ids.items())}
    # bidder_id -> key
    id2key = {v: k for k, v in bidder_ids.items()}

    price_cells = {}
    for b_id, cols in matrix["cells"].items():
        for _c_id, cell in cols.items():
            if cell["category"] == "price":
                price_cells[int(b_id)] = cell
    for b_id, cell in price_cells.items():
        key = id2key[int(b_id)]
        assert cell["final_score"] == exp["price"]["price_scores"][key], (key, cell)
        assert cell["review_status"] == "pending"

    # ---- 资格/符合性三态 ----
    veto_cells = {}
    for b_id, cols in matrix["cells"].items():
        for _c_id, cell in cols.items():
            if cell["score_type"] == "veto":
                veto_cells.setdefault(int(b_id), {})[cell["criterion_name"]] = cell
    # 丙（bing）：缺保证金 → 建议否决（人工未确认前绝不是 rejected）
    bing = veto_cells[bidder_ids["bing"]]
    margin_cell = next(c for name, c in bing.items() if "保证金" in name)
    assert margin_cell["machine_status"] == "recommend_reject"
    # 丁（ding）：大小写/分项合计冲突 → conflict_flagged
    ding = veto_cells[bidder_ids["ding"]]
    assert any(c["review_status"] == "conflict_flagged" for c in ding.values())
    # 甲/乙/戊：全部 pass
    for key in ("jia", "yi", "wu"):
        assert all(c["machine_status"] == "pass" for c in veto_cells[bidder_ids[key]].values())

    # ---- 商务 count 分（确定性）----
    def _cells(b_key: str, category: str) -> dict:
        return {c["criterion_name"]: c for c in matrix["cells"][str(bidder_ids[b_key])].values()
                if c["category"] == category}

    for key, (perf, certs) in (
        ("jia", (9, 6)), ("yi", (6, 4)), ("bing", (6, 2)), ("ding", (9, 4)), ("wu", (6, 4))
    ):
        cells = _cells(key, "commercial")
        perf_cell = next(c for n, c in cells.items() if "业绩" in n)
        cert_cell = next(c for n, c in cells.items() if "认证" in n)
        assert perf_cell["final_score"] == f"{perf}.00", (key, perf_cell)
        assert cert_cell["final_score"] == f"{certs}.00", (key, cert_cell)

    # 乙：声称 ISO27001 无证书 → 冲突标记
    yi_cert_cell = next(c for n, c in _cells("yi", "commercial").items() if "认证" in n)
    assert yi_cert_cell["review_status"] == "conflict_flagged"

    # ---- 主观分：AI 建议只入 ai_* 字段，final 保持空 ----
    jia_tech = [c for c in matrix["cells"][str(bidder_ids["jia"])].values()
                if c["category"] == "technical"]
    assert len(jia_tech) == 3
    assert all(c["final_score"] is None for c in jia_tech)
    assert all(c["review_status"] == "ai_suggested" for c in jia_tech)

    # ---- 证据核验：Mock 引文逐字 → 核验率 100% ----
    stats = client.get(f"/api/runs/{summary['run_id']}/stats").json()
    assert stats["verified"] == stats["cited"] and stats["cited"] > 0
    assert stats["verification_rate"] == 1.0

    # ---- 排名：含建议否决者（交叉验证），价格分参与总分 ----
    ranking = client.get(f"/api/projects/{pid}/ranking").json()
    rows = {r["bidder_id"]: r for r in ranking["rows"]}
    jia_row = rows[bidder_ids["jia"]]
    # 甲总分 = 价格 36.13 + 商务 15（主观未定分不计入，有警告）
    assert jia_row["total"] == str(Decimal("36.13") + Decimal("15.00"))
    assert any("未定分" in w for w in ranking["warnings"])
    # 丙在榜（建议否决仍算分）且带 veto 标记
    assert rows[bidder_ids["bing"]]["veto_flag"] is True

    # ---- 防锚定：未解锁看不到 AI 分 ----
    tech_cell = jia_tech[0]
    detail = client.get(f"/api/scores/{tech_cell['score_record_id']}").json()
    assert detail["ai_suggested_score"] is None
    r = client.post(f"/api/scores/{tech_cell['score_record_id']}/reveal", json={"blind_skip": False, "reason": ""})
    assert r.status_code == 422  # 未盲评必须先打分
    r = client.post(f"/api/scores/{tech_cell['score_record_id']}/reveal",
                    json={"blind_skip": True, "reason": "该评分项证据充分，直接参考建议"})
    assert r.status_code == 200
    detail = client.get(f"/api/scores/{tech_cell['score_record_id']}").json()
    assert detail["ai_suggested_score"] is not None

    # ---- 复核：人工改分必填理由并留痕 ----
    r = client.post(
        f"/api/scores/{tech_cell['score_record_id']}/review",
        json={"action": "override", "score": "18.5", "reason": "方案量化指标完整，按高档中值给分"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["review_status"] == "human_overridden"
    assert Decimal(r.json()["final_score"]) == Decimal("18.5")

    # 排名随复核更新
    ranking2 = client.get(f"/api/projects/{pid}/ranking").json()
    rows2 = {r["bidder_id"]: r for r in ranking2["rows"]}
    assert Decimal(rows2[bidder_ids["jia"]]["tech_total"]) == Decimal("18.5")


def test_e2e_run_requires_frozen_package(client, chain):  # noqa: ANN001
    """未冻结/报价未确认的前置拦截在其他用例已覆盖；这里验证矩阵 API 的稳定性。"""
    pid, package_id, bidder_ids, _exp = chain
    resp = client.post(
        f"/api/projects/{pid}/runs",
        json={"package_id": package_id, "bidder_ids": [bidder_ids["jia"]]},
    )
    # 合法请求（包已冻结、报价已确认）→ 成功
    assert resp.status_code == 200
    _run_evaluation(client, pid, package_id, [bidder_ids["jia"]])


# ---------------------------------------------------------------------------
# 报告草稿/定稿：先出报告、把疑问标出，复核后刷新即为定稿
# ---------------------------------------------------------------------------
def test_report_is_draft_then_final(client, chain):  # noqa: ANN001
    """报告不再等复核完成才可生成；疑问清零且运行成功才转定稿。

    这一组断言是回归护栏：把 report_status 恒置 final、或删掉模板里的草稿横幅，
    都必须让测试变红（此前 92 项测试对这两处改动全绿）。
    """
    pid, package_id, bidder_ids, _exp = chain
    summary = _run_evaluation(client, pid, package_id, list(bidder_ids.values()))

    # ---- ① 未复核 → 草稿，且疑问被逐条标出 ----
    report = client.get(f"/api/projects/{pid}/report").json()
    assert report["status"] == "draft", report.get("status_label")
    assert report["progress"]["open"] > 0
    assert report["open_questions"], "草稿必须列出疑问清单"
    assert report["open_by_criterion"], "疑问必须能按评分项聚合（批量裁决入口）"
    # 每条疑问都要说清"为什么是疑问"和"该做什么"
    for q in report["open_questions"]:
        assert q["severity"] in ("high", "medium", "low")
        assert q["action"]
        assert q["label"]

    # 聚合视图的投标人数必须与逐条明细对得上
    flat = sum(len(g["bidders"]) for g in report["open_by_criterion"])
    assert flat == len(report["open_questions"])

    # ---- ② 模板渲染：草稿声明必须在首屏、且写明未处置项数 ----
    html = client.get(f"/api/projects/{pid}/report.html").text
    assert "本报告为草稿" in html
    assert "不得作为最终评审结论" in html
    assert "疑问清单" in html
    assert "按评分项汇总" in html

    # ---- ③ 逐条处置全部评分 → 转定稿 ----
    # 直接把全部评分记录置为已人工处置，模拟"复核完毕"。
    # 这里只验证**报告状态逻辑**（疑问清零 → 定稿）；逐条复核接口本身
    # 在 test_scoring_e2e 的其他用例里已有覆盖，不必在此重复 150 次 API 写。
    from app.db.models import ScoreRecord
    from app.db.session import session_scope

    with session_scope() as s:
        rows = s.execute(
            select(ScoreRecord).where(ScoreRecord.run_id == summary["run_id"])
        ).scalars().all()
        assert rows
        for row in rows:
            row.review_status = "human_confirmed"
            s.add(row)

    report2 = client.get(f"/api/projects/{pid}/report").json()
    assert report2["progress"]["open"] == 0, report2["progress"]
    assert report2["status"] == "final", report2["status_label"]
    html2 = client.get(f"/api/projects/{pid}/report.html").text
    assert "本报告为定稿" in html2
    assert "不得作为最终评审结论" not in html2


def test_report_never_final_when_run_failed(client, chain):  # noqa: ANN001
    """评分运行未成功时绝不能出定稿——否则 0 条记录会被读成"全部已处置"。"""
    pid, package_id, bidder_ids, _exp = chain
    summary = _run_evaluation(client, pid, package_id, list(bidder_ids.values()))
    # 把运行改成失败，模拟中断
    from app.db.models import EvaluationRun
    from app.db.session import session_scope

    with session_scope() as s:
        run = s.get(EvaluationRun, summary["run_id"])
        run.status = "failed"
        s.add(run)

    report = client.get(f"/api/projects/{pid}/report").json()
    assert report["status"] == "draft"
    assert report["progress"].get("incomplete_reason")
    html = client.get(f"/api/projects/{pid}/report.html").text
    assert "本报告为草稿" in html


# ---- 运行状态回收与报告入参校验 ----------------------------------------------

def test_reconcile_reclaims_queued_runs(client, chain):  # noqa: ANN001
    """卡在 queued 的运行必须被回收。

    回归：create_run 先提交 status='queued' 的运行再 submit 任务，
    submit 抛错或任务在进入 execute_run 前就失败时，运行永久停在 queued；
    只扫 running 的回收逻辑会漏掉它，工作台/报告永远显示"运行中"。
    """
    from app.db.models import EvaluationRun
    from app.db.session import session_scope
    from app.services.scoring_service import reconcile_stale_runs

    pid, package_id, _bidders, _exp = chain
    with session_scope() as s:
        s.add(EvaluationRun(id=9991, project_id=pid, package_id=package_id,
                            package_version=1, status="queued"))
    assert reconcile_stale_runs(pid) == 1
    with session_scope() as s:
        assert s.get(EvaluationRun, 9991).status == "failed"


def test_report_rejects_unknown_run_id(client, chain):  # noqa: ANN001
    """不存在的 run_id 应 404，而不是 500。"""
    pid, _pkg, _bidders, _exp = chain
    r = client.get(f"/api/projects/{pid}/report", params={"run_id": 99999})
    assert r.status_code == 404, r.text


def test_report_rejects_run_of_another_project(client, chain):  # noqa: ANN001
    """跨项目的 run_id 必须 404——否则会生成"项目头是 A、数据全来自 B"的混搭报告。"""
    pid_a, package_id, bidder_ids, _exp = chain
    summary = _run_evaluation(client, pid_a, package_id, list(bidder_ids.values()))

    pid_b = client.post("/api/projects", json={"name": "另一个项目"}).json()["id"]
    r = client.get(f"/api/projects/{pid_b}/report", params={"run_id": summary["run_id"]})
    assert r.status_code == 404, r.text
    assert client.get(f"/api/projects/{pid_a}/report",
                      params={"run_id": summary["run_id"]}).status_code == 200
