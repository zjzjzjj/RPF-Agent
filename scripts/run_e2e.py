"""端到端验收脚本（离线 Mock，可重复）。

用法：
    python scripts/run_e2e.py --provider mock --assert-expected   # 全链路 + 断言 expected_scores.json
    python scripts/run_e2e.py --dump-nodes                        # 只解析招标文件并打印条款树
    python scripts/run_e2e.py --provider deepseek --no-assert     # 真实模型跑通（需配置 API）

断言口径（对应验收指标）：
- 价格分与生成侧独立计算的真值**精确一致**（0 差异）
- 资格/符合性三态与 expected 一致（建议否决 ≠ 自动否决）
- 商务计数分精确
- 证据核验率（Mock 引文逐字 → 100%）
- 排名含建议否决者，且未定分项有警告
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

BIDDER_KEYS = ["jia", "yi", "bing", "ding", "wu"]


def _wait(client, job_id: int, timeout: float = 300.0) -> dict:  # noqa: ANN001
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.2)
    raise TimeoutError(f"任务 {job_id} 超时")


def _upload(client, pid: str, path: Path, kind: str, bidder_id: int | None = None) -> dict:  # noqa: ANN001
    data = {"kind": kind}
    if bidder_id is not None:
        data["bidder_id"] = str(bidder_id)
    resp = client.post(
        f"/api/projects/{pid}/documents",
        files={"file": (path.name, path.read_bytes(), "application/pdf")},
        data=data,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="mock", help="mock | deepseek | qwen ...")
    parser.add_argument("--assert-expected", action="store_true")
    parser.add_argument("--dump-nodes", action="store_true")
    parser.add_argument("--keep-data", action="store_true", help="保留临时数据目录便于排查")
    args = parser.parse_args()

    if args.provider == "mock":
        os.environ.setdefault("BID_EVAL_LLM_PROVIDER", "mock")

    tmp = tempfile.mkdtemp(prefix="bid-eval-e2e-")
    os.environ.setdefault("BID_EVAL_DATA_DIR", tmp)
    os.environ.setdefault("BID_EVAL_DB_PATH", str(Path(tmp) / "e2e.db"))
    print(f"[env] 数据目录：{tmp}")

    samples = REPO / "data" / "samples"
    tender_pdf = next((samples / "tender").glob("*招标文件.pdf"), None)
    if tender_pdf is None:
        print("样例缺失，先生成……")
        os.system(f'"{sys.executable}" "{REPO / "scripts" / "generate_sample_tender.py"}"')
        os.system(f'"{sys.executable}" "{REPO / "scripts" / "generate_sample_bids.py"}"')
        tender_pdf = next((samples / "tender").glob("*招标文件.pdf"))
    expected = json.loads((samples / "bids" / "expected_scores.json").read_text("utf-8"))

    from app.db import session as db
    from app.main import create_app
    from fastapi.testclient import TestClient

    db.reset_engine()
    db.init_db()
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        mark = "✓" if ok else "✗"
        print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    with TestClient(create_app()) as client:
        pid = client.post("/api/projects", json={"name": "端到端验收", "max_price": "8000000"}).json()["id"]
        print(f"[1/7] 新建项目 {pid}")

        out = _upload(client, pid, tender_pdf, "tender")
        job = _wait(client, out["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        doc = client.get(f"/api/documents/{out['document_id']}").json()
        print(f"[2/7] 招标文件解析：{doc['page_count']} 页，状态 {doc['parse_status']}")
        check("招标文件解析成功", doc["parse_status"] == "parsed")

        if args.dump_nodes:
            nodes = client.get(f"/api/documents/{out['document_id']}/nodes",
                               params={"parent_id": f"tender{out['document_id']}:root"}).json()
            for n in nodes:
                print(f"    {n['node_id']} p{n['page']} {n['title']}")
            return 0

        resp = client.post(f"/api/projects/{pid}/rules/compile",
                           json={"document_id": out["document_id"], "provider": args.provider})
        job = _wait(client, resp.json()["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        package_id = job["result_json"]["package_id"]
        pkg = client.get(f"/api/rules/packages/{package_id}").json()
        by_cat: dict[str, int] = {}
        for c in pkg["criteria"]:
            by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
        errors = [v for v in pkg["validations"] if v["severity"] == "error"]
        print(f"[3/7] 规则编译：{len(pkg['criteria'])} 项 {by_cat}；校验 error {len(errors)} 条")
        check("规则覆盖 7 否决 + 1 价格 + 3 技术 + 2 商务",
              by_cat == {"qualification": 4, "compliance": 3, "price": 1, "technical": 3, "commercial": 2},
              str(by_cat))
        check("确定性校验无 error", not errors, str(errors[:2]))

        for c in pkg["criteria"]:
            r = client.post(f"/api/rules/criteria/{c['id']}/accept", json={"reason": "与原文核对一致"})
            assert r.status_code == 200, r.text
        client.post(f"/api/rules/packages/{package_id}/confirm")
        frozen = client.post(f"/api/rules/packages/{package_id}/freeze").json()
        print(f"[4/7] 规则冻结：v{frozen['version']} 哈希 {frozen['content_hash']}")
        check("冻结产生内容哈希", bool(frozen["content_hash"]))
        r = client.patch(f"/api/rules/criteria/{pkg['criteria'][0]['id']}",
                         json={"name": "篡改测试", "reason": "尝试修改"})
        check("冻结后写入被拒（409）", r.status_code == 409, f"实际 {r.status_code}")

        bidder_ids: dict[str, int] = {}
        pdfs = sorted((samples / "bids").glob("*_投标文件.pdf"))
        for key in BIDDER_KEYS:
            pdf = next(p for p in pdfs if p.name.startswith(f"{key}_"))
            b = client.post(f"/api/projects/{pid}/bidders", json={"name": pdf.stem.split("_")[1]}).json()
            bo = _upload(client, pid, pdf, "bid", bidder_id=b["id"])
            jb = _wait(client, bo["job_id"])
            assert jb["status"] == "succeeded", jb.get("error")
            price = client.get(f"/api/bidders/{b['id']}/price").json()
            ok = Decimal(price["bid_price"]) == Decimal(expected["prices_yuan"][key])
            check(f"报价抽取 {key}", ok, f"{price['bid_price']}（期望 {expected['prices_yuan'][key]}）")
            client.post(f"/api/bidders/{b['id']}/price/confirm", json={"reason": "与开标一览表核对一致"})
            bidder_ids[key] = b["id"]
        print(f"[5/7] 5 家投标人文件解析与报价确认完成")

        resp = client.post(f"/api/projects/{pid}/runs", json={
            "package_id": package_id,
            "bidder_ids": list(bidder_ids.values()),
            "stages": ["objective", "price", "subjective"],
            "provider": args.provider if args.provider != "mock" else None,
        })
        run_job = _wait(client, resp.json()["job_id"])
        assert run_job["status"] == "succeeded", run_job.get("error")
        run_id = resp.json()["run_id"]
        stats = client.get(f"/api/runs/{run_id}/stats").json()
        print(f"[6/7] 评分运行 #{run_id}：{stats['score_records']} 条记录，待人工 {stats['need_human']}，"
              f"证据核验率 {stats['verification_rate']}")
        check("证据核验率 100%（Mock 引文逐字）", stats["verification_rate"] == 1.0, str(stats))
        check("存在待人工项（建议否决/冲突/主观分）", stats["need_human"] > 0)

        matrix = client.get(f"/api/runs/{run_id}/matrix").json()
        id2key = {v: k for k, v in bidder_ids.items()}
        price_ok = True
        for b_id, cols in matrix["cells"].items():
            for cell in cols.values():
                if cell["category"] == "price":
                    want = expected["price"]["price_scores"][id2key[int(b_id)]]
                    if cell["final_score"] != want:
                        price_ok = False
                        print(f"    价格分不一致：{id2key[int(b_id)]} 得 {cell['final_score']}，期望 {want}")
        check("价格分与独立真值 0 差异", price_ok)

        veto_ok = True
        for b_id, cols in matrix["cells"].items():
            key = id2key[int(b_id)]
            for cell in cols.values():
                if cell["score_type"] != "veto":
                    continue
                # 语义断言：保证金项在丙处必须为"建议否决"，其余必须通过
                if "保证金" in cell["criterion_name"]:
                    if key == "bing" and cell["machine_status"] != "recommend_reject":
                        veto_ok = False
                        print(f"    丙应被建议否决：实际 {cell['machine_status']}")
                    if key != "bing" and cell["machine_status"] != "pass":
                        veto_ok = False
                        print(f"    {key} 保证金应通过：实际 {cell['machine_status']}")
                # 丁的报价冲突项必须进人工（不得自动选边）
                if key == "ding" and "报价" in cell["criterion_name"]:
                    if cell["review_status"] != "conflict_flagged":
                        veto_ok = False
                        print(f"    丁报价冲突应进人工裁决：实际 {cell['review_status']}")
        check("资格/符合性三态符合预期（建议否决≠自动否决）", veto_ok)

        commercial_ok = True
        for b_id, cols in matrix["cells"].items():
            key = id2key[int(b_id)]
            want = expected["commercial"][key]
            for cell in cols.values():
                if "业绩" in cell["criterion_name"] and cell["final_score"] != f"{want['performance']}.00":
                    commercial_ok = False
                if "认证" in cell["criterion_name"] and cell["final_score"] != f"{want['certificates']}.00":
                    commercial_ok = False
        check("商务计数分精确", commercial_ok)

        ranking = client.get(f"/api/projects/{pid}/ranking").json()
        veto_flagged = [r for r in ranking["rows"] if r["veto_flag"]]
        print(f"[7/7] 排名：")
        for row in ranking["rows"]:
            print(f"    #{row['rank']} {row['bidder_name']} 总分 {row['total']}"
                  f"{'（建议否决/已否决）' if row['veto_flag'] else ''}")
        check("建议否决者保留在榜（交叉验证）", len(veto_flagged) == 1)
        check("未定分项有警告", bool(ranking["warnings"]))

        report = client.get(f"/api/projects/{pid}/report.html")
        check("评标报告可导出（HTML）", report.status_code == 200 and "评标报告" in report.text)

    print()
    if failures:
        print(f"验收失败 {len(failures)} 项：")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("验收通过：全链路离线跑通，断言全部成立。")
    if not args.keep_data:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
