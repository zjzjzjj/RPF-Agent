# -*- coding: utf-8 -*-
"""批量重新解析项目文档（服务器运维脚本）。

用途：OCR 引擎/解析逻辑修复后，让已入库的文档重新走一遍解析（扫描件内容才
会真正进入 DocumentPage 文本）。**只重解析、不重跑评分**——旧的 score_records /
evidence_citations 仍指向旧页文本，必须在新规则包冻结后另起一次评分。

为什么不用重传：重传会新建 document 行（sha256 重复）、投标人挂接要重做；
`POST /documents/{id}/reparse` 是幂等的（先删 DocNode/DocumentPage 再写）。

安全护栏：重解析招标文件会让 `criteria.source_node_id` 失效（形如
`tender11:root:C12:SEC6:I5:N1`），因此默认只重解析标书，并在前后核对招标文档的
node_id 集合未变——变了立即告警。

用法（在服务器仓库根目录）：
    python scripts/reparse_documents.py --project PRJ-20261021-A1G9 --kind bid
    python scripts/reparse_documents.py --project PRJ-20261021-A1G9 --kind bid --ids 12,13
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _get(url: str, timeout: float = 30.0) -> dict | list:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(url: str, timeout: float = 60.0) -> dict:
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _node_ids(db: Path, document_id: int) -> set[str]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT node_id FROM doc_nodes WHERE document_id=?", (document_id,)
        ).fetchall()
    finally:
        con.close()
    return {r[0] for r in rows}


def _source_stats(db: Path, document_id: int) -> list[tuple[str, int]]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT source, COUNT(*) FROM document_pages WHERE document_id=?"
            " GROUP BY source ORDER BY 2 DESC",
            (document_id,),
        ).fetchall()
    finally:
        con.close()


def _warnings(db: Path, document_id: int) -> list[str]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT warnings_json FROM documents WHERE id=?", (document_id,)
        ).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        return []
    try:
        # warnings_json 条目混有 dict 与裸字符串（如"材料页 OCR 救援：第 111 页"）
        return [
            str(w.get("message") or w) if isinstance(w, dict) else str(w)
            for w in json.loads(row[0])
        ]
    except (TypeError, ValueError):
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="项目 ID，如 PRJ-20261021-A1G9")
    ap.add_argument("--kind", default="bid", choices=["bid", "tender", "addendum", "attachment"])
    ap.add_argument("--ids", default="", help="只重解析这些 document id（逗号分隔）")
    ap.add_argument("--api", default="http://127.0.0.1:18200", help="服务地址")
    ap.add_argument("--db", default=str(REPO / "data" / "bid_eval.db"))
    ap.add_argument("--timeout", type=float, default=7200.0, help="单文档等待上限（秒）")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"[x] 数据库不存在：{db}", file=sys.stderr)
        return 2

    docs = _get(f"{args.api}/api/projects/{args.project}/documents")
    wanted = {int(x) for x in args.ids.split(",") if x.strip()} if args.ids else None
    targets = [
        d for d in docs
        if d.get("kind") == args.kind and (wanted is None or d["id"] in wanted)
    ]
    if not targets:
        print(f"[!] 没有 kind={args.kind} 的文档可重解析")
        return 1

    # 护栏：记录所有招标文档的 node_id 集合（重解析标书不应影响它们）
    tender_ids = [d["id"] for d in docs if d.get("kind") == "tender"]
    tender_nodes = {i: _node_ids(db, i) for i in tender_ids}

    print(f"将重解析 {len(targets)} 份文档（kind={args.kind}）")
    for d in targets:
        before = dict(_source_stats(db, d["id"]))
        print(f"\n=== doc {d['id']} {d.get('original_filename')} 当前 {before}")

    jobs: list[tuple[dict, int]] = []
    for d in targets:
        try:
            out = _post(f"{args.api}/api/documents/{d['id']}/reparse")
        except urllib.error.HTTPError as exc:
            print(f"[x] doc {d['id']} 提交失败：{exc.code} {exc.reason}")
            continue
        job_id = out.get("job_id")
        print(f"[>] doc {d['id']} 已提交，job {job_id}")
        jobs.append((d, int(job_id)))

    failed = 0
    for d, job_id in jobs:
        deadline = time.time() + args.timeout
        status = "unknown"
        while time.time() < deadline:
            try:
                job = _get(f"{args.api}/api/jobs/{job_id}")
            except (urllib.error.URLError, OSError) as exc:
                print(f"    job {job_id} 查询失败：{exc}")
                time.sleep(5)
                continue
            status = str(job.get("status"))
            if status in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(5)
        if status != "succeeded":
            failed += 1
        print(f"[{'+' if status == 'succeeded' else 'x'}] doc {d['id']} job {job_id} → {status}")
        stats = _source_stats(db, d["id"])
        print(f"    页来源分布：{dict(stats)}")
        for w in _warnings(db, d["id"])[:6]:
            print(f"    warn: {w}")
        ocr_pages = sum(n for s, n in stats if s in ("ocr", "ocr_img"))
        if ocr_pages == 0:
            print("    [!] 本份文档零 OCR 页——检查 OCR 引擎配置/探针（warnings 首条会写实际引擎）")

    # 招标文档节点集合必须不变（否则冻结包的 source_node_id 全部失效）
    broken = []
    for i, before in tender_nodes.items():
        after = _node_ids(db, i)
        if after != before:
            broken.append((i, len(before), len(after)))
    if broken:
        print(f"\n[x] 招标文档节点集合发生变化：{broken}")
        print("    criteria.source_node_id 可能失效——请勿在修复前重新冻结规则包")
        return 3
    if tender_ids:
        print(f"\n[+] 招标文档节点集合未变（{len(tender_ids)} 份，已核对）")

    print(f"\n完成：{len(jobs) - failed} 成功 / {failed} 失败")
    print("提醒：重解析不会重跑评分。请新建规则包版本 → 确认 → 冻结 → 再发起评分。")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
