"""法规通用规则包（law_packs/general-law）固化测试。

覆盖：包加载与规模（87 条 = FORM37/QUAL23/RES12/REJ15）、文件名==code、
法条引用 ∈ policies.json（21 项）、非法条引用 ∈ external_refs（5 条）、
法条节点对仓库 data/law_kb.json 全解析、内容哈希稳定、
导入 → 确认 → 冻结全链、坏包 fail-fast、build 脚本 --check 护栏。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.rules import law_kb as lk
from app.rules.freeze import canonical_json
from app.rules.library.law_pack_loader import (
    LawPackError,
    default_pack_dir,
    load_law_pack,
    law_pack_content_hash,
    reset_law_pack_cache,
)

REPO = Path(__file__).resolve().parents[2]
PACK_DIR = default_pack_dir()
REPO_KB = REPO / "data" / "law_kb.json"
ARTIFACT = Path(r"E:\ZJUBJ\法规库\00_索引\生成_规则包_通用规则库全量.json")

EXPECTED_DIST = {"FORM": 37, "QUAL": 23, "RES": 12, "REJ": 15}
EXPECTED_EXTERNAL = {"招标文件://投标人须知前附表"}


@pytest.fixture(scope="module")
def pack_doc() -> dict:
    return load_law_pack()


# ---------------------------------------------------------------------------
# 加载与规模
# ---------------------------------------------------------------------------
def test_pack_loads_and_counts(pack_doc: dict) -> None:
    criteria = pack_doc["criteria"]
    assert len(criteria) == 87
    dist: dict[str, int] = {}
    for c in criteria:
        prefix = str(c["code"]).split("-")[0]
        dist[prefix] = dist.get(prefix, 0) + 1
    assert dist == EXPECTED_DIST, dist

    meta = pack_doc["package"]
    assert meta["id"] == "general-law"
    assert meta["source"] == "LAW"
    assert meta["version"]

    codes = [c["code"] for c in criteria]
    assert len(codes) == len(set(codes)), "code 必须全包唯一"

    # 组序 = FORM→QUAL→RES→REJ（sort_order 语义）
    order = ["FORM", "QUAL", "RES", "REJ"]
    groups = [c["code"].split("-")[0] for c in criteria]
    assert groups == sorted(groups, key=order.index)


def test_rule_filename_matches_code() -> None:
    rule_files = sorted((PACK_DIR / "rules").glob("*.json"))
    assert len(rule_files) == 87
    for path in rule_files:
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["code"] == path.stem, f"{path.name} 的 code 与文件名不一致"


# ---------------------------------------------------------------------------
# 法条登记与引用白名单
# ---------------------------------------------------------------------------
def test_law_nodes_covered_by_policies(pack_doc: dict) -> None:
    policies = json.loads((PACK_DIR / "policies.json").read_text(encoding="utf-8"))
    assert len(policies) == 21

    law_nodes = {c["source_node_id"] for c in pack_doc["criteria"]
                 if (c.get("source_node_id") or "").startswith("法条://")}
    assert law_nodes, "87 条里必须有法条引用"
    for node in law_nodes:
        entry = policies.get(node)
        assert entry is not None, f"{node} 未在 policies.json 登记"
        assert str(entry["text"]).strip(), f"{node} 的法条原文为空"
        assert entry["law_name"] and entry["article_no"]


def test_non_law_nodes_declared(pack_doc: dict) -> None:
    meta = json.loads((PACK_DIR / "pack.json").read_text(encoding="utf-8"))
    non_law = {c["source_node_id"] for c in pack_doc["criteria"]
               if c.get("source_node_id") and not c["source_node_id"].startswith("法条://")}
    assert non_law == EXPECTED_EXTERNAL
    assert non_law <= set(meta["external_refs"])


# ---------------------------------------------------------------------------
# 包 ↔ 仓库知识库一致性
# ---------------------------------------------------------------------------
def test_nodes_resolve_against_repo_kb(pack_doc: dict, monkeypatch) -> None:  # noqa: ANN001
    assert REPO_KB.exists(), "仓库 data/law_kb.json 缺失（vendoring 被删？）"
    monkeypatch.setenv("BID_EVAL_LAW_KB_PATH", str(REPO_KB))
    lk.reset_cache()
    try:
        nodes = {c["source_node_id"] for c in pack_doc["criteria"]
                 if (c.get("source_node_id") or "").startswith("法条://")}
        assert len(nodes) == 21
        unresolved = [n for n in nodes if not lk.node_exists(n)]
        assert unresolved == [], f"以下法条在仓库知识库中无法解析：{unresolved}"
    finally:
        lk.reset_cache()


def test_content_hash_stable() -> None:
    h1 = law_pack_content_hash()
    doc1 = load_law_pack()
    reset_law_pack_cache()
    assert law_pack_content_hash() == h1 and len(h1) == 16
    assert canonical_json(load_law_pack()) == canonical_json(doc1)


# ---------------------------------------------------------------------------
# 导入 → 确认 → 冻结（仓库包 + 仓库知识库，全程离线）
# ---------------------------------------------------------------------------
def test_import_confirm_freeze_from_pack(client, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("BID_EVAL_LAW_KB_PATH", str(REPO_KB))
    lk.reset_cache()
    try:
        pid = client.post("/api/projects", json={"name": "固定规则包演示"}).json()["id"]
        doc = load_law_pack()
        r = client.post(f"/api/projects/{pid}/rules/import-law", json=doc)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["imported"] == 87
        assert len(out["dropped_nodes"]) == 5  # 招标文件:// 前附表组
        assert all(v["severity"] != "error" for v in out["validations"]), out["validations"]

        assert client.post(f"/api/rules/packages/{out['package_id']}/confirm").status_code == 200
        r = client.post(f"/api/rules/packages/{out['package_id']}/freeze")
        assert r.status_code == 200, r.text
        assert r.json()["content_hash"]

        cov = client.get(f"/api/rules/packages/{out['package_id']}/coverage").json()
        assert cov["law"] == 87
        assert cov["template_fallback_pending"] == 0
    finally:
        lk.reset_cache()


# ---------------------------------------------------------------------------
# 坏包 fail-fast
# ---------------------------------------------------------------------------
def _make_pack(tmp_path: Path, name: str, mutate) -> Path:  # noqa: ANN001
    """复制真实包到 tmp 后施加变异，供 fail-fast 用例。"""
    dst = tmp_path / name
    (dst / "rules").mkdir(parents=True)
    (dst / "pack.json").write_text((PACK_DIR / "pack.json").read_text(encoding="utf-8"),
                                   encoding="utf-8")
    (dst / "policies.json").write_text((PACK_DIR / "policies.json").read_text(encoding="utf-8"),
                                       encoding="utf-8")
    src = json.loads((PACK_DIR / "rules" / "FORM-001.json").read_text(encoding="utf-8"))
    mutate(src)
    (dst / "rules" / "FORM-001.json").write_text(
        json.dumps(src, ensure_ascii=False, indent=2), encoding="utf-8")
    return dst


def test_bad_pack_fails_fast(tmp_path: Path) -> None:  # noqa: ANN001
    # 1) code != 文件名
    with pytest.raises(LawPackError, match="FORM-001"):
        load_law_pack(_make_pack(tmp_path, "a", lambda r: r.update({"code": "FORM-999"})))

    # 2) 未知法条节点（policies 无登记）
    with pytest.raises(LawPackError, match="policies"):
        load_law_pack(_make_pack(
            tmp_path, "b", lambda r: r.update({"source_node_id": "法条://不存在的法规/第一条"})))

    # 3) 非法条前缀未在 external_refs 声明
    with pytest.raises(LawPackError, match="external_refs"):
        load_law_pack(_make_pack(
            tmp_path, "c", lambda r: r.update({"source_node_id": "规范://某规范/第一条"})))

    # 4) rule_count 与实际文件数不一致（真实包声明 87，坏包只有 1 条）
    with pytest.raises(LawPackError, match="rule_count"):
        load_law_pack(_make_pack(tmp_path, "d", lambda r: r))

    # 5) policies 条目 text 为空
    p5 = tmp_path / "e"
    (p5 / "rules").mkdir(parents=True)
    (p5 / "pack.json").write_text((PACK_DIR / "pack.json").read_text(encoding="utf-8"),
                                  encoding="utf-8")
    policies = json.loads((PACK_DIR / "policies.json").read_text(encoding="utf-8"))
    first = next(iter(policies))
    policies[first]["text"] = ""
    (p5 / "policies.json").write_text(json.dumps(policies, ensure_ascii=False),
                                      encoding="utf-8")
    src = json.loads((PACK_DIR / "rules" / "FORM-001.json").read_text(encoding="utf-8"))
    (p5 / "rules" / "FORM-001.json").write_text(json.dumps(src, ensure_ascii=False),
                                                encoding="utf-8")
    with pytest.raises(LawPackError, match="text"):
        load_law_pack(p5)


# ---------------------------------------------------------------------------
# build 脚本 --check 护栏（依赖外部制品，缺失时跳过）
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not ARTIFACT.exists(),
                    reason="本地法规库制品缺失（E:\\ZJUBJ\\法规库）")
def test_build_script_check() -> None:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "build_law_pack.py"), "--check"],
        cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    assert r.returncode == 0, r.stdout + r.stderr
