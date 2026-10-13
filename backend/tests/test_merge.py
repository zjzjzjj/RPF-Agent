"""逐条覆盖（S1）全链路测试。

链路：实例化模板包 → 上传招标文件并以 merge 模式编译（产出临时编译包）
→ 匹配提案（确定性 + LLM 辅助）→ 人工处置（override / append_document / drop_template）
→ 签批剩余模板兜底 → 确认 → 冻结 → 快照溯源断言。
"""
from __future__ import annotations

import time
from pathlib import Path

from .conftest import upload_pdf, wait_job

REPO = Path(__file__).resolve().parents[2]


def _instantiate(client, pid: str) -> int:  # noqa: ANN001
    r = client.post(f"/api/projects/{pid}/rules/instantiate",
                    json={"template_id": "comprehensive-goods"})
    assert r.status_code == 200, r.text
    return r.json()["package_id"]


def _compile_merge(client, pid: str, document_id: int, target_id: int) -> int:  # noqa: ANN001
    r = client.post(
        f"/api/projects/{pid}/rules/compile",
        json={"document_id": document_id, "provider": "mock",
              "mode": "merge", "merge_target_id": target_id},
    )
    assert r.status_code == 200, r.text
    job = wait_job(client, r.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    return job["result_json"]["package_id"]


def _build_merge_context(client, samples_dir):  # noqa: ANN001
    """搭好合并上下文：模板包 + 临时编译包。返回 (pid, target_id, merge_package_id)。"""
    pid = client.post("/api/projects", json={"name": "逐条覆盖测试"}).json()["id"]
    target_id = _instantiate(client, pid)
    tender_pdf = next((samples_dir / "tender").glob("*招标文件.pdf"))
    out = upload_pdf(client, pid, tender_pdf, "tender")
    job = wait_job(client, out["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    merge_package_id = _compile_merge(client, pid, out["document_id"], target_id)
    return pid, target_id, merge_package_id


def test_match_proposal_structure(client, samples_dir) -> None:  # noqa: ANN001
    pid, target_id, merge_package_id = _build_merge_context(client, samples_dir)
    resp = client.get(f"/api/rules/packages/{merge_package_id}/match-proposal")
    assert resp.status_code == 200, resp.text
    prop = resp.json()
    assert prop["target_package_id"] == target_id
    assert prop["pairs"], "确定性或 LLM 辅助匹配应产出至少 1 对"
    for p in prop["pairs"]:
        assert p["pair_id"] and p["match_method"] in ("exact", "deterministic", "llm")
        assert p["template"]["id"] and p["document"]["id"]
    assert prop["unmatched_template"]
    assert prop["unmatched_document"]
    # 未匹配集合与配对集合不相交
    pair_doc_ids = {p["document"]["id"] for p in prop["pairs"]}
    pair_tpl_ids = {p["template"]["id"] for p in prop["pairs"]}
    assert all(d["id"] not in pair_doc_ids for d in prop["unmatched_document"])
    assert all(t["id"] not in pair_tpl_ids for t in prop["unmatched_template"])


def test_resolve_override_append_drop(client, samples_dir) -> None:  # noqa: ANN001
    pid, target_id, merge_package_id = _build_merge_context(client, samples_dir)
    prop = client.get(f"/api/rules/packages/{merge_package_id}/match-proposal").json()

    # ① 覆盖全部"否决项"配对（不影响 TOTAL_SCORE）
    decisions = [
        {"action": "override", "pair_id": p["pair_id"],
         "reason": "与招标文件逐字条款核对一致"}
        for p in prop["pairs"] if p["template"]["is_veto"]
    ]
    assert decisions, "提案中应包含否决项配对"
    # ② 追加一条未匹配的文档否决项
    unmatched_veto = next(
        (d for d in prop["unmatched_document"] if d["is_veto"]), None
    )
    assert unmatched_veto is not None
    decisions.append({"action": "append_document",
                      "document_criterion_id": unmatched_veto["id"],
                      "reason": "招标文件明确规定的否决项，模板中无对应条目"})
    # ③ 弃用一个未匹配模板条目（缴纳税收社保 → retired）
    drop_tpl = next(
        (t for t in prop["unmatched_template"] if t["code"] == "T1.4"), None
    )
    assert drop_tpl is not None
    decisions.append({"action": "drop_template",
                      "template_criterion_id": drop_tpl["id"],
                      "reason": "本项目招标文件未规定该资格门槛，予以弃用"})

    resp = client.post(
        f"/api/rules/packages/{merge_package_id}/match-proposal/resolve",
        json={"decisions": decisions},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["target_package_id"] == target_id

    # 模板包 origin → merged；被覆盖条目来源升级为 document
    pkg = client.get(f"/api/rules/packages/{target_id}").json()
    assert pkg["origin"] == "merged"
    by_code = {c["code"]: c for c in pkg["criteria"]}
    overridden_codes = [p["template"]["code"] for p in prop["pairs"] if p["template"]["is_veto"]]
    for code in overridden_codes:
        c = by_code[code]
        assert c["origin"] == "document", (code, c["origin"])
        assert c["status"] == "verified", (code, c["status"])
        assert c["source_quote"], (code, "覆盖后应带有招标文件原文")
        assert c["override_json"] is not None
        assert c["override_json"]["won_source"] in ("P1", "P2", "P3", "P4")
        assert c["override_json"]["lost_source"] == "P5"
    # 弃用条目 → retired
    assert by_code["T1.4"]["status"] == "retired", by_code["T1.4"]["status"]
    # 追加条目进入模板包且为 document 来源
    assert unmatched_veto["code"] in by_code
    assert by_code[unmatched_veto["code"]]["origin"] == "document"

    # ④ 签批剩余模板兜底 → 确认 → 冻结
    for c in pkg["criteria"]:
        if c["status"] == "needs_signoff":
            r = client.post(f"/api/rules/criteria/{c['id']}/signoff", json={"reason": "模板兜底启用"})
            assert r.status_code == 200, r.text
    r = client.post(f"/api/rules/packages/{target_id}/confirm")
    assert r.status_code == 200, r.text
    r = client.post(f"/api/rules/packages/{target_id}/freeze")
    assert r.status_code == 200, r.text
    assert r.json()["content_hash"]

    # ⑤ 覆盖率：document_backed = 覆盖 + 追加；retired = 1
    cov = client.get(f"/api/rules/packages/{target_id}/coverage").json()
    assert cov["retired"] == 1
    assert cov["document_backed"] >= len(overridden_codes) + 1
    assert cov["template_fallback_pending"] == 0

    # ⑥ 快照：被覆盖条目带 document 溯源；retired 不在快照中
    from app.db.models import RulePackage
    from app.db.session import session_scope

    with session_scope() as s:
        snap = s.get(RulePackage, target_id).snapshot_json
    snap_codes = {c["code"]: c for c in snap["criteria"]}
    for code in overridden_codes:
        c = snap_codes[code]
        assert c["origin"] == "document"
        assert c["template_code"] in ("T1.1", "T1.3", "T2.4")  # 编号稳定锚保留
    assert "T1.4" not in snap_codes


def test_merge_requires_template_package(client) -> None:  # noqa: ANN001
    """无模板包时 merge 编译应被拒绝。"""
    pid = client.post("/api/projects", json={"name": "无模板合并"}).json()["id"]
    from pathlib import Path

    # 用样例招标文件（app_env 下 samples 不存在则跳过：直接手工断言前先有文档）
    import pytest

    samples = Path(__file__).resolve().parents[2] / "data" / "samples" / "tender"
    if not samples.exists():
        pytest.skip("样例数据缺失")
    pdf = next(samples.glob("*招标文件.pdf"))
    out = upload_pdf(client, pid, pdf, "tender")
    job = wait_job(client, out["job_id"])
    assert job["status"] == "succeeded"
    resp = client.post(
        f"/api/projects/{pid}/rules/compile",
        json={"document_id": out["document_id"], "provider": "mock",
              "mode": "merge", "merge_target_id": None},
    )
    assert resp.status_code == 422, resp.text