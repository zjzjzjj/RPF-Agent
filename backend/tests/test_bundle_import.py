"""项目包导入端到端：建档/role 标注/双通道抽价/失败跳过/包装根/重导覆盖。"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

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


PRICE_ROWS = [
    ["开标一览表"],
    ["产品名称", "不含增值税投标报价（元）"],
    ["产品甲", 100],
]


def _make_zip_bytes(tender_files: dict[str, bytes], companies: dict[str, dict[str, bytes]]) -> bytes:
    """companies: 公司名 → {文件名: 内容}；tender_files: 文件名 → 内容。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in tender_files.items():
            zf.writestr(f"招标/{name}", data)
        for comp, files in companies.items():
            for name, data in files.items():
                zf.writestr(f"投标/{comp}/{name}", data)
    return buf.getvalue()


def _sample_tender(samples_dir: Path) -> bytes:
    p = next((samples_dir / "tender").glob("*招标文件.pdf"))
    return p.read_bytes()


def _import(client, content: bytes, name: str = "导入测试项目") -> dict:  # noqa: ANN001
    resp = client.post(
        "/api/projects/import-bundle",
        files={"file": ("项目包.zip", content, "application/zip")},
        data={"name": name},
    )
    assert resp.status_code == 202, resp.text
    out = resp.json()
    job = wait_job(client, out["job_id"])
    assert job["status"] == "succeeded", job
    return {"project_id": out["project_id"], "job": job}


def test_import_end_to_end(client, app_env, samples_dir) -> None:  # noqa: ANN001
    content = _make_zip_bytes(
        {
            "某项目(招标文件).pdf": _sample_tender(samples_dir),
            "答疑澄清.pdf": _sample_tender(samples_dir),
        },
        {
            "公司甲": {
                "开标一览表.xlsx": _xlsx_bytes(PRICE_ROWS),
                "投标文件.pdf": _sample_tender(samples_dir),
            },
            "公司乙": {
                "供应商信息收集模板.xlsx": _xlsx_bytes([["公司名", "公司乙"]]),
            },
        },
    )
    res = _import(client, content)
    pid = res["project_id"]
    result = res["job"]["result_json"]

    assert result["counts"] == {"bidders": 2, "priced": 1, "unpriced": 1}
    assert result["wrapper_stripped"] is False
    # 招标侧：主招标文件 + addendum
    roles = {d["filename"]: d["role"] for d in result["tender_documents"]}
    assert roles["某项目(招标文件).pdf"] == "tender"
    assert roles["答疑澄清.pdf"] == "addendum"

    # 投标人 = 公司文件夹名
    bidders = client.get(f"/api/projects/{pid}/bidders").json()
    assert [b["name"] for b in bidders] == ["公司甲", "公司乙"]
    jia, yi = bidders
    assert float(jia["bid_price"]) == 100.0
    assert jia["price_status"] == "extracted"
    assert yi["bid_price"] is None
    assert yi["price_status"] == "missing"

    # role 标注：报价表 → price，大 PDF → main
    by_role: dict[str, list[str]] = {}
    for d in result["bidders"][0]["files"]:
        by_role.setdefault(d["role"], []).append(d["filename"])
    assert "开标一览表.xlsx" in by_role.get("price", [])
    assert "投标文件.pdf" in by_role.get("main", [])

    # 跳过公司在 result 中标注
    assert any("公司乙" in w for w in result["warnings"])
    assert result["bidders"][1]["price"]["status"] == "missing"


def test_import_main_pdf_fallback_matches_truth(client, app_env, samples_dir) -> None:  # noqa: ANN001
    """公司文件夹只有主投标 PDF（无开标一览表/报价表）：第 4 通道（主文件文本五级兜底）
    在 job 内抽价，必须与生成侧独立真值一致——与 run_e2e 的"报价抽取 0 差异"同口径。"""
    import json

    exp = json.loads((samples_dir / "bids" / "expected_scores.json").read_text("utf-8"))["prices_yuan"]
    pdfs = sorted((samples_dir / "bids").glob("*_投标文件.pdf"))
    assert len(pdfs) == 5
    content = _make_zip_bytes(
        {"某项目(招标文件).pdf": _sample_tender(samples_dir)},
        {p.stem.split("_")[1]: {p.name: p.read_bytes()} for p in pdfs},
    )
    res = _import(client, content, name="主PDF兜底")
    pid = res["project_id"]
    assert res["job"]["result_json"]["counts"] == {"bidders": 5, "priced": 5, "unpriced": 0}

    key_by_name = {p.stem.split("_")[1]: p.stem.split("_")[0] for p in pdfs}
    for b in client.get(f"/api/projects/{pid}/bidders").json():
        want = exp[key_by_name[b["name"]]]
        assert abs(float(b["bid_price"]) - float(want)) < 0.01, (b["name"], b["bid_price"], want)
        assert b["price_status"] in ("extracted", "conflict")  # conflict 是信息性标记，不卡评分
        assert (b["price_breakdown_json"] or {}).get("source") == "main_pdf_text"


def test_import_wrapper_root_stripped(client, app_env, samples_dir) -> None:  # noqa: ANN001
    inner = _make_zip_bytes(
        {"某项目(招标文件).pdf": _sample_tender(samples_dir)},
        {"公司甲": {"开标一览表.xlsx": _xlsx_bytes(PRICE_ROWS)}},
    )
    # 外面再包一层包装根
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        with zipfile.ZipFile(io.BytesIO(inner)) as src:
            for info in src.infolist():
                zf.writestr(f"项目包/{info.filename}", src.read(info))
    res = _import(client, buf.getvalue())
    result = res["job"]["result_json"]
    assert result["wrapper_stripped"] is True
    assert result["counts"]["priced"] == 1


def test_reimport_replaces_not_doubles(client, app_env, samples_dir) -> None:  # noqa: ANN001
    content = _make_zip_bytes(
        {"某项目(招标文件).pdf": _sample_tender(samples_dir)},
        {"公司甲": {"开标一览表.xlsx": _xlsx_bytes(PRICE_ROWS)}},
    )
    res = _import(client, content)
    pid = res["project_id"]
    resp = client.post(
        f"/api/projects/{pid}/import-bundle",
        files={"file": ("项目包.zip", content, "application/zip")},
    )
    assert resp.status_code == 202, resp.text
    job = wait_job(client, resp.json()["job_id"])
    assert job["status"] == "succeeded", job
    assert job["result_json"]["counts"]["bidders"] == 1  # 不是 2
    bidders = client.get(f"/api/projects/{pid}/bidders").json()
    assert len(bidders) == 1
    assert float(bidders[0]["bid_price"]) == 100.0


def test_import_gbk_zip(client, app_env, samples_dir, monkeypatch) -> None:  # noqa: ANN001
    """GBK 无 flag 文件名（资源管理器打包形态）也能识别目录结构。"""
    import zipfile as zfmod

    def _cp437_no_flag(self) -> tuple[bytes, int]:  # noqa: ANN001
        return self.filename.encode("cp437"), 0

    monkeypatch.setattr(zfmod.ZipInfo, "_encodeFilenameFlags", _cp437_no_flag)

    def mojibake(name: str) -> str:
        return name.encode("gbk").decode("cp437")

    entries = {
        "招标/某项目(招标文件).pdf": _sample_tender(samples_dir),
        "投标/公司甲/开标一览表.xlsx": _xlsx_bytes(PRICE_ROWS),
    }
    buf = io.BytesIO()
    with zfmod.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(mojibake(name), data)
    res = _import(client, buf.getvalue())
    assert res["job"]["result_json"]["counts"] == {"bidders": 1, "priced": 1, "unpriced": 0}
