"""导入 → 评分直通：无确认卡点、缺价公司无效价标注、冻结价采纳口径。"""
from __future__ import annotations

import io
import zipfile
from decimal import Decimal
from pathlib import Path

from app.db.models import EvaluationRun, Project, RulePackage
from app.db.session import session_scope
from app.services.automatic_scoring_service import _resolve_prices

from .conftest import wait_job


def _xlsx_bytes(rows: list[list[object]]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "开标一览表"
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _freeze_template(client, pid: str) -> int:  # noqa: ANN001
    r = client.post(f"/api/projects/{pid}/rules/instantiate",
                    json={"template_id": "comprehensive-goods"})
    assert r.status_code == 200, r.text
    package_id = r.json()["package_id"]
    pkg = client.get(f"/api/rules/packages/{package_id}").json()
    for c in pkg["criteria"]:
        rr = client.post(f"/api/rules/criteria/{c['id']}/signoff",
                         json={"reason": "模板通用判据作为基线启用"})
        assert rr.status_code == 200, rr.text
    assert client.post(f"/api/rules/packages/{package_id}/confirm").status_code == 200
    assert client.post(f"/api/rules/packages/{package_id}/freeze").status_code == 200
    return package_id


def test_import_then_score_without_confirm(client, app_env, samples_dir) -> None:  # noqa: ANN001
    """导入抽价即生效：不做任何确认动作直接发起评分；缺价公司按无效价标注。"""
    tender_pdf = next((samples_dir / "tender").glob("*招标文件.pdf")).read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("招标/某项目(招标文件).pdf", tender_pdf)
        zf.writestr("投标/公司甲/开标一览表.xlsx",
                    _xlsx_bytes([["产品名称", "不含增值税投标报价（元）"], ["产品甲", 12345]]))
        zf.writestr("投标/公司甲/投标文件.pdf", tender_pdf)  # 有文本的 PDF 即可
        zf.writestr("投标/公司乙/供应商信息收集模板.xlsx",
                    _xlsx_bytes([["公司名", "公司乙"]]))  # 无任何报价源
    resp = client.post("/api/projects/import-bundle",
                       files={"file": ("项目包.zip", buf.getvalue(), "application/zip")},
                       data={"name": "导入直评项目"})
    assert resp.status_code == 202, resp.text
    out = resp.json()
    pid = out["project_id"]
    job = wait_job(client, out["job_id"])
    assert job["status"] == "succeeded", job

    package_id = _freeze_template(client, pid)

    # 不传 bidder_ids（缺省=全部）、不确认报价 → 直接可发起
    resp = client.post(f"/api/projects/{pid}/runs",
                       json={"package_id": package_id, "stages": ["objective", "price"]})
    assert resp.status_code == 200, resp.text
    run_job = wait_job(client, resp.json()["job_id"], timeout=180)
    assert run_job["status"] == "succeeded", run_job.get("error")

    bidders = {b["name"]: b for b in client.get(f"/api/projects/{pid}/bidders").json()}
    assert float(bidders["公司甲"]["bid_price"]) == 12345.0
    assert bidders["公司乙"]["price_status"] == "missing"

    matrix = client.get(f"/api/runs/{run_job['result_json']['run_id']}/matrix").json()
    assert len(matrix["bidders"]) == 2
    price_cells = {}
    for b_id, cols in matrix["cells"].items():
        for cell in cols.values():
            if cell["category"] == "price":
                price_cells[int(b_id)] = cell
    assert len(price_cells) == 2
    # 缺价公司：价格分 0 + 无效价标注（照常参评、结果标注在 ScoreRecord/报告里）
    yi_id = bidders["公司乙"]["id"]
    yi_cell = price_cells[yi_id]
    assert Decimal(str(yi_cell["final_score"])) == Decimal(0)
    from sqlalchemy import select

    from app.db.models import ScoreRecord

    with session_scope() as s:
        warns = [
            str(r.warnings_json)
            for r in s.execute(
                select(ScoreRecord).where(ScoreRecord.bidder_id == yi_id)
            ).scalars()
        ]
    assert any("报价为空或≤0" in w for w in warns), warns
    # 有价公司：价格分已由确定性引擎落库
    jia_cell = price_cells[bidders["公司甲"]["id"]]
    assert jia_cell["final_score"] is not None


def test_resolve_prices_accepts_extracted(app_env) -> None:  # noqa: ANN001
    """自动管线采纳冻结价：price_status=extracted（无需 confirmed）即可直接用。"""
    with session_scope() as s:
        project = Project(id="PRJ-FROZEN-PRICE", name="冻结价采纳")
        s.add(project)
        s.flush()
        pkg = RulePackage(project_id=project.id, version=1, status="frozen")
        s.add(pkg)
        s.flush()
        run = EvaluationRun(project_id=project.id, package_id=pkg.id, package_version=1)
        s.add(run)
        s.flush()
        run_id = run.id

    manifest = {
        "bidders": [
            {"id": 1, "name": "公司甲", "bid_price": "88888.50", "price_status": "extracted"},
            {"id": 2, "name": "公司乙", "bid_price": "77777", "price_status": "conflict"},
            {"id": 3, "name": "公司丙", "bid_price": None, "price_status": "missing"},
        ],
        "criteria": [],
    }

    def _gateway_factory(*_a, **_k):  # noqa: ANN001, ANN202
        raise AssertionError("冻结价采纳不应触发 LLM 补抽")

    # 公司丙（missing）走补抽分支会调网关——这里只验证两家有价公司的采纳，
    # 丙的 missing 分支由 test_import_then_score_without_confirm 端到端覆盖。
    manifest["bidders"] = manifest["bidders"][:2]
    prices = _resolve_prices(run_id, manifest, {}, _gateway_factory)
    assert prices == {1: Decimal("88888.50"), 2: Decimal("77777")}
