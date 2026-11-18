# -*- coding: utf-8 -*-
"""确定性事实供给清单（2026-11-18，ADR 0015）：检测器结论 → 批审 manifest。

批审（auto_review）证据闸门要求模型报的每个事实带原页证据；截图节等图片页证据
模型给不出引文 → 定向补抽 2 次耗尽 → 悬置。本模块把**确定性检测器**的结论按
`auto_review._inject_supplied_facts` 的协议产出 manifest，由注入层合成合法证据。

红线（不可破）：
- **正向供给**：仅检测器命中（有页锚/命中项）才产出条目；未检出不供给、不否定
  （机器看不见≠不符合，模型/人工旧通道照走）。
- **只升不降**：案例布尔检测 True 升模型 None/False；检测 False/None 不降模型 True。
- 锚 quote 只由检测器从送审页文本切片，注入层再逐字复核（复核不过丢锚）。
- 横向条款（跨投标人）与价格格不产 manifest（锚归属歧义/已有冻结价通道）。
"""
from __future__ import annotations

from typing import Any


def build_supplied_facts(criterion: dict, pages: list[dict]) -> dict[str, dict]:
    """按格消费契约挑字段，正向命中才产 manifest。

    criterion 是送审规则 dict（params.field/engine/person_scope 决定消费面）；
    pages 是该投标人**全部附件页**（{document_id, page_no, source, text, ...}）。
    """
    if not criterion or not pages:
        return {}
    # 横向条款/价格格/无显式计分契约 → 不产（与 _consumed_fact_fields 同哲学）
    if criterion.get("_frozen_bidders") or criterion.get("_extract_price"):
        return {}
    params = criterion.get("params") or {}
    field = str(params.get("field") or "")
    engine = str(params.get("engine") or "")
    if not (field or engine or params.get("person_scope")):
        return {}
    out: dict[str, dict] = {}
    if field == "certificates" or field == "cmmi":
        _supply_cert_facts(out, pages)
    if engine == "deviation_deduct_table":
        _supply_deviation_counts(out, pages, criterion)
    return out


# ---------------------------------------------------------------------------
# a/b. 认证截图节 + 证书三件套（certificates / certificate_evidence / cert_backing）
# ---------------------------------------------------------------------------


def _supply_cert_facts(out: dict[str, dict], pages: list[dict]) -> None:
    from app.engine.facts import extract_bid_facts
    from app.engine.objective import (
        classify_cert_backing,
        detect_cert_section,
        find_cert_copy_pages_anchored,
    )
    from app.parsing.normalize import normalize_no_ws

    det = detect_cert_section(pages)
    if det.state is True:
        out["cert_section_present"] = {
            "value": True, "detector": "cert_evidence_present",
            "mode": "evidence", "exclusive": True,
            "anchors": [
                {"document_id": h.get("document_id"), "page_no": h.get("page_no"),
                 "quote": h.get("quote"), "observation": h.get("basis")}
                for h in det.hits[:2]
            ],
        }
    elif det.state is False:
        # 文本层可读确认未见：引擎闸门（ISO 门）的标定输入——只写值不造否定引文
        out["cert_section_present"] = {
            "value": False, "detector": "cert_evidence_present",
            "mode": "provenance_only", "exclusive": True, "anchors": [],
        }
    # det.state is None（盲态）→ 不供给不否定（红线）

    full_text = "\n".join(str(p.get("text") or "") for p in pages)
    names = list(extract_bid_facts(full_text).certificates or [])
    if not names:
        return
    page_pairs = [(str(p.get("source") or ""), str(p.get("text") or "")) for p in pages]
    backing = classify_cert_backing(names, page_pairs, (det.state is True, det.basis))

    class _F:
        certificates = names

    anchors: list[dict] = []
    ev_value: dict[str, str] = {}
    for name in names:
        anchored = find_cert_copy_pages_anchored(pages, name)
        best = (next((a for k, a in anchored if k == "copy"), None)
                or next((a for k, a in anchored), None))
        if not best:
            continue  # 无锚证书不供给（模型照旧抽取，正向供给红线）
        key = normalize_no_ws(str(name))
        anchors.append({"key": key, "document_id": best.get("document_id"),
                        "page_no": best.get("page_no"), "quote": best.get("quote")})
        ev_value[name] = "auto"  # 注入层由锚物化引用替换
    if not anchors:
        return
    out["certificates"] = {
        "value": names, "detector": "material_inventory.certs",
        "identity": "normalize_cert", "exclusive": False, "mode": "evidence",
        "anchors": anchors,
    }
    out["certificate_evidence"] = {
        "value": ev_value, "detector": "find_cert_copy_pages",
        "identity": "normalize_cert", "exclusive": False, "mode": "evidence",
        "anchors": anchors,
    }
    out["cert_backing"] = {
        "value": {name: state for name, state in
                  ((n, backing.get(_backing_key(n, backing))) for n in names)
                  if state is not None},
        "detector": "classify_cert_backing",
        "identity": "normalize_cert", "exclusive": False, "mode": "evidence",
        "anchors": anchors,
    }


def _backing_key(name: str, backing: dict) -> str:
    """classify_cert_backing 的键是归一化证书键——回查时同归一。"""
    from app.engine.objective import normalize_cert
    from app.parsing.normalize import normalize_no_ws

    nk = normalize_no_ws(normalize_cert(str(name or "")))
    for k in backing:
        if normalize_no_ws(str(k)) == nk:
            return k
    return str(name)


# ---------------------------------------------------------------------------
# e. 偏离表计数（deviation_counts：total/missing/negative 全三键 + 区间页锚）
# ---------------------------------------------------------------------------


def _supply_deviation_counts(out: dict[str, dict], pages: list[dict],
                             criterion: dict) -> None:
    from app.engine.objective import _extract_deviation_counts_from_pages

    pairs = [(int(p.get("page_no") or 0), str(p.get("text") or "")) for p in pages]
    counts, snippet, region_pages = _extract_deviation_counts_from_pages(pairs)
    if counts is None or not region_pages:
        return  # 定位不到表 → 不供给（模型通道照走，R33 兜底不变）
    page_by_no = {int(p.get("page_no") or 0): p for p in pages}
    anchors = []
    for pno in region_pages[:3]:
        p = page_by_no.get(int(pno))
        if p is None:
            continue
        text = str(p.get("text") or "")
        pos = text.find(snippet[:20]) if snippet else -1
        quote = text[max(0, pos): pos + 80].strip() if pos >= 0 else text[:80].strip()
        anchors.append({"document_id": p.get("document_id"), "page_no": p.get("page_no"),
                        "quote": quote})
    if not anchors:
        return
    out["deviation_counts"] = {
        "value": counts, "detector": "_extract_deviation_counts",
        "exclusive": True, "mode": "evidence", "anchors": anchors,
    }
