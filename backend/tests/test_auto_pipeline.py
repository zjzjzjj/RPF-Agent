"""全自动模式端到端测试（BID_EVAL_AUTO_MODE=1，默认生产语义）。

与 test_scoring_e2e / test_law_e2e（人工回退模式，conftest 显式关 auto）成对。
本文件用**全确定性且对样例标书全部通过**的合成迷你法规包，离线验证自动收敛的
结构保证——
- 法规包 confirm/freeze 的阻断项自动采纳；
- 报价自动确认（无报价的投标人自动剔除留痕）；
- 评分运行零 need_human/conflict_flagged 残留，全部收敛 auto_confirmed；
- 报告直接 final。

说明：human_only 法条（87 条真实法规包中占 72 条）与冲突信号的 LLM 带证据
裁决收敛属真实模型行为，不再由任何内置假 LLM 覆盖——由
``scripts/run_auto_pipeline.py --assert-final``（接真实模型）验收。
"""
from __future__ import annotations

import json

import pytest

from .conftest import upload_pdf, wait_job
from app.rules import law_kb as lk

LAW51 = (
    "有下列情形之一的，评标委员会应当否决其投标：（一）投标文件未经投标单位盖章和单位负责人签字；"
)
LAW26 = (
    "投标人应当具备承担招标项目的能力；国家有关规定对投标人资格条件有规定的，"
    "投标人应当具备规定的资格条件。"
)
NODE_51 = "法条://中华人民共和国招标投标法实施条例（2019年修订）/第五十一条"
NODE_26 = "法条://中华人民共和国招标投标法（2017年修正本）/第二十六条"


def _mini_law_pack() -> dict:
    """全确定性、对 5 家样例标书全部通过：投标函章节每家都有（document_present），
    业绩证明材料每家 ≥1 份（count_at_least）。"""
    return {
        "package": {"name": "全自动收敛迷你法规包", "version": "0.1.0", "source": "LAW",
                    "total_max": 100, "status": "draft"},
        "criteria": [
            {"code": "REJ-T1", "category": "compliance", "name": "投标文件应包含投标函",
             "score_type": "veto", "is_veto": True, "veto_consequence": "disqualify",
             "description": "投标文件须包含投标函章节，否则否决投标。",
             "max_score": None, "weight": None, "levels": [],
             "check_type": "document_present", "params": {"document": "投标函"},
             "source_quote": LAW51, "source_node_id": NODE_51},
            {"code": "QUAL-T1", "category": "qualification", "name": "类似业绩证明材料完整性",
             "score_type": "veto", "is_veto": True, "veto_consequence": "disqualify",
             "description": "业绩须提供合同关键页及中标通知书或验收证明。",
             "max_score": None, "weight": None, "levels": [],
             "check_type": "count_at_least", "params": {"field": "performance", "min_count": 1},
             "source_quote": LAW26, "source_node_id": NODE_26},
        ],
    }


@pytest.fixture()
def law_kb(tmp_path):
    """小法条知识库写入 data_dir/law_kb.json（app_env 已把 data_dir 指向 tmp_path）。"""
    kb = [
        {"lawName": "中华人民共和国招标投标法实施条例（2019年修订）",
         "articles": [{"articleNo": "第五十一条", "text": LAW51}]},
        {"lawName": "中华人民共和国招标投标法（2017年修正本）",
         "articles": [{"articleNo": "第二十六条", "text": LAW26}]},
    ]
    path = tmp_path / "law_kb.json"
    path.write_text(json.dumps(kb, ensure_ascii=False), encoding="utf-8")
    lk.reset_cache()
    yield path
    lk.reset_cache()


@pytest.fixture()
def auto_client(app_env, monkeypatch):  # noqa: ANN001
    """在 app_env 隔离环境上显式开启全自动模式（provider 保持未配置，全程离线）。"""
    monkeypatch.setenv("BID_EVAL_AUTO_MODE", "1")
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def frozen_auto_chain(auto_client, law_kb):  # noqa: ANN001
    """导入迷你法规包 → 确认（阻断项自动采纳）→ 冻结。返回 (pid, package_id)。"""
    c = auto_client
    pid = c.post("/api/projects", json={"name": "全自动链路"}).json()["id"]
    r = c.post(f"/api/projects/{pid}/rules/import-law", json=_mini_law_pack())
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 2
    package_id = r.json()["package_id"]

    assert c.post(f"/api/rules/packages/{package_id}/confirm").status_code == 200, \
        "全自动模式下阻断项应已被自动采纳"
    fr = c.post(f"/api/rules/packages/{package_id}/freeze")
    assert fr.status_code == 200, fr.text
    return pid, package_id


def test_auto_pipeline_full_chain(auto_client, frozen_auto_chain, samples_dir) -> None:  # noqa: ANN001
    c, (pid, package_id) = auto_client, frozen_auto_chain

    bidder_ids: list[int] = []
    for pdf in sorted((samples_dir / "bids").glob("*_投标文件.pdf")):
        b = c.post(f"/api/projects/{pid}/bidders",
                   json={"name": pdf.stem.split("_")[1]}).json()
        up = upload_pdf(c, pid, pdf, "bid", bidder_id=b["id"])
        job = wait_job(c, up["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        price = c.get(f"/api/bidders/{b['id']}/price").json()
        assert price["bid_price"], price  # 无报价会被评分入口自动剔除，这里样例都有
        bidder_ids.append(b["id"])

    resp = c.post(f"/api/projects/{pid}/runs", json={
        "package_id": package_id,
        "bidder_ids": bidder_ids,
        "stages": ["objective"],  # 客观阶段全确定性；模糊项裁决由真实模型验收覆盖
    })
    assert resp.status_code == 200, resp.text
    job = wait_job(c, resp.json()["job_id"], timeout=180)
    assert job["status"] == "succeeded", job.get("error")
    summary = job["result_json"]

    # ---- 核心护栏：零人工残留，全部收敛 auto_confirmed ----
    assert summary["need_human"] == 0, summary
    matrix = c.get(f"/api/runs/{summary['run_id']}/matrix").json()
    cells = [cell for cols in matrix["cells"].values() for cell in cols.values()]
    assert cells
    bad = [x for x in cells if x["review_status"] not in ("auto_confirmed", "not_applicable")]
    assert not bad, [(x["criterion_name"], x["review_status"]) for x in bad[:3]]

    # ---- 报告：直接定稿，无待裁决疑问 ----
    report = c.get(f"/api/projects/{pid}/report").json()
    assert report["status"] == "final", report["status"]
    assert report["open_questions"] == []


def test_auto_mode_confirms_prices_without_manual_step(auto_client, frozen_auto_chain,
                                                       samples_dir) -> None:  # noqa: ANN001
    """报价未经人工确认也能进评分（全自动确认并留痕）。"""
    c, (pid, package_id) = auto_client, frozen_auto_chain
    pdf = sorted((samples_dir / "bids").glob("*_投标文件.pdf"))[0]
    b = c.post(f"/api/projects/{pid}/bidders", json={"name": pdf.stem.split("_")[1]}).json()
    up = upload_pdf(c, pid, pdf, "bid", bidder_id=b["id"])
    job = wait_job(c, up["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    c.get(f"/api/bidders/{b['id']}/price")  # 触发抽取（不调用 confirm）

    # 抽取后仍是 extracted（未人工确认）……
    bidder = c.get(f"/api/projects/{pid}/bidders").json()
    target = next(x for x in bidder if x["id"] == b["id"])
    assert target["price_status"] in ("extracted", "confirmed")

    # ……但评分入口自动确认，不 422；确认动作已在评分记录的终态里体现
    resp = c.post(f"/api/projects/{pid}/runs",
                  json={"package_id": package_id, "bidder_ids": [b["id"]],
                        "stages": ["objective"]})
    assert resp.status_code == 200, resp.text
    job = wait_job(c, resp.json()["job_id"], timeout=180)
    assert job["status"] == "succeeded", job.get("error")
    matrix = c.get(f"/api/runs/{job['result_json']['run_id']}/matrix").json()
    cells = [cell for cols in matrix["cells"].values() for cell in cols.values()]
    assert cells and all(x["review_status"] == "auto_confirmed" for x in cells)
