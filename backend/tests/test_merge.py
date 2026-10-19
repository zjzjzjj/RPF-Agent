"""逐条覆盖（S1）测试（LLM-free）。

- ``deterministic_match`` 纯函数单元测试：exact/相似配对、category/score_type
  硬过滤、价格 1:1、贪心互斥、未匹配清单——不依赖 DB 与任何模型；
- resolve 全链路：DB 直种合并辅助包（与模板条目同名的文档条目）→ 匹配提案
  （纯确定性；LLM 辅助层在未配置 provider 时按设计降级为仅确定性）→
  override / append_document / drop_template → 签批兜底 → 确认 → 冻结 →
  快照溯源断言。

语义抽取式的 merge 编译（LLM 产出临时编译包）由 scripts/run_e2e.py 接真实
模型冒烟覆盖。
"""
from __future__ import annotations

from pathlib import Path

from app.rules.merger import deterministic_match, name_similarity

from .conftest import upload_pdf, wait_job

REPO = Path(__file__).resolve().parents[2]


def _instantiate(client, pid: str) -> int:  # noqa: ANN001
    r = client.post(f"/api/projects/{pid}/rules/instantiate",
                    json={"template_id": "comprehensive-goods"})
    assert r.status_code == 200, r.text
    return r.json()["package_id"]


# ---------------------------------------------------------------------------
# deterministic_match 纯函数单元测试（无 DB、无模型）
# ---------------------------------------------------------------------------
def _c(cid: int, code: str, name: str, category: str = "qualification",
       score_type: str = "veto", is_veto: bool = True) -> dict:
    return {"id": cid, "code": code, "name": name, "category": category,
            "score_type": score_type, "is_veto": is_veto, "max_score": None,
            "source_page": None, "source_quote": None, "origin": "document",
            "status": "verified"}


def test_deterministic_match_pairs_and_filters() -> None:
    template = [
        _c(1, "T1.1", "具有独立法人资格"),
        _c(2, "T1.3", "未被列入失信被执行人名单"),
        _c(3, "T1.4", "依法缴纳税收和社会保障资金的良好记录"),
        _c(4, "T3.1", "投标报价得分", "price", "formula", False),
        _c(5, "T5.1", "同类项目业绩", "commercial", "count", False),
    ]
    document = [
        _c(11, "2.1.1", "具有独立法人资格"),                      # exact
        _c(12, "2.1.3", "未被列入失信被执行人名单的承诺"),          # 高相似 → deterministic
        _c(13, "2.1.9", "安全生产许可证"),                        # 无语义对应 → 未匹配
        _c(14, "3.1", "投标报价得分", "price", "formula", False),  # price 1:1
        # 与 T5.1 同名但 category/score_type 不同 → 硬过滤，不得配对
        _c(15, "4.1", "同类项目业绩", "technical", "subjective", False),
    ]
    out = deterministic_match(template, document)
    pairs = {(p["template"]["id"], p["document"]["id"]): p["match_method"]
             for p in out["pairs"]}
    assert pairs[(1, 11)] == "exact"
    assert pairs[(2, 12)] == "deterministic"
    assert pairs[(4, 14)] in ("exact", "deterministic")  # 价格项天然 1:1
    # 硬过滤：同名不同型不得配对
    assert (5, 15) not in pairs
    # 未匹配集合与配对精确互补
    assert [t["id"] for t in out["unmatched_template"]] == [3, 5]
    assert [d["id"] for d in out["unmatched_document"]] == [13, 15]


def test_deterministic_match_greedy_mutual_exclusion() -> None:
    """一个文档条目只能被配一次：贪心取先到者，其余落未匹配。"""
    template = [_c(1, "T1", "具有独立法人资格"), _c(2, "T2", "具有独立法人资格")]
    document = [_c(11, "2.1", "具有独立法人资格")]
    out = deterministic_match(template, document)
    assert len(out["pairs"]) == 1
    assert out["pairs"][0]["template"]["id"] == 1
    assert out["pairs"][0]["document"]["id"] == 11
    assert [t["id"] for t in out["unmatched_template"]] == [2]


def test_name_similarity_thresholds() -> None:
    assert name_similarity("具有独立法人资格", "具有独立法人资格") == 1.0
    # 语义无关的两个名称必须低于确定性阈值（0.55 / 0.75）
    assert name_similarity("具有独立法人资格", "未被列入失信被执行人名单") < 0.55


# ---------------------------------------------------------------------------
# resolve 全链路（DB 直种合并辅助包，纯确定性匹配）
# ---------------------------------------------------------------------------
def _seed_merge_context(client):  # noqa: ANN001
    """不经编译、直接 DB 种合并辅助包：文档条目与模板条目同名（exact 配对）。

    返回 (pid, target_id, merge_package_id)。
    """
    pid = client.post("/api/projects", json={"name": "逐条覆盖测试"}).json()["id"]
    target_id = _instantiate(client, pid)
    from app.db.models import Criterion, CriterionCheck, RulePackage
    from app.db.session import session_scope

    with session_scope() as s:
        merge = RulePackage(project_id=pid, version=2, status="draft",
                            merge_target_id=target_id, source_document_id=None,
                            compiled_by="test", created_by="test")
        s.add(merge)
        s.flush()
        doc_specs = [
            ("2.1.1", "具有独立法人资格", "qualification"),
            ("2.2.1", "投标函已按要求签署并加盖公章", "compliance"),
            ("2.2.2", "投标保证金已按招标文件要求缴纳", "compliance"),
            ("2.2.9", "履约保证金已按合同约定缴纳", "compliance"),  # 模板无对应 → 追加
            ("3.1", "投标报价得分", "price"),
        ]
        for i, (code, name, cat) in enumerate(doc_specs, start=1):
            is_price = cat == "price"
            c = Criterion(package_id=merge.id, code=code, category=cat, name=name,
                          score_type="formula" if is_price else "veto",
                          is_veto=not is_price,
                          veto_consequence=None if is_price else "disqualify",
                          description=f"{name}（招标文件逐字条款）",
                          source_quote=f"招标文件原文：{name}",
                          source_page=20 + i, origin="document", status="verified",
                          sort_order=i)
            s.add(c)
            s.flush()
            if not is_price:
                s.add(CriterionCheck(criterion_id=c.id, check_type="document_present",
                                     params_json={"document": name[:12]},
                                     on_fail="recommend_reject", source_quote=c.source_quote))
        merge_package_id = merge.id
    return pid, target_id, merge_package_id


def test_match_proposal_structure(client) -> None:  # noqa: ANN001
    pid, target_id, merge_package_id = _seed_merge_context(client)
    resp = client.get(f"/api/rules/packages/{merge_package_id}/match-proposal")
    assert resp.status_code == 200, resp.text
    prop = resp.json()
    assert prop["target_package_id"] == target_id
    assert prop["pairs"], "确定性匹配应产出至少 1 对"
    for p in prop["pairs"]:
        assert p["pair_id"] and p["match_method"] in ("exact", "deterministic")
        assert p["template"]["id"] and p["document"]["id"]
    assert prop["unmatched_template"]
    assert prop["unmatched_document"]
    # 未匹配集合与配对集合不相交
    pair_doc_ids = {p["document"]["id"] for p in prop["pairs"]}
    pair_tpl_ids = {p["template"]["id"] for p in prop["pairs"]}
    assert all(d["id"] not in pair_doc_ids for d in prop["unmatched_document"])
    assert all(t["id"] not in pair_tpl_ids for t in prop["unmatched_template"])


def test_resolve_override_append_drop(client) -> None:  # noqa: ANN001
    pid, target_id, merge_package_id = _seed_merge_context(client)
    prop = client.get(f"/api/rules/packages/{merge_package_id}/match-proposal").json()

    # ① 覆盖全部"否决项"配对（不影响 TOTAL_SCORE）
    decisions = [
        {"action": "override", "pair_id": p["pair_id"],
         "reason": "与招标文件逐字条款核对一致"}
        for p in prop["pairs"] if p["template"]["is_veto"]
    ]
    assert len(decisions) == 3, prop["pairs"]  # T1.1 / T2.1 / T2.3
    # ② 追加一条未匹配的文档否决项
    unmatched_veto = next(d for d in prop["unmatched_document"] if d["is_veto"])
    decisions.append({"action": "append_document",
                      "document_criterion_id": unmatched_veto["id"],
                      "reason": "招标文件明确规定的否决项，模板中无对应条目"})
    # ③ 弃用一个未匹配模板条目（缴纳税收社保 → retired）
    drop_tpl = next(t for t in prop["unmatched_template"] if t["code"] == "T1.4")
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
    overridden_codes = sorted(p["template"]["code"] for p in prop["pairs"]
                              if p["template"]["is_veto"])
    assert overridden_codes == ["T1.1", "T2.1", "T2.3"]
    for code in overridden_codes:
        c = by_code[code]
        assert c["origin"] == "document", (code, c["origin"])
        assert c["status"] == "verified", (code, c["status"])
        assert c["source_quote"], (code, "覆盖后应带有招标文件原文")
        assert c["override_json"] is not None
        assert c["override_json"]["won_source"] in ("P1", "P2", "P3", "P4")
        assert c["override_json"]["lost_source"] == "P5"
        # 检查原语随覆盖迁入（fields 级替换含 checks 全删全建）
        assert c["checks"] and c["checks"][0]["check_type"] == "document_present"
    # 弃用条目 → retired
    assert by_code["T1.4"]["status"] == "retired", by_code["T1.4"]["status"]
    # 追加条目进入模板包且为 document 来源（含 checks）
    assert unmatched_veto["code"] in by_code
    appended = by_code[unmatched_veto["code"]]
    assert appended["origin"] == "document"
    assert appended["checks"], "追加条目应带检查原语"

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
        assert c["template_code"] in ("T1.1", "T2.1", "T2.3")  # 编号稳定锚保留
    assert "T1.4" not in snap_codes


def test_merge_requires_template_package(client) -> None:  # noqa: ANN001
    """无模板包时 merge 编译应被拒绝（请求校验层 422，不产生任务）。"""
    pid = client.post("/api/projects", json={"name": "无模板合并"}).json()["id"]
    samples = Path(__file__).resolve().parents[2] / "data" / "samples" / "tender"
    if not samples.exists():
        import pytest

        pytest.skip("样例数据缺失")
    pdf = next(samples.glob("*招标文件.pdf"))
    out = upload_pdf(client, pid, pdf, "tender")
    job = wait_job(client, out["job_id"])
    assert job["status"] == "succeeded"
    resp = client.post(
        f"/api/projects/{pid}/rules/compile",
        json={"document_id": out["document_id"],
              "mode": "merge", "merge_target_id": None},
    )
    assert resp.status_code == 422, resp.text
