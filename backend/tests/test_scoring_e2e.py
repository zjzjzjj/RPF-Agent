"""全链路端到端测试（LLM-free）。

链路：模板实例化 → 全部签批 → 确认 → 冻结 → 5 家投标人 → 上传投标文件
→ 报价抽取（确定性五级启发，与生成侧真值精确对照）与确认 → 评分运行
（objective+price 阶段，全确定性）→ 矩阵/报告/防锚定/复核。

不再内置任何假 LLM：编译类语义抽取路径由 scripts/run_e2e.py 接真实模型冒烟
覆盖；本文件守护的是不依赖模型的确定性主干（状态机/评分/报告/审计）。
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

import pytest

from .conftest import upload_pdf, wait_job

REPO = Path(__file__).resolve().parents[2]

BIDDER_KEYS = ["jia", "yi", "bing", "ding", "wu"]  # 按 bids.py 顺序


@pytest.fixture()
def chain(client, samples_dir):  # noqa: ANN001
    """搭好全链路（LLM-free）：冻结的模板包 + 5 家报价已确认的投标人。

    返回 (pid, package_id, bidder_ids, expected)。
    """
    exp = json.loads((samples_dir / "bids" / "expected_scores.json").read_text("utf-8"))
    pid = client.post("/api/projects", json={"name": "端到端验收项目"}).json()["id"]

    # ① 模板实例化 → 逐条签批 → 确认 → 冻结（不依赖任何模型）
    r = client.post(f"/api/projects/{pid}/rules/instantiate",
                    json={"template_id": "comprehensive-goods"})
    assert r.status_code == 200, r.text
    package_id = r.json()["package_id"]
    pkg = client.get(f"/api/rules/packages/{package_id}").json()
    for c in pkg["criteria"]:
        rr = client.post(f"/api/rules/criteria/{c['id']}/signoff",
                         json={"reason": "模板通用判据作为基线启用"})
        assert rr.status_code == 200, rr.text
    r = client.post(f"/api/rules/packages/{package_id}/confirm")
    assert r.status_code == 200, r.text
    r = client.post(f"/api/rules/packages/{package_id}/freeze")
    assert r.status_code == 200, r.text
    assert r.json()["content_hash"]

    # ② 冻结后写入 409（不可变性护栏保留）
    crit_id = pkg["criteria"][0]["id"]
    resp = client.patch(f"/api/rules/criteria/{crit_id}",
                        json={"name": "篡改", "reason": "尝试修改冻结包"})
    assert resp.status_code == 409, resp.text

    # ③ 投标人与投标文件：报价抽取是 LLM-free 五级启发，必须与生成侧真值 0 差异
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
              "stages": ["objective", "price"]},  # 两阶段全确定性，无需任何模型
    )
    assert resp.status_code == 200, resp.text
    job = wait_job(client, resp.json()["job_id"], timeout=180)
    assert job["status"] == "succeeded", job.get("error")
    return job["result_json"]


def test_objective_price_run_basic(client, chain):  # noqa: ANN001
    """客观+价格阶段的确定性主干：矩阵形态、三态语义、价格分落库。"""
    pid, package_id, bidder_ids, _exp = chain
    summary = _run_evaluation(client, pid, package_id, list(bidder_ids.values()))
    assert summary["need_human"] > 0  # 人工回退模式：human_only 条款（T2.1/T2.2）留待人工

    matrix = client.get(f"/api/runs/{summary['run_id']}/matrix").json()
    assert len(matrix["bidders"]) == 5

    cells_by_bidder: dict[int, list[dict]] = {}
    for b_id, cols in matrix["cells"].items():
        for cell in cols.values():
            cells_by_bidder.setdefault(int(b_id), []).append(cell)

    # 价格分：确定性引擎已落 final_score，人工回退模式待确认
    price_cells = [c for cs in cells_by_bidder.values() for c in cs if c["category"] == "price"]
    assert len(price_cells) == 5
    assert all(c["final_score"] is not None for c in price_cells)
    assert all(c["review_status"] == "pending" for c in price_cells)

    # 计数分（T5.1/T5.2）：确定性引擎直接给 final_score
    for cs in cells_by_bidder.values():
        count_cells = [c for c in cs if c["score_type"] == "count"]
        assert len(count_cells) == 2
        assert all(c["final_score"] is not None for c in count_cells)

    # human_only 否决项：人工回退模式下绝不自动定论
    for cs in cells_by_bidder.values():
        human_only = [c for c in cs if c["criterion_name"] == "投标函已按要求签署并加盖公章"]
        assert len(human_only) == 1
        assert human_only[0]["machine_status"] == "need_human"
        assert human_only[0]["final_score"] is None


def test_anti_anchoring_reveal_and_human_override(client, chain):  # noqa: ANN001
    """防锚定与人工复核（LLM-free）：DB 直种主观分记录，走 reveal → 改分 → 排名。"""
    pid, package_id, bidder_ids, _exp = chain
    summary = _run_evaluation(client, pid, package_id, [bidder_ids["jia"]])

    from app.db.models import Criterion, ScoreRecord
    from app.db.session import session_scope

    with session_scope() as s:
        crit = s.execute(
            select(Criterion).where(Criterion.package_id == package_id,
                                    Criterion.code == "T4.1")
        ).scalars().first()
        assert crit is not None
        row = ScoreRecord(
            run_id=summary["run_id"], bidder_id=bidder_ids["jia"], criterion_id=crit.id,
            max_score=Decimal("20"), score_type="subjective", machine_status="pass",
            ai_suggested_score=Decimal("18.0"), ai_level_label="优",
            ai_reasoning="（测试直种）对照档位判据建议 18 分", ai_confidence=0.8,
            review_status="ai_suggested",
        )
        s.add(row)
        s.flush()
        score_id = row.id

    # ---- 防锚定：未解锁看不到 AI 分；未盲评必须先打分 ----
    detail = client.get(f"/api/scores/{score_id}").json()
    assert detail["ai_suggested_score"] is None
    r = client.post(f"/api/scores/{score_id}/reveal", json={"blind_skip": False, "reason": ""})
    assert r.status_code == 422  # 未盲评必须先打分
    r = client.post(f"/api/scores/{score_id}/reveal",
                    json={"blind_skip": True, "reason": "该评分项证据充分，直接参考建议"})
    assert r.status_code == 200, r.text
    detail = client.get(f"/api/scores/{score_id}").json()
    assert Decimal(str(detail["ai_suggested_score"])) == Decimal("18.0")

    # ---- 复核：人工改分必填理由并留痕，排名随复核更新 ----
    r = client.post(
        f"/api/scores/{score_id}/review",
        json={"action": "override", "score": "18.5", "reason": "方案量化指标完整，按高档中值给分"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["review_status"] == "human_overridden"
    assert Decimal(r.json()["final_score"]) == Decimal("18.5")

    ranking2 = client.get(f"/api/projects/{pid}/ranking").json()
    rows2 = {x["bidder_id"]: x for x in ranking2["rows"]}
    assert Decimal(rows2[bidder_ids["jia"]]["tech_total"]) == Decimal("18.5")


# ---------------------------------------------------------------------------
# 报告草稿/定稿：先出报告、把疑问标出，复核后刷新即为定稿
# ---------------------------------------------------------------------------
def test_report_is_draft_then_final(client, chain):  # noqa: ANN001
    """报告不再等复核完成才可生成；疑问清零且运行成功才转定稿。

    这一组断言是回归护栏：把 report_status 恒置 final、或删掉模板里的草稿横幅，
    都必须让测试变红。
    """
    pid, package_id, bidder_ids, _exp = chain
    summary = _run_evaluation(client, pid, package_id, list(bidder_ids.values()))

    # ---- ① 未复核 → 草稿，且疑问被逐条标出 ----
    report = client.get(f"/api/projects/{pid}/report").json()
    assert report["status"] == "draft", report.get("status_label")
    assert report["progress"]["open"] > 0
    assert report["open_questions"], "草稿必须列出疑问清单"
    assert report["open_by_criterion"], "疑问必须能按评分项聚合（批量裁决入口）"
    for q in report["open_questions"]:
        assert q["severity"] in ("high", "medium", "low")
        assert q["action"]
        assert q["label"]

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
    # 在本文件其他用例里已有覆盖，不必在此重复 150 次 API 写。
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
