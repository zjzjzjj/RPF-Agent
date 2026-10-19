"""端到端冒烟验收（真实模型）。人工回退模式的口径——BID_EVAL_AUTO_MODE=0。

与 run_auto_pipeline.py（全自动模式验收）成对。**不内置任何假 LLM**：编译、
主观分等语义环节走 .env 或 --provider 指定的真实模型（约 3-10 分钟、少量费用）。

用法：
    python scripts/run_e2e.py                       # 用 .env 配置的模型跑全链
    python scripts/run_e2e.py --provider deepseek   # 显式指定 provider
    python scripts/run_e2e.py --dump-nodes          # 只解析招标文件并打印条款树（离线）

结构断言（不依赖模型输出形状）：
- 编译产出 ≥1 条评分项（含价格项）；未决项自动补全留痕后可确认/冻结（冻结后写入 409）
- 5 家报价抽取与生成侧独立真值精确一致（确定性五级启发，0 差异）
- 评分运行成功、排名可计算、评标报告可导出

参考值（打印不判分，反映抽取质量而非代码对错）：分类计数、价格分对照、证据核验率。
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

# 样例招标文件的价格公式真值（scripts/sample_content/tender.py 的固定契约），
# 仅用于给"模型漏抽结构化参数"的公式补全——补全动作带理由留痕，可在审计中回溯
SAMPLE_FORMULA = {"formula_type": "benchmark_ratio", "benchmark_method": "average",
                  "k": "0.95", "upper_bound": "40", "rounding": 2}


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
    parser = argparse.ArgumentParser(description="端到端冒烟验收（真实模型，人工回退模式口径）")
    parser.add_argument("--provider", default=None,
                        help="覆盖 .env 的 provider（openai/deepseek/qwen/...）；缺省用已配置模型")
    parser.add_argument("--dump-nodes", action="store_true")
    parser.add_argument("--keep-data", action="store_true", help="保留临时数据目录便于排查")
    args = parser.parse_args()

    if args.provider:
        os.environ["BID_EVAL_LLM_PROVIDER"] = args.provider  # 显式指定优先于 .env
    # 本脚本的口径是人工回退模式（三态/复核队列）；全自动验收走 run_auto_pipeline.py
    os.environ.setdefault("BID_EVAL_AUTO_MODE", "0")

    tmp = tempfile.mkdtemp(prefix="bid-eval-e2e-")
    os.environ.setdefault("BID_EVAL_DATA_DIR", tmp)
    os.environ.setdefault("BID_EVAL_DB_PATH", str(Path(tmp) / "e2e.db"))

    from app.db import session as db
    from app.main import create_app
    from fastapi.testclient import TestClient

    # .env 注入发生在 app.config 导入时；此后打印才是最终生效的 provider
    print(f"[env] 数据目录：{tmp}｜provider={os.environ.get('BID_EVAL_LLM_PROVIDER', '(未配置)')}"
          f"｜model={os.environ.get('BID_EVAL_LLM_MODEL', '(默认)')}")
    samples = REPO / "data" / "samples"
    tender_pdf = next((samples / "tender").glob("*招标文件.pdf"), None)
    if tender_pdf is None:
        print("样例缺失，先生成……")
        os.system(f'"{sys.executable}" "{REPO / "scripts" / "generate_sample_tender.py"}"')
        os.system(f'"{sys.executable}" "{REPO / "scripts" / "generate_sample_bids.py"}"')
        tender_pdf = next((samples / "tender").glob("*招标文件.pdf"))
    expected = json.loads((samples / "bids" / "expected_scores.json").read_text("utf-8"))

    db.reset_engine()
    db.init_db()
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        mark = "✓" if ok else "✗"
        print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    with TestClient(create_app()) as client:
        pid = client.post("/api/projects", json={"name": "端到端冒烟", "max_price": "8000000"}).json()["id"]
        print(f"[1/7] 新建项目 {pid}")

        out = _upload(client, pid, tender_pdf, "tender")
        job = _wait(client, out["job_id"], timeout=600)
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

        # ---- 编译（真实模型；超长文档自动按章节优先级选段，范围落任务消息）----
        resp = client.post(f"/api/projects/{pid}/rules/compile",
                           json={"document_id": out["document_id"]})
        job = _wait(client, resp.json()["job_id"], timeout=600)
        assert job["status"] == "succeeded", job.get("error")
        package_id = job["result_json"]["package_id"]
        pkg = client.get(f"/api/rules/packages/{package_id}").json()
        by_cat: dict[str, int] = {}
        for c in pkg["criteria"]:
            by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
        print(f"[3/7] 规则编译：{len(pkg['criteria'])} 项 {by_cat}"
              f"（参考：样例契约为 4 资格 + 3 符合 + 1 价格 + 3 技术 + 2 商务）")
        check("编译产出评分项", len(pkg["criteria"]) > 0)
        check("编译出价格分项", by_cat.get("price", 0) >= 1, str(by_cat))

        # ---- 未决项自动补全（留痕）：真实模型常漏结构化参数，人工口径 = 补全后确认 ----
        # 注意：price-formula PATCH 只修公式行，criterion 状态必须由 PATCH criterion 置 verified
        patched = 0
        for c in pkg["criteria"]:
            if c["status"] in ("verified", "retired"):
                continue
            if c["category"] == "price":
                r = client.patch(f"/api/rules/packages/{package_id}/price-formula",
                                 json={**SAMPLE_FORMULA, "reason": "冒烟验收：按样例招标文件载明的公式补全"})
                if r.status_code != 200:
                    check("自动补全价格公式", False, r.text[:200])
            r = client.patch(f"/api/rules/criteria/{c['id']}",
                             json={"description": c.get("description") or c["name"],
                                   "reason": "冒烟验收：人工复核确认，自动补全未决项"})
            if r.status_code != 200:
                check(f"自动补全 {c['code']} {c['name']}", False, r.text[:200])
            patched += 1
        if patched:
            print(f"      自动补全未决项 {patched} 条（全部带理由留痕）")

        for c in pkg["criteria"]:
            r = client.post(f"/api/rules/criteria/{c['id']}/accept", json={"reason": "与原文核对一致"})
            assert r.status_code == 200, r.text
        client.post(f"/api/rules/packages/{package_id}/confirm")
        frozen = client.post(f"/api/rules/packages/{package_id}/freeze").json()
        print(f"[4/7] 规则冻结：v{frozen.get('version')} 哈希 {frozen.get('content_hash')}")
        check("冻结产生内容哈希", bool(frozen.get("content_hash")))
        r = client.patch(f"/api/rules/criteria/{pkg['criteria'][0]['id']}",
                         json={"name": "篡改测试", "reason": "尝试修改"})
        check("冻结后写入被拒（409）", r.status_code == 409, f"实际 {r.status_code}")

        bidder_ids: dict[str, int] = {}
        pdfs = sorted((samples / "bids").glob("*_投标文件.pdf"))
        for key in BIDDER_KEYS:
            pdf = next(p for p in pdfs if p.name.startswith(f"{key}_"))
            b = client.post(f"/api/projects/{pid}/bidders", json={"name": pdf.stem.split("_")[1]}).json()
            bo = _upload(client, pid, pdf, "bid", bidder_id=b["id"])
            jb = _wait(client, bo["job_id"], timeout=600)
            assert jb["status"] == "succeeded", jb.get("error")
            price = client.get(f"/api/bidders/{b['id']}/price").json()
            ok = Decimal(price["bid_price"]) == Decimal(expected["prices_yuan"][key])
            check(f"报价抽取 {key}", ok, f"{price['bid_price']}（期望 {expected['prices_yuan'][key]}）")
            client.post(f"/api/bidders/{b['id']}/price/confirm", json={"reason": "与开标一览表核对一致"})
            bidder_ids[key] = b["id"]
        print("[5/7] 5 家投标人文件解析与报价确认完成")

        resp = client.post(f"/api/projects/{pid}/runs", json={
            "package_id": package_id,
            "bidder_ids": list(bidder_ids.values()),
            "stages": ["objective", "price", "subjective"],
        })
        assert resp.status_code == 200, resp.text
        run_job = _wait(client, resp.json()["job_id"], timeout=900)
        assert run_job["status"] == "succeeded", run_job.get("error")
        run_id = resp.json()["run_id"]
        stats = client.get(f"/api/runs/{run_id}/stats").json()
        print(f"[6/7] 评分运行 #{run_id}：{stats['score_records']} 条记录，待人工 {stats['need_human']}，"
              f"证据核验率 {stats['verification_rate']}（人工回退模式：待人工属预期，不判分）")

        # 价格分对照：公式已按样例真值补全，理论上应与生成侧独立真值一致
        matrix = client.get(f"/api/runs/{run_id}/matrix").json()
        id2key = {v: k for k, v in bidder_ids.items()}
        diffs: list[str] = []
        price_cells = 0
        for b_id, cols in matrix["cells"].items():
            for cell in cols.values():
                if cell["category"] == "price":
                    price_cells += 1
                    want = expected["price"]["price_scores"][id2key[int(b_id)]]
                    if cell["final_score"] != want:
                        diffs.append(f"{id2key[int(b_id)]} 得 {cell['final_score']}，期望 {want}")
        if price_cells:
            # 基准价剔除口径取决于否决项的编译结果（模型相关），按文件头承诺只打印不判分
            if diffs:
                print(f"  [参考] 价格分与独立真值存在差异（抽取质量信息）：{'；'.join(diffs[:3])}")
            else:
                print("  [参考] 价格分与独立真值完全一致")
        else:
            check("价格分有落库记录", False, "矩阵中没有价格类单元格")

        ranking = client.get(f"/api/projects/{pid}/ranking").json()
        print("[7/7] 排名：")
        for row in ranking["rows"]:
            print(f"    #{row['rank']} {row['bidder_name']} 总分 {row['total']}"
                  f"{'（建议否决/已否决）' if row['veto_flag'] else ''}")
        check("排名覆盖全部投标人", len(ranking["rows"]) == 5, f"实际 {len(ranking['rows'])}")

        report = client.get(f"/api/projects/{pid}/report.html")
        check("评标报告可导出（HTML）", report.status_code == 200 and "评标报告" in report.text)

    print()
    if failures:
        print(f"冒烟验收失败 {len(failures)} 项：")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("冒烟验收通过：解析→编译→补全→冻结→评分→报告全链跑通（真实模型）。")
    if not args.keep_data:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
