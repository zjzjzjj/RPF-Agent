"""全自动模式端到端测试（BID_EVAL_AUTO_MODE=1，默认生产语义）。

以读取真实样例页并返回逐字引文的受控 provider 验证资格 AI 结论、完整矩阵和
报告门槛。缺少模型或视觉能力必须明确失败，保留已完成记录且不生成假分。
不符合的投标人仍进入排名；缺少外部记录可以形成明确的证据边界结论。
"""
from __future__ import annotations

import json
import re
from decimal import Decimal

import pytest

from .conftest import upload_pdf, wait_job
from app.rules import law_kb as lk
from app.llm.base import LLMResult

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
    """样例都含投标函与业绩清单；AI 仍须读取原页并核实。"""
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
            {"code": "QUAL-T1", "category": "qualification", "name": "类似项目业绩清单",
             "score_type": "veto", "is_veto": True, "veto_consequence": "disqualify",
             "description": "投标文件须至少列示一项类似项目业绩。",
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
def evidence_provider(monkeypatch):
    """Exercise the real review/gateway/citation chain using controlled, sourced replies."""
    from app.llm import registry

    class EvidenceProvider:
        name, model, host = "offline-test", "evidence-test", "localhost"

        def __init__(self):
            self.calls = []

        def complete(self, system, user, **kwargs):
            assert user.startswith("<review>\n"), "Unexpected model task must not be faked"
            payload = json.loads(user.split("\n", 1)[1].rsplit("\n", 1)[0])
            self.calls.append(payload)
            code = payload["rule"]["code"]
            if code == "EXT-T1":
                reply = {"verdict": "unverifiable", "reasoning": "现有材料无法证实平台递交时间，缺少平台递交回执。",
                         "missing_evidence": ["平台递交回执"], "evidence": []}
            else:
                token = {"REJ-T1": "投标函", "QUAL-T1": "类似项目业绩",
                         "QUAL-CAPITAL": "注册资本", "REJ-T2": "法定代表人"}[code]
                page = next(p for p in payload["pages"] if token in p["text"])
                start = max(0, page["text"].index(token) - 20)
                quote = page["text"][start:start + 200]
                verdict = "pass"
                reasoning = f"原页可定位{token}及对应内容。"
                if code == "QUAL-CAPITAL":
                    capital = re.search(r"注册资本[：:]\s*人民币\s*([\d,，.]+)\s*万元", page["text"])
                    assert capital, "Capital failure must be grounded in an actual amount"
                    assert Decimal(capital[1].replace(",", "").replace("，", "")) < Decimal("100000000")
                    verdict, reasoning = "fail", "营业执照列示的注册资本低于冻结细则要求的100000000万元。"
                reply = {"verdict": verdict, "reasoning": reasoning, "evidence": [{
                    "document_id": page["document_id"], "page_no": page["page_no"], "quote": quote,
                }]}
            return LLMResult(text=json.dumps(reply, ensure_ascii=False), provider=self.name, model=self.model)

    provider = EvidenceProvider()
    monkeypatch.setattr(registry, "get_provider", lambda *args, **kwargs: provider)
    return provider


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


def test_auto_pipeline_full_chain(auto_client, frozen_auto_chain, samples_dir, evidence_provider) -> None:  # noqa: ANN001
    c, (pid, package_id) = auto_client, frozen_auto_chain

    bidder_ids: list[int] = []
    for pdf in sorted((samples_dir / "bids").glob("*_投标文件.pdf")):
        b = c.post(f"/api/projects/{pid}/bidders",
                   json={"name": pdf.stem.split("_")[1]}).json()
        up = upload_pdf(c, pid, pdf, "bid", bidder_id=b["id"])
        job = wait_job(c, up["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        price = c.get(f"/api/bidders/{b['id']}/price").json()
        assert price["bid_price"], price
        bidder_ids.append(b["id"])

    resp = c.post(f"/api/projects/{pid}/runs", json={
        "package_id": package_id,
        "bidder_ids": bidder_ids,
        "stages": ["objective"],
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
    assert len(cells) == len(bidder_ids) * 2
    assert all(cell["evidence_status"] == "verified" for cell in cells)
    assert {call["rule"]["code"] for call in evidence_provider.calls} == {"REJ-T1", "QUAL-T1"}
    progress = c.get(f"/api/runs/{summary['run_id']}").json()["automation"]
    assert progress["completed"] == progress["expected"] == len(cells)
    assert progress["remaining"] == progress["failed"] == 0

    # ---- 报告：直接定稿，无待裁决疑问 ----
    report = c.get(f"/api/projects/{pid}/report").json()
    assert report["status"] == "final", report["status"]
    assert report["open_questions"] == []


def test_auto_mode_confirms_prices_without_manual_step(auto_client, frozen_auto_chain,
                                                       samples_dir, evidence_provider) -> None:  # noqa: ANN001
    """已抽取报价未经人工确认，也可执行不含价格分的自动资格评审。"""
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

    # 自动资格评审无需人工确认；价格分会由独立报价核验阶段处理。
    resp = c.post(f"/api/projects/{pid}/runs",
                  json={"package_id": package_id, "bidder_ids": [b["id"]],
                        "stages": ["objective"]})
    assert resp.status_code == 200, resp.text
    job = wait_job(c, resp.json()["job_id"], timeout=180)
    assert job["status"] == "succeeded", job.get("error")
    matrix = c.get(f"/api/runs/{job['result_json']['run_id']}/matrix").json()
    cells = [cell for cols in matrix["cells"].values() for cell in cols.values()]
    assert cells and all(x["review_status"] == "auto_confirmed" for x in cells)


def _import_extra_rule(c, extra):
    pack = _mini_law_pack()
    pack["criteria"].append(extra)
    pid = c.post("/api/projects", json={"name": "全自动资格评审"}).json()["id"]
    r = c.post(f"/api/projects/{pid}/rules/import-law", json=pack)
    assert r.status_code == 200, r.text
    package_id = r.json()["package_id"]
    assert c.post(f"/api/rules/packages/{package_id}/confirm").status_code == 200
    fr = c.post(f"/api/rules/packages/{package_id}/freeze")
    assert fr.status_code == 200, fr.text
    return pid, package_id


def _one_bidder(c, pid, samples_dir):
    pdf = sorted((samples_dir / "bids").glob("*_投标文件.pdf"))[0]
    b = c.post(f"/api/projects/{pid}/bidders", json={"name": pdf.stem.split("_")[1]}).json()
    up = upload_pdf(c, pid, pdf, "bid", bidder_id=b["id"])
    job = wait_job(c, up["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    c.get(f"/api/bidders/{b['id']}/price")
    return b["id"]


def _evaluate(c, pid, package_id, bidder_id):
    resp = c.post(f"/api/projects/{pid}/runs", json={
        "package_id": package_id, "bidder_ids": [bidder_id], "stages": ["objective"],
        # These legacy-contract tests assert stop-on-error semantics; the API
        # default is now the partial/continue policy.
        "failure_policy": "stop",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["run_id"], wait_job(c, resp.json()["job_id"], timeout=180)


def _cells(c, run_id):
    matrix = c.get(f"/api/runs/{run_id}/matrix").json()
    return [cell for cols in matrix["cells"].values() for cell in cols.values()]


def _human_rule(code, name, description):
    return {"code": code, "category": "compliance", "name": name,
            "score_type": "veto", "is_veto": True, "veto_consequence": "disqualify",
            "description": description, "max_score": None, "weight": None, "levels": [],
            "check_type": "human_only", "params": {}, "source_quote": LAW51, "source_node_id": NODE_51}


def test_missing_qualification_model_fails_without_final_report(auto_client, frozen_auto_chain, samples_dir):
    c, (pid, package_id) = auto_client, frozen_auto_chain
    bidder_id = _one_bidder(c, pid, samples_dir)
    run_id, job = _evaluate(c, pid, package_id, bidder_id)
    assert job["status"] == "failed", job
    run = c.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "failed"
    assert run["automation"]["remaining"] > 0 and run["automation"]["resumable"]
    cells = _cells(c, run_id)
    assert cells and all(cell["final_score"] is None for cell in cells)
    assert any(cell["machine_status"] == "execution_failed" for cell in cells)
    report = c.get(f"/api/projects/{pid}/report").json()
    assert report["status"] == "draft" and report["progress"]["blocking_open"] > 0
    assert c.get(f"/api/projects/{pid}/report/formal.html").status_code == 422
    ranking = c.get(f"/api/projects/{pid}/ranking").json()
    assert not ranking["is_final"] and all(row["rank"] is None for row in ranking["rows"])


def test_signature_requires_configured_vision_and_preserves_completed_cells(
    auto_client, law_kb, samples_dir, evidence_provider, monkeypatch,
):
    from app.services import automatic_scoring_service as scoring

    # OCR is orthogonal here; preserve the actual pages, real rendering and visual capability checks.
    original_supplement = scoring._supplement_pages
    monkeypatch.setattr(scoring, "_supplement_pages", lambda rule, pages, visual, **kwargs:
                        original_supplement(rule, pages, visual=True, **kwargs) if visual else pages)
    c = auto_client
    pid, package_id = _import_extra_rule(c, _human_rule(
        "REJ-T2", "投标文件签章", "投标文件须按要求签章，否则不符合要求。",
    ))
    run_id, job = _evaluate(c, pid, package_id, _one_bidder(c, pid, samples_dir))
    assert job["status"] == "failed", job
    assert "BID_EVAL_VISION_MODEL" in str(job.get("error_json") or job.get("error") or job)
    cells = _cells(c, run_id)
    signature = next(cell for cell in cells if cell["criterion_code"] == "REJ-T2")
    assert signature["machine_status"] == "execution_failed" and signature["final_score"] is None
    assert len([cell for cell in cells if cell["review_status"] == "auto_confirmed"]) == 2
    assert c.get(f"/api/projects/{pid}/report").json()["status"] == "draft"


def test_ai_noncompliance_is_final_and_bidder_remains_ranked(auto_client, law_kb, samples_dir, evidence_provider):
    c = auto_client
    pid, package_id = _import_extra_rule(c, _human_rule(
        "QUAL-CAPITAL", "营业执照注册资本门槛", "注册资本不得低于人民币100000000万元。",
    ))
    bidder_id = _one_bidder(c, pid, samples_dir)
    run_id, job = _evaluate(c, pid, package_id, bidder_id)
    assert job["status"] == "succeeded", job
    failed = next(cell for cell in _cells(c, run_id) if cell["criterion_code"] == "QUAL-CAPITAL")
    assert failed["machine_status"] == "fail" and failed["review_status"] == "auto_confirmed"
    assert failed["evidence_status"] == "verified"
    ranking = c.get(f"/api/projects/{pid}/ranking").json()
    assert ranking["is_final"] and ranking["rows"][0]["bidder_id"] == bidder_id
    assert ranking["rows"][0]["rank"] == 1 and not ranking["rows"][0]["veto_flag"]
    assert c.get(f"/api/projects/{pid}/report").json()["status"] == "final"


def test_missing_external_record_is_a_completed_evidence_boundary(
    auto_client, law_kb, samples_dir, evidence_provider,
):
    c = auto_client
    pid, package_id = _import_extra_rule(c, _human_rule(
        "EXT-T1", "电子投标文件递交时限", "按交易平台递交回执核对是否在截止时间前递交。",
    ))
    run_id, job = _evaluate(c, pid, package_id, _one_bidder(c, pid, samples_dir))
    assert job["status"] == "succeeded", job
    external = next(cell for cell in _cells(c, run_id) if cell["criterion_code"] == "EXT-T1")
    assert external["machine_status"] == "unverifiable" and external["review_status"] == "auto_confirmed"
    report = c.get(f"/api/projects/{pid}/report").json()
    assert report["status"] == "final" and report["progress"]["blocking_open"] == 0
    assert "现有材料无法证实" in json.dumps(report, ensure_ascii=False)

