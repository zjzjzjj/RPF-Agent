"""清理法规知识库并合并效力元数据（一次性，幂等）。

做四件事：
1. 删除**草案/已被取代的旧版**（见 DELETE_EXACT）。
2. 删除**旧标题重复项**（见 MERGE_INTO，统一并入带版本/文号的现行标题）。
3. 同一法规的重复副本/附件碎片去重（每个规范名称只保留一条完整文本）。
4. 合并 `data/law_metadata.json` 的效力元数据（status/category/文号/施行日期…）。

安全约束：84 条法规规则引用的 7 个 `法条://法规名` **必须保留**，否则拒绝写入。
KB 的 `lawName` / `articles` 内容不改（只删多余条目、加元数据字段）。

用法（在仓库根目录）：
    python scripts/clean_law_kb.py --check   # 只报告，不写
    python scripts/clean_law_kb.py           # 执行清理并写回 data/law_kb.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KB_PATH = ROOT / "data" / "law_kb.json"
META_PATH = ROOT / "data" / "law_metadata.json"

# 84 条法规规则实际引用的法规名——清理后必须全部存在
CITED_REQUIRED = {
    "中华人民共和国招标投标法实施条例（2019年修订）",
    "评标委员会和评标方法暂行规定",
    "工程建设项目施工招标投标办法（第30号，2013年第23号令修订）",
    "中华人民共和国招标投标法（2017年修正本）",
    "中华人民共和国政府采购法（2014年修正）",
    "电子招标投标办法",
    "政府采购货物和服务招标投标管理办法",
}

# 草案 / 已被取代的旧版：整条删除
DELETE_EXACT = {
    # 草案（2026-06-23 仅一审，未通过未生效）
    "中华人民共和国招标投标法（修订草案公开征求意见稿）",
    "中华人民共和国招标投标法（修订草案公开征求意见稿）（现行规定与拟修订规定对照）",
    # 已被 2014 修正取代
    "中华人民共和国政府采购法",
    # 已被 2019 修订取代
    "中华人民共和国招标投标法实施条例",
    # 已被 2013 第23号令修订的“暂行规定”取代
    "《标准施工招标资格预审文件》和《标准施工招标文件》试行规定（九部委令第56号）",
}

# 旧标题/附件变体 → 并入带版本或文号的现行标题（删除键，保留值）
MERGE_INTO = {
    "工程建设项目施工招标投标办法": "工程建设项目施工招标投标办法（第30号，2013年第23号令修订）",
    "工程建设项目货物招标投标办法": "工程建设项目货物招标投标办法（第27号，2013年第23号令修订）",
    "政府采购评审专家管理办法": "政府采购评审专家管理办法（财库〔2016〕198号）",
    "政府采购进口产品管理办法": "政府采购进口产品管理办法（财库〔2007〕119号）",
    "国家发展改革委关于废止和修改部分招标投标规章和规范性文件的决定":
        "国家发展和改革委员会关于废止和修改部分招标投标规章和规范性文件的决定（九部委令第23号）",
    "财政部关于印发《政府采购需求管理办法》的通知": "政府采购需求管理办法",
    "中华人民共和国政府采购法（2002年6月29日通过，2014年8月31日修正）":
        "中华人民共和国政府采购法（2014年修正）",
    "上海市政府采购监管智能体应用管理办法（试行）（官方附件PDF）":
        "上海市政府采购监管智能体应用管理办法（试行）",
}


def canon(name: str) -> str:
    """去掉附件/另源件等文件拆分后缀（保留版本括号）。"""
    return re.sub(r"_(附件\d*(\.(pdf|docx|doc))?|另源件)$", "", name).strip()


def _rank(entry: dict, canonical: str) -> tuple:
    """同组内挑选“最完整正式”的条目：名称无后缀 > 有来源 URL > 条文数多。"""
    name = entry.get("lawName", "")
    return (
        name == canonical,                       # 精确原名优先
        bool((entry.get("sourceUrl") or "").strip()),
        int(entry.get("articleCount") or 0),
    )


def clean(kb: list[dict], meta: dict) -> tuple[list[dict], dict]:
    stats = {"input": len(kb), "deleted_exact": 0, "merged_away": 0, "deduped": 0}
    kept: list[dict] = []
    for e in kb:
        name = e.get("lawName", "")
        if name in DELETE_EXACT:
            stats["deleted_exact"] += 1
            continue
        if name in MERGE_INTO:
            stats["merged_away"] += 1
            continue
        kept.append(e)

    # 同规范名去重，保留最佳一条
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in kept:
        groups[canon(e.get("lawName", ""))].append(e)
    out: list[dict] = []
    for canonical, ents in groups.items():
        best = max(ents, key=lambda x: _rank(x, canonical))
        stats["deduped"] += len(ents) - 1
        out.append(best)

    # 合并效力元数据
    for e in out:
        name = e.get("lawName", "")
        m = meta.get(name) or {}
        e["status"] = m.get("status") or "待核实"
        e["category"] = m.get("category") or ""
        e["issuer"] = m.get("issuer") or ""
        e["docNumber"] = m.get("docNumber") or ""
        e["effectiveDate"] = m.get("effectiveDate") or ""
        e["repealedDate"] = m.get("repealedDate") or ""
        e["note"] = m.get("note") or ""

    out.sort(key=lambda x: x.get("lawName", ""))
    stats["output"] = len(out)
    return out, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只报告不写")
    args = ap.parse_args()

    kb = json.loads(KB_PATH.read_text(encoding="utf-8"))
    meta = json.loads(META_PATH.read_text(encoding="utf-8")) if META_PATH.exists() else {}
    cleaned, stats = clean(kb, meta)

    names = {e.get("lawName", "") for e in cleaned}
    missing = CITED_REQUIRED - names
    print("清理统计：", stats)
    print(f"被引用法规缺失：{len(missing)}")
    for m in sorted(missing):
        print("  MISSING:", m)
    if missing:
        print("拒绝写入：被引用法规名不能缺失（会让 84 条规则校验失败）", file=sys.stderr)
        return 2

    by_status: dict[str, int] = defaultdict(int)
    for e in cleaned:
        by_status[e.get("status", "?")] += 1
    print("效力分布：", dict(by_status))
    print("将删除的草案/旧版：", sorted(x for x in DELETE_EXACT if any(e.get('lawName') == x for e in kb)))
    print("将合并的旧标题：", sorted(set(MERGE_INTO) & {e.get('lawName') for e in kb}))

    if args.check:
        print("--check：未写入。")
        return 0
    KB_PATH.write_text(
        json.dumps(cleaned, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"已写入 {KB_PATH}（{len(cleaned)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
