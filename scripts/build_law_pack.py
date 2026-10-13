#!/usr/bin/env python
"""法规库制品 → 仓库内固定通用规则包（law_packs/general-law/）的生成/同步脚本。

这是包的唯一生成通道：`backend/app/rules/library/law_packs/general-law/` 下的
90 个文件全部由本脚本从法规库制品生成并提交 git——**禁止手改包内 rules/*.json**
（`--check` 会抓漂移）。法规库更新后重跑本脚本即可同步。

用法：
    python scripts/build_law_pack.py                    # 生成/同步到仓库包目录
    python scripts/build_law_pack.py --check            # 只比对，漂移则 exit 2（测试/CI 用）
    python scripts/build_law_pack.py --artifact <json> --law-kb <json>   # 显式指定制品
    python scripts/build_law_pack.py --set-version 0.3.0 # 内容变化时人工定版

幂等：同一制品重跑输出逐字节一致（pack.json 记 artifact_sha256_16，不含时间戳）；
无变化时零写入。收尾自检：law_pack_loader 全量校验 + law_import_service.validate_only
（error 必须为 0），失败 exit 2。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 法规库制品默认位置（目录曾从 261010/文件/法规库 上移至顶层，勿再用旧路径）
DEFAULT_ARTIFACT = Path(r"E:\ZJUBJ\法规库\00_索引\生成_规则包_通用规则库全量.json")
DEFAULT_LAW_KB = Path(r"E:\ZJUBJ\法规库\00_索引\生成_法条知识库.json")
REPO_KB = REPO / "data" / "law_kb.json"
PACK_DIR = REPO / "backend" / "app" / "rules" / "library" / "law_packs" / "general-law"
LAW_PREFIX = "法条://"

# pack.json 字段序（固定清单，保证重跑输出稳定）
_META_ORDER = (
    "id", "version", "extends", "name", "description", "scope", "source",
    "total_max", "status", "enabled", "rule_count", "external_refs", "generated_from",
)
_GEN_ORDER = ("artifact", "artifact_sha256_16", "law_nodes", "non_law_criteria")


def _load_json(path: Path) -> dict:
    if not path.exists():
        print(f"文件不存在：{path}", file=sys.stderr)
        sys.exit(2)
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def _bump_patch(version: str) -> str:
    parts = version.split(".")
    while len(parts) < 3:
        parts.append("0")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def _split_node(node_id: str) -> tuple[str, str]:
    rest = node_id[len(LAW_PREFIX):]
    law_name, _, article_no = rest.rpartition("/")
    return law_name, article_no


def _kb_article_index(kb_doc: list) -> dict[str, dict]:
    """法名 → {条号: {text, sourceFile, sourceUrl}}（NFKC 归一在核验侧做，这里直接原文键）。"""
    index: dict[str, dict] = {}
    for law in kb_doc if isinstance(kb_doc, list) else []:
        name = str(law.get("lawName") or "")
        if not name:
            continue
        entries = index.setdefault(name, {})
        for art in law.get("articles") or []:
            no = str(art.get("articleNo") or "")
            if no:
                entries[no] = {
                    "text": str(art.get("text") or ""),
                    "source_file": str(law.get("sourceFile") or ""),
                    "source_url": str(law.get("sourceUrl") or ""),
                }
    return index


def _node_exists(index: dict[str, dict], node_id: str) -> bool:
    law_name, article_no = _split_node(node_id)
    return article_no in index.get(law_name, {})


def _sorted_criteria(criteria: list[dict]) -> list[dict]:
    def key(raw: dict):
        m = __import__("re").match(r"^([A-Z]+)-(\d+)$", str(raw.get("code") or ""))
        order = ("FORM", "QUAL", "RES", "REJ")
        if not m:
            return (len(order), str(raw.get("code") or ""), 0)
        prefix, seq = m.group(1), int(m.group(2))
        group = order.index(prefix) if prefix in order else len(order)
        return (group, prefix, seq)

    return sorted(criteria, key=key)


def main() -> int:
    parser = argparse.ArgumentParser(description="法规库制品 → 仓库内固定通用规则包")
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT,
                        help="法规规则包制品 JSON（默认法规库 00_索引 全量包）")
    parser.add_argument("--law-kb", type=Path, default=DEFAULT_LAW_KB,
                        help="法条知识库制品 JSON（与 --artifact 同目录）")
    parser.add_argument("--check", action="store_true", help="只比对不写入，漂移 exit 2")
    parser.add_argument("--set-version", help="内容变化时使用的包版本（缺省自动 bump patch）")
    args = parser.parse_args()

    from app.rules.freeze import content_hash

    artifact_doc = _load_json(args.artifact)
    meta_src = artifact_doc.get("package") or {}
    criteria = artifact_doc.get("criteria") or []
    if not criteria:
        print("制品 criteria 为空", file=sys.stderr)
        return 2

    artifact_sha = content_hash(artifact_doc)

    # ------------------------------------------------------------------
    # 1. 收集法条引用 → 双端（外部制品 KB + 仓库 vendored KB）核验
    # ------------------------------------------------------------------
    criteria_sorted = _sorted_criteria(criteria)
    law_nodes: list[str] = []
    non_law: list[str] = []
    for raw in criteria_sorted:
        node = str(raw.get("source_node_id") or "")
        if not node:
            continue
        (law_nodes if node.startswith(LAW_PREFIX) else non_law).append(node)

    kb_doc = _load_json(args.law_kb)
    kb_index = _kb_article_index(kb_doc)
    policies: dict[str, dict] = {}
    missing: list[str] = []
    for node in dict.fromkeys(law_nodes):  # 去重保序
        if not _node_exists(kb_index, node):
            missing.append(node)
            continue
        law_name, article_no = _split_node(node)
        art = kb_index[law_name][article_no]
        policies[node] = {
            "text": art["text"],
            "law_name": law_name,
            "article_no": article_no,
            "source_file": art["source_file"],
            "source_url": art["source_url"],
        }
    if missing:
        print(f"{len(missing)} 条法条引用在外部知识库中不存在（零写入）：", file=sys.stderr)
        for n in missing:
            print(f"  - {n}", file=sys.stderr)
        return 2

    repo_kb_doc = _load_json(REPO_KB) if REPO_KB.exists() else []
    repo_missing = [n for n in dict.fromkeys(law_nodes)
                    if not _node_exists(_kb_article_index(repo_kb_doc), n)]
    if repo_missing:
        print(f"{len(repo_missing)} 条法条引用在仓库 data/law_kb.json 中不存在"
              "（先用 import_law_rules.py --refresh-kb 重新 vendoring）：", file=sys.stderr)
        for n in repo_missing:
            print(f"  - {n}", file=sys.stderr)
        return 2

    # ------------------------------------------------------------------
    # 2. 组装三件目标内容（字段序固定；无时间戳，幂等靠 artifact_sha256_16）
    # ------------------------------------------------------------------
    existing_meta: dict = {}
    existing_meta_path = PACK_DIR / "pack.json"
    if existing_meta_path.exists():
        try:
            existing_meta = json.loads(existing_meta_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 —— 损坏时视为无旧包
            existing_meta = {}

    generated_from = {
        "artifact": args.artifact.name,
        "artifact_sha256_16": artifact_sha,
        "law_nodes": len(policies),
        "non_law_criteria": len(non_law),
    }
    gen_ordered = {k: generated_from[k] for k in _GEN_ORDER}
    meta_ordered = {
        "id": "general-law",
        "version": existing_meta.get("version") or args.set_version or "1.0.0",
        "extends": None,
        "name": str(meta_src.get("name") or "法律通用规则包"),
        "description": meta_src.get("description"),
        "scope": "law",
        "source": meta_src.get("source") or "LAW",
        "total_max": meta_src.get("total_max"),
        "status": "draft",
        "enabled": True,
        "rule_count": len(criteria_sorted),
        "external_refs": sorted({n for n in non_law}),
        "generated_from": gen_ordered,
    }
    meta_ordered = {k: meta_ordered[k] for k in _META_ORDER}

    targets: dict[Path, str] = {
        PACK_DIR / "policies.json": _dump(dict(sorted(policies.items()))),
        **{
            PACK_DIR / "rules" / f"{raw['code']}.json": _dump(raw)
            for raw in criteria_sorted
        },
    }

    # ------------------------------------------------------------------
    # 3. 幂等写入（--check 只比对）
    # ------------------------------------------------------------------
    if (PACK_DIR / "rules").is_dir():
        for path in (PACK_DIR / "rules").glob("*.json"):
            if path not in targets:
                if args.check:
                    print(f"[drift] 包内多余文件：{path.name}")
                else:
                    path.unlink()
                    print(f"[clean] 已删除包内多余文件：{path.name}")

    changed: list[Path] = []
    for path, content in targets.items():
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            changed.append(path)

    meta_changed = (
        not existing_meta_path.exists()
        or existing_meta_path.read_text(encoding="utf-8") != _dump(meta_ordered)
    )
    if args.check:
        if changed or meta_changed:
            print(f"[check] 包与制品漂移：{len(changed)} 个文件变化"
                  + ("（含 pack.json）" if meta_changed else ""))
            for p in changed[:10]:
                print(f"  - {p.relative_to(REPO)}")
            return 2
        print("[check] 包与制品一致，无漂移。")
        return 0

    if not changed and not meta_changed:
        print(f"[build] 无变化：87 条规则与制品同步（artifact_sha256_16={artifact_sha}）。")
        return 0

    if not existing_meta:
        # 首次构建：直接用 meta 里的缺省版本，不做 bump
        new_version = str(meta_ordered["version"])
        print(f"[build] 首次构建：包版本 {new_version}，"
              f"artifact_sha256_16={artifact_sha}。")
    else:
        new_version = args.set_version or _bump_patch(str(meta_ordered["version"]))
        if not args.set_version:
            print(f"[build] 内容有变化：版本 {meta_ordered['version']} -> {new_version}"
                  "（--set-version 可人工定版；minor 及以上升级请人工评估）")
    meta_ordered["version"] = new_version
    targets[existing_meta_path] = _dump(meta_ordered)

    (PACK_DIR / "rules").mkdir(parents=True, exist_ok=True)
    for path, content in sorted(targets.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    print(f"[build] 已写入：policies 1 + rules {len(criteria_sorted)} + pack.json 1"
          f"（变化 {len(targets)} 个文件），artifact_sha256_16={artifact_sha}")

    # ------------------------------------------------------------------
    # 4. 收尾自检：包加载 + 导入侧纯内存校验（error 必须为 0）
    # ------------------------------------------------------------------
    sys.path.insert(0, str(REPO / "backend"))
    import os

    os.environ["BID_EVAL_LAW_KB_PATH"] = str(REPO_KB)
    from app.rules.library.law_pack_loader import (  # noqa: E402
        law_pack_content_hash,
        load_law_pack,
        reset_law_pack_cache,
    )

    reset_law_pack_cache()
    doc = load_law_pack()
    print(f"[self-check] 包加载成功：{len(doc['criteria'])} 条，"
          f"content_hash={law_pack_content_hash()}")

    from app.services import law_import_service  # noqa: E402

    findings = law_import_service.validate_only(doc)
    errors = [f for f in findings if f.get("severity") == "error"]
    if errors:
        print(f"[self-check] validate_only 存在 {len(errors)} 条 error：", file=sys.stderr)
        for f in errors[:10]:
            print(f"  [{f.get('code')}] {f.get('message')}", file=sys.stderr)
        return 2
    print("[self-check] validate_only 通过（error 0）。")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(REPO / "backend"))
    sys.exit(main())
