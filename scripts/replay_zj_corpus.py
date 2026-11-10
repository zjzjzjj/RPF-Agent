# -*- coding: utf-8 -*-
"""21 文件回放：评分细则确定性抽取回归基线（零 LLM）。

用途（docs/评分规则库改进方案.md §6.3）：
- 骨架闭合率：parse_skeleton 在每份语料上 closed() 或显式告警（不静默）；
- 价格六变体确定性识别率：parse_price_formula_text 命中六变体之一 = hit；
- 结构化完备率（硬线）：原文有分档截尾/门槛/离群句式 → 对应结构化字段必须产出。

红线：本脚本只做结构语料回放，输出数值仅记录命中/未命中，不进引擎、不调参。

用法：python scripts/replay_zj_corpus.py [--corpus DIR] [--out REPORT.md]
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

DEFAULT_CORPUS = Path(r"E:\ZJUBJ\招标文件集")
KNOWN_METHODS = {
    "second_lowest", "average_trimmed_then_below", "average_trimmed",
    "average_below", "average",
}
# 句式存在性探针（结构化完备率分母）。
# 分档截尾=含**区间中档**的三段式（"5＜n≤10"/"超过6且不足11"/"9≥n≥6"）；
# 单档"＞5家去1高1低/≤5家全平均"由旧键 trim_count/trim_threshold 全保真覆盖，不算 gap
PROBE_TIERS = re.compile(
    r"\d+\s*(?:个|家)?\s*[＜<][\s\S]{0,60}?[≤<=]\s*\d+\s*(?:个|家)?"
    r"|(?:超过|大于)\s*\d+\s*(?:个|家)[\s\S]{0,15}?(?:且不足|不足)\s*\d+\s*(?:个|家)"
    r"|\d+\s*(?:个|家)?\s*[≥>][\s\S]{0,20}?[≥>]\s*\d+\s*(?:个|家)?"
)
PROBE_GATE = re.compile(r"不(?:进入|纳入)(?:最佳报价|最佳报价值|评标基准价)|赋(?:值为)?满分(?:报价分)?的")
PROBE_OUTLIER = re.compile(r"剔除该报价后重新计算|剔除后重新计算")


def _price_window(pages: dict[int, str]) -> str:
    """价格机制窗口：命中关键词的页 + 相邻页（报价机制是纯文字，G19）。"""
    keys = ("基准价", "最佳报价值", "评标价", "报价得分", "算术平均")
    hit = {p for p, t in pages.items() if any(k in (t or "") for k in keys)}
    span: set[int] = set()
    for p in hit:
        span |= {p - 1, p, p + 1}
    span = {p for p in span if p in pages}
    return "\n".join(pages[p] for p in sorted(span))


def replay_one(path: Path) -> dict:
    import pymupdf
    from app.rules.compiler import parse_price_formula_text
    from app.rules.scoring_skeleton import (
        extract_score_skeleton, locate_scoring_section,
    )

    out: dict = {"file": path.name, "ok": False}
    doc = pymupdf.open(str(path))
    try:
        pages = [(i + 1, doc[i].get_text() or "") for i in range(len(doc))]
        pages_by_no = dict(pages)
        # 只对尾部/锚点页跑 find_tables（评分细则恒在尾部 8-15 页）
        tail = {p for p, _t in pages if p > len(pages) - 25}
        anchors = {p for p, t in pages if re.search(r"评标细则|评分细则|评标办法|评审细则", t or "")}
        cand = sorted(tail | anchors | {p + d for p in anchors for d in (-1, 0, 1)
                                        if 1 <= p + d <= len(pages)})
        tables_by_page: dict[int, list] = {}
        for pno in cand:
            try:
                tabs = doc[pno - 1].find_tables()
                tables_by_page[pno] = [t.extract() for t in tabs.tables]
            except Exception:  # noqa: BLE001
                tables_by_page[pno] = []
        span = locate_scoring_section(pages, tables_by_page)
        subset = {p: t for p, t in tables_by_page.items()
                  if span and span[0] <= p <= span[1]} if span else tables_by_page
        sk = extract_score_skeleton(str(path), [(p, pages_by_no[p]) for p in sorted(subset)]) \
            if subset else None
        out["ok"] = True
        out["skeleton"] = {
            "located": bool(span),
            "closed": bool(sk and sk.closed()),
            "rows": len(sk.rows) if sk else 0,
            "rows_alt": len(sk.rows_alt) if sk else 0,
            "ledger": len(sk.ledger) if sk else 0,
            "warnings": list(sk.warnings) if sk else [],
            "no_skeleton": sk is None,
        }
        win = _price_window(pages_by_no)
        parsed = parse_price_formula_text(win)
        method = parsed.get("benchmark_method")
        out["price"] = {
            "method": method,
            "hit": bool(method) and (method in KNOWN_METHODS or method == "second_lowest"),
            "k": str(parsed.get("k")) if parsed.get("k") is not None else None,
            "has_tiers_struct": "trim_tiers" in parsed,
            "has_gate_struct": "benchmark_gate" in parsed,
            "has_outlier_struct": "outlier_policy" in parsed,
        }
        out["probes"] = {
            "tiers_sentence": bool(PROBE_TIERS.search(win)),
            "gate_sentence": bool(PROBE_GATE.search(win)),
            "outlier_sentence": bool(PROBE_OUTLIER.search(win)),
        }
    finally:
        doc.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    ap.add_argument("--out", default=str(BACKEND.parent / "docs" / "replay_baseline.md"))
    args = ap.parse_args()
    corpus = Path(args.corpus)
    files = sorted(p for p in corpus.iterdir() if p.suffix.lower() == ".pdf")
    results = []
    for p in files:
        try:
            r = replay_one(p)
        except Exception as e:  # noqa: BLE001
            r = {"file": p.name, "ok": False, "error": str(e)}
        results.append(r)
        sk = r.get("skeleton") or {}
        pr = r.get("price") or {}
        print(f"[{'ok' if r.get('ok') else 'ERR'}] {r['file'][:40]:42s} "
              f"closed={sk.get('closed')!s:5s} rows={sk.get('rows')!s:3s} "
              f"method={pr.get('method')}")

    ok = [r for r in results if r.get("ok")]
    n = len(results)
    closed = sum(1 for r in ok if (r.get("skeleton") or {}).get("closed"))
    hit = sum(1 for r in ok if (r.get("price") or {}).get("hit"))
    tiers_completeness = _completeness(ok, "tiers_sentence", "has_tiers_struct")
    gate_completeness = _completeness(ok, "gate_sentence", "has_gate_struct")
    outlier_completeness = _completeness(ok, "outlier_sentence", "has_outlier_struct")

    lines = [
        "# 21 文件回放基线（首跑）", "",
        f"- 语料：{corpus}（PDF {n} 份；.doc 不在本脚本范围）",
        f"- 骨架闭合率：{closed}/{len(ok)}",
        f"- 价格六变体确定性识别率：{hit}/{len(ok)}",
        f"- 分档截尾句式结构化率：{tiers_completeness}",
        f"- 门槛句式结构化率：{gate_completeness}",
        f"- 离群句式结构化率：{outlier_completeness}", "",
        "| 文件 | closed | rows | rows_alt | warnings | method | k |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        sk = r.get("skeleton") or {}
        pr = r.get("price") or {}
        w = "; ".join(x.split("：")[0] for x in sk.get("warnings", [])) or "-"
        lines.append(
            f"| {r['file'][:38]} | {sk.get('closed')} | {sk.get('rows')} "
            f"| {sk.get('rows_alt')} | {w} | {pr.get('method')} | {pr.get('k')} |"
        )
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nreport → {args.out}")
    return 0


def _completeness(ok: list[dict], probe: str, struct: str) -> str:
    den = [r for r in ok if (r.get("probes") or {}).get(probe)]
    num = [r for r in den if (r.get("price") or {}).get(struct)]
    return f"{len(num)}/{len(den)}" + ("（硬线未达，见报告）" if len(num) != len(den) else "（硬线 ✓）")


if __name__ == "__main__":
    raise SystemExit(main())
