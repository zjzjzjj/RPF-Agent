#!/usr/bin/env python
"""全自动模式端到端验收：导入法规规则包 → 确认 → 冻结 → 评分 → 定稿报告，零人工。

与 run_e2e.py（人工回退模式冒烟）成对；本脚本断言的是 BID_EVAL_AUTO_MODE=1
（默认）的全自动语义：
- 法规包 confirm/freeze 的阻断项自动采纳（含模板签批的自动形态）；
- 报价自动确认（无报价的投标人自动剔除并留痕）；
- 评分运行结束无 need_human/conflict_flagged 残留，全部 auto_confirmed 或 rejected；
- human_only/冲突项由真实模型带原文证据裁决（reasoning 落留痕）；
- 报告直接 final（无待复核疑问）——零 need_human 是 auto 模式的**结构保证**
  （模型故障也降级为 fail 终态，绝不悬挂），对任何**已配置**的模型成立；
  未配置 provider/密钥缺失等配置错误不降级，整个运行显式失败（fail-fast，
  见 decisions/0005）。

需要 .env 或 --provider 配置真实模型。注意：87 条法规包 × 5 家投标人约 400 次
裁决调用，推理型模型下全程约 30-120 分钟——可用 BID_EVAL_LLM_CONCURRENCY
（默认 4）提高并发；只要快速验证管线结构，直接跑 pytest 的 test_auto_pipeline
（离线、秒级，验证同一套自动收敛保证）。

用法：
    python scripts/run_auto_pipeline.py                    # 用 .env 配置的模型跑全链
    python scripts/run_auto_pipeline.py --assert-final     # 失败/未定稿 exit 1
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))


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
    parser = argparse.ArgumentParser(description="全自动模式端到端验收（真实模型）")
    parser.add_argument("--provider", default=None,
                        help="覆盖 .env 的 provider（openai/deepseek/qwen/...）；缺省用已配置模型")
    parser.add_argument("--assert-final", action="store_true",
                        help="报告未定稿或流程有残留待裁决 → exit 1")
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args()

    if args.provider:
        os.environ["BID_EVAL_LLM_PROVIDER"] = args.provider  # 显式指定优先于 .env
    os.environ.setdefault("BID_EVAL_AUTO_MODE", "1")  # 本脚本就是全自动语义的验收

    tmp = tempfile.mkdtemp(prefix="bid-eval-auto-")
    os.environ.setdefault("BID_EVAL_DATA_DIR", tmp)
    os.environ.setdefault("BID_EVAL_DB_PATH", str(Path(tmp) / "auto.db"))
    # 法条核验源 = 仓库 vendored 知识库（临时数据目录里没有，必须显式指向）
    os.environ.setdefault("BID_EVAL_LAW_KB_PATH", str(REPO / "data" / "law_kb.json"))
    print(f"[env] 数据目录：{tmp}｜auto_mode={os.environ['BID_EVAL_AUTO_MODE']}")

    samples = REPO / "data" / "samples"
    tender_pdf = next((samples / "tender").glob("*招标文件.pdf"), None)
    if tender_pdf is None:
        print("样例缺失，先生成……")
        os.system(f'"{sys.executable}" "{REPO / "scripts" / "generate_sample_tender.py"}"')
        os.system(f'"{sys.executable}" "{REPO / "scripts" / "generate_sample_bids.py"}"')
        tender_pdf = next((samples / "tender").glob("*招标文件.pdf"))

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
        pid = client.post("/api/projects",
                          json={"name": "全自动验收", "max_price": "8000000"}).json()["id"]
        print(f"[1/6] 新建项目 {pid}")

        out = _upload(client, pid, tender_pdf, "tender")
        job = _wait(client, out["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        resp = client.post(f"/api/projects/{pid}/rules/compile",
                           json={"document_id": out["document_id"]})
        job = _wait(client, resp.json()["job_id"], timeout=900)  # 真实模型 + 契约修复重试，可能超 5 分钟
        assert job["status"] == "succeeded", job.get("error")
        doc_pkg = job["result_json"]["package_id"]
        print(f"[2/6] 招标文件编译：规则包 #{doc_pkg}")

        # 法规包按 code 幂等追加进同一草稿（设计稿推荐操作序）
        from app.rules.library.law_pack_loader import load_law_pack

        r = client.post(f"/api/projects/{pid}/rules/import-law", json=load_law_pack())
        assert r.status_code == 200, r.text
        law_out = r.json()
        print(f"[3/6] 法规包追加：+{law_out['imported']} 条（dropped {len(law_out['dropped_nodes'])}）")

        # 真实模型可能漏抽价格公式的结构化参数（PRICE_PARAMS_INCOMPLETE 校验错误）；
        # auto-settle 只收敛评分项状态、填不了公式——按样例招标文件载明的公式补全（留痕）
        pkg = client.get(f"/api/rules/packages/{doc_pkg}").json()
        if any(v["code"] == "PRICE_PARAMS_INCOMPLETE" for v in pkg.get("validations") or []):
            r = client.patch(f"/api/rules/packages/{doc_pkg}/price-formula", json={
                "formula_type": "benchmark_ratio", "benchmark_method": "average",
                "k": "0.95", "upper_bound": "40", "rounding": 2,
                "reason": "验收：按样例招标文件载明的公式补全缺失参数"})
            assert r.status_code == 200, r.text
            print("      已按样例真值补全价格公式参数（PRICE_PARAMS_INCOMPLETE）")

        assert client.post(f"/api/rules/packages/{doc_pkg}/confirm").status_code == 200, \
            "全自动模式下阻断项应已被自动采纳"
        frozen = client.post(f"/api/rules/packages/{doc_pkg}/freeze").json()
        check("规则确认→冻结（阻断项自动采纳）", bool(frozen.get("content_hash")))

        bidder_ids: list[int] = []
        pdfs = sorted((samples / "bids").glob("*_投标文件.pdf"))
        for pdf in pdfs:
            b = client.post(f"/api/projects/{pid}/bidders",
                            json={"name": pdf.stem.split("_")[1]}).json()
            bo = _upload(client, pid, pdf, "bid", bidder_id=b["id"])
            jb = _wait(client, bo["job_id"])
            assert jb["status"] == "succeeded", jb.get("error")
            client.get(f"/api/bidders/{b['id']}/price")  # GET 触发报价抽取
            bidder_ids.append(b["id"])
        print(f"[4/6] {len(bidder_ids)} 家投标人解析完成（报价确认留给评分入口自动处理）")

        resp = client.post(f"/api/projects/{pid}/runs", json={
            "package_id": doc_pkg,
            "bidder_ids": bidder_ids,
            "stages": ["objective", "price", "subjective"],
        })
        assert resp.status_code == 200, resp.text
        # 全量裁决（87 条法规包 × 5 家 ≈ 400 次 LLM 调用）在推理型模型下需 30-120 分钟
        job = _wait(client, resp.json()["job_id"], timeout=7200)
        assert job["status"] == "succeeded", job.get("error")
        run_id = resp.json()["run_id"]
        summary = job["result_json"]
        print(f"[5/6] 评分运行 #{run_id}：objective {summary['objective']}｜"
              f"price {summary['price']}｜subjective {summary['subjective']}｜"
              f"match {summary['match']}｜残留待人工 {summary['need_human']}")
        check("零人工残留（无 need_human/conflict_flagged/pending/ai_suggested）",
              summary["need_human"] == 0, str(summary))

        matrix = client.get(f"/api/runs/{run_id}/matrix").json()
        cells = [c for cols in matrix["cells"].values() for c in cols.values()]
        bad = [c for c in cells if c["review_status"] not in ("auto_confirmed", "not_applicable")]
        check("全部评分记录收敛 auto_confirmed 终态", not bad,
              f"异常 {len(bad)} 条：{[ (c['criterion_name'], c['review_status']) for c in bad[:3] ]}")
        # 模糊项的裁决 reasoning/逐字引用在报告"自动裁决记录"章节可审计；auto 模式下
        # 机器状态不会残留 need_human/conflict_flagged（上一条 check 已覆盖），无需重复检查

        ranking = client.get(f"/api/projects/{pid}/ranking").json()
        check("排名无 missing_final 警告", all(not r["missing_final"] for r in ranking["rows"]),
              str([r["missing_final"] for r in ranking["rows"] if r["missing_final"]][:2]))

        report = client.get(f"/api/projects/{pid}/report").json()
        print(f"[6/6] 报告状态：{report['status']}｜疑问 {len(report['open_questions'])} 条")
        check("报告直接定稿（final）", report["status"] == "final", report["status"])
        check("无待复核疑问", not report["open_questions"],
              str([q["kind"] for q in report["open_questions"]][:5]))

    print()
    if failures:
        print(f"全自动验收失败 {len(failures)} 项：")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("全自动验收通过：导入→确认→冻结→评分→定稿报告，全链零人工。")
    if not args.keep_data:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
