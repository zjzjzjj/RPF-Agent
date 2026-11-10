# -*- coding: utf-8 -*-
"""法规库清理 + 效力元数据回归（2026-10-21）。

审计后：草案（招标投标法修订草案）与已被取代的旧版（2002 政府采购法、2011
实施条例、2007 标准施工试行规定）已从 data/law_kb.json 删除；每条带
status/category 等效力元数据；84 条法规规则引用的 7 部法律必须仍在且现行有效。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_KB = Path(__file__).resolve().parents[2] / "data" / "law_kb.json"

CITED = [
    "中华人民共和国招标投标法实施条例（2019年修订）",
    "评标委员会和评标方法暂行规定",
    "工程建设项目施工招标投标办法（第30号，2013年第23号令修订）",
    "中华人民共和国招标投标法（2017年修正本）",
    "中华人民共和国政府采购法（2014年修正）",
    "电子招标投标办法",
    "政府采购货物和服务招标投标管理办法",
]

REMOVED = [
    "中华人民共和国招标投标法（修订草案公开征求意见稿）",
    "中华人民共和国招标投标法（修订草案公开征求意见稿）（现行规定与拟修订规定对照）",
    "中华人民共和国政府采购法",
    "中华人民共和国招标投标法实施条例",
    "《标准施工招标资格预审文件》和《标准施工招标文件》试行规定（九部委令第56号）",
]


@pytest.fixture()
def repo_kb(monkeypatch):
    monkeypatch.setenv("BID_EVAL_LAW_KB_PATH", str(REPO_KB))
    from app.rules import law_kb

    law_kb.reset_cache()
    return law_kb


def test_removed_drafts_and_superseded(repo_kb) -> None:
    names = {e.get("lawName") for e in json.loads(REPO_KB.read_text(encoding="utf-8"))}
    for bad in REMOVED:
        assert bad not in names, f"草案/旧版仍在库中：{bad}"
    for good in CITED:
        assert good in names, f"被引用法律缺失：{good}"


def test_cited_laws_are_current(repo_kb) -> None:
    meta = repo_kb.load_metadata()
    assert meta
    for name in CITED:
        assert meta[name]["status"] in ("现行有效", "尚未施行"), (name, meta[name]["status"])


def test_citations_still_resolve(repo_kb) -> None:
    rules_dir = (
        REPO_KB.parents[1]
        / "backend" / "app" / "rules" / "library" / "law_packs" / "general-law" / "rules"
    )
    nodes = []
    for f in rules_dir.glob("*.json"):
        node = json.loads(f.read_text(encoding="utf-8")).get("source_node_id") or ""
        if node.startswith("法条://"):
            nodes.append(node)
    assert nodes, "未找到法规规则"
    bad = [n for n in nodes if not repo_kb.node_exists(n)]
    assert not bad, f"法规引用无法解析：{bad[:5]}"


def test_new_orders_not_yet_effective(repo_kb) -> None:
    meta = repo_kb.load_metadata()
    for name in ("公共资源交易中心招标投标现场管理暂行办法", "招标投标领域信用管理暂行办法"):
        assert meta[name]["status"] == "尚未施行"
        assert meta[name]["effectiveDate"] == "2027-01-01"
