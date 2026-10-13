#!/usr/bin/env python
"""法律通用规则包导入脚本（S-Law，进程内 TestClient，无需启动服务）。

用法：
    py -3 scripts/import_law_rules.py --dry-run [--package <json>]        # 纯校验，不落库
    py -3 scripts/import_law_rules.py --new-project "法律规则演示" --confirm --freeze
    py -3 scripts/import_law_rules.py --project PRJ-XXXX [--confirm] [--freeze]

参数：
    --package     默认 <工作区>/261010/文件/法规库/00_索引/生成_规则包_通用规则库全量.json
    --law-kb      默认 <同目录>/生成_法条知识库.json
    --refresh-kb  强制覆盖 vendoring 的知识库
    --dry-run     只跑 validate_only 打印 findings（error>0 → exit 2），不建 app、不写库
    --confirm     导入成功后顺带调既有 confirm 端点
    --freeze      （隐含 --confirm）冻结规则包

知识库路径（必需步骤）：confirm/freeze 的 run_validations 读 config.law_kb_path()。
导入路径会把 --law-kb vendoring 到该默认位置（BID_EVAL_LAW_KB_PATH 可覆盖）；
dry-run 是同进程纯内存校验，直接把该 env 指向源文件即可。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

WORKSPACE = REPO.parent  # ZJUBJ（RPF Agent 与 261010 同级）
DEFAULT_PKG = WORKSPACE / "261010/文件/法规库/00_索引/生成_规则包_通用规则库全量.json"
DEFAULT_KB = WORKSPACE / "261010/文件/法规库/00_索引/生成_法条知识库.json"


def _load_json(path: Path) -> dict:
    if not path.exists():
        print(f"文件不存在：{path}", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def _print_findings(findings: list[dict]) -> tuple[int, int]:
    errors = [f for f in findings if f.get("severity") == "error"]
    warns = [f for f in findings if f.get("severity") != "error"]
    for f in findings:
        print(f"  [{f.get('severity', '?').upper()}] {f.get('code')} "
              f"{f.get('subject_type')}:{f.get('subject_id') or '-'} {f.get('message')}")
    print(f"  合计：error {len(errors)}｜warn {len(warns)}")
    return len(errors), len(warns)


def _dry_run(pkg: dict, kb_source: Path) -> int:
    os.environ["BID_EVAL_LAW_KB_PATH"] = str(kb_source)  # 同进程直读源文件
    from app.rules.law_kb import reset_cache

    reset_cache()
    from app.services import law_import_service

    print(f"[dry-run] 包：{pkg.get('package', {}).get('name')} "
          f"v{pkg.get('package', {}).get('version')}｜条目 {len(pkg.get('criteria', []))}")
    print(f"[dry-run] 法条知识库：{kb_source}")
    try:
        findings = law_import_service.validate_only(pkg)
    except Exception as exc:  # noqa: BLE001 —— ValidationFailed(422) 等业务异常直接呈现
        print(f"[dry-run] 结构/法条核验失败：{exc}")
        detail = getattr(exc, "detail", None)
        for b in (detail or {}).get("blockers", [])[:20]:
            print(f"  - {b}")
        return 2
    errors, _ = _print_findings(findings)
    print("[dry-run] 校验通过，可执行导入。" if errors == 0
          else "[dry-run] 存在 error——导入后 confirm 将被阻断。")
    return 2 if errors else 0


def _ensure_law_kb(source: Path, refresh: bool) -> Path:
    """把知识库 vendoring 到 config.law_kb_path()（服务/校验读同一位置）。"""
    from app import config

    target = config.law_kb_path()
    try:
        same = source.resolve() == target.resolve()
    except OSError:
        same = False
    if same:
        return target
    if refresh or not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        print(f"[kb] 已 vendoring：{source} -> {target}")
    else:
        print(f"[kb] 已存在，保留：{target}（--refresh-kb 可覆盖）")
    return target


def _import(args: argparse.Namespace, pkg: dict) -> int:
    _ensure_law_kb(args.law_kb, args.refresh_kb)

    from app.db import session as db
    from app.main import create_app
    from fastapi.testclient import TestClient

    db.reset_engine()
    db.init_db()

    with TestClient(create_app()) as client:
        if args.new_project:
            pid = client.post("/api/projects", json={"name": args.new_project}).json()["id"]
            print(f"[project] 新建项目：{pid}")
        elif args.project:
            pid = args.project
            r = client.get(f"/api/projects/{pid}")
            if r.status_code != 200:
                print(f"项目不存在：{pid}", file=sys.stderr)
                return 4
        else:
            print("需要 --project <ID> 或 --new-project <名称>", file=sys.stderr)
            return 1

        r = client.post(f"/api/projects/{pid}/rules/import-law", json=pkg)
        if r.status_code != 200:
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            print(f"[import] 失败 HTTP {r.status_code}：{body.get('message') or r.text[:300]}")
            for b in (body.get("detail") or {}).get("blockers", [])[:20]:
                print(f"  - {b}")
            return 2 if r.status_code == 422 else 3 if r.status_code == 409 else 4

        out = r.json()
        print(f"[import] package_id={out['package_id']} v{out['version']} "
              f"created={out['created']} imported={out['imported']}")
        if out["skipped_codes"]:
            print(f"[import] 幂等跳过 {len(out['skipped_codes'])} 条："
                  f"{', '.join(out['skipped_codes'][:10])}{'…' if len(out['skipped_codes']) > 10 else ''}")
        for d in out["dropped_nodes"]:
            print(f"[import] 节点置空（非法律引用，无招标文件可核验）：{d['code']} -> {d['node_id']}")
        errors, warns = _print_findings(out["validations"])

        if args.confirm or args.freeze:
            rc = client.post(f"/api/rules/packages/{out['package_id']}/confirm")
            if rc.status_code != 200:
                print(f"[confirm] 失败 HTTP {rc.status_code}")
                for b in (rc.json().get("detail") or {}).get("blockers", [])[:20]:
                    print(f"  - {b}")
                return 2
            print("[confirm] 已确认（免签批）")
        if args.freeze:
            rc = client.post(f"/api/rules/packages/{out['package_id']}/freeze")
            if rc.status_code != 200:
                print(f"[freeze] 失败 HTTP {rc.status_code}")
                for b in (rc.json().get("detail") or {}).get("blockers", [])[:20]:
                    print(f"  - {b}")
                return 2
            print(f"[freeze] 已冻结 content_hash={rc.json().get('content_hash')}")

        if errors:
            print(f"提示：{errors} 条 error 仍待处理（confirm 已通过则不涉及）。")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="法律通用规则包导入（S-Law）")
    parser.add_argument("--package", type=Path, default=DEFAULT_PKG, help="规则包 JSON 路径")
    parser.add_argument("--law-kb", type=Path, default=DEFAULT_KB, help="法条知识库 JSON 路径")
    parser.add_argument("--project", help="已有项目 ID")
    parser.add_argument("--new-project", help="新建项目名称（与 --project 二选一）")
    parser.add_argument("--dry-run", action="store_true", help="只校验不落库")
    parser.add_argument("--confirm", action="store_true", help="导入后确认规则包")
    parser.add_argument("--freeze", action="store_true", help="确认后冻结（隐含 --confirm）")
    parser.add_argument("--refresh-kb", action="store_true", help="强制覆盖 vendoring 的知识库")
    args = parser.parse_args()

    if args.freeze:
        args.confirm = True

    pkg = _load_json(args.package)
    if args.dry_run:
        return _dry_run(pkg, args.law_kb)
    return _import(args, pkg)


if __name__ == "__main__":
    sys.exit(main())
