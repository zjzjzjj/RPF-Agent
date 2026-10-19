"""规则包逐条覆盖（S1）：模板条目 ↔ 招标文件编译条目的 1:1 配对。

设计稿 §5：匹配策略"确定性优先，LLM 兜底，人工终审"。
- 第一层（纯代码）：硬过滤 category/score_type 相等 → 名称精确/相似度匹配。
- 第二层（LLM 辅助）：对未匹配集合做封闭菜单配对（MATCH_SYSTEM_PROMPT），
  confidence < 0.5 丢弃。
- 第三层（人工）：匹配提案只读，任何覆盖在人工确认前不生效（resolve 时执行）。

本模块只产出"配对"，不写任何业务行。
"""
from __future__ import annotations

import logging
from difflib import SequenceMatcher
from typing import Any

from app.parsing.normalize import normalize

log = logging.getLogger("bid_eval.merger")

# 确定性匹配阈值（设计稿 §5.2）：
#   名称精确 → exact；bigram Jaccard ≥ 0.55 或 SequenceMatcher.ratio ≥ 0.75 → deterministic
DETERMINISTIC_JACCARD = 0.55
DETERMINISTIC_RATIO = 0.75
# LLM 辅助匹配的置信度下限（设计稿：confidence < 0.5 直接丢弃）
LLM_MIN_CONFIDENCE = 0.5


def _norm_name(name: str | None) -> str:
    return (normalize(name or "") or "").strip().lower()


def _bigrams(s: str) -> set[str]:
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def bigram_jaccard(a: str, b: str) -> float:
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    union = ga | gb
    return len(ga & gb) / len(union) if union else 0.0


def name_similarity(a: str, b: str) -> float:
    """名称相似度 = max(bigram Jaccard, SequenceMatcher.ratio)。"""
    return max(bigram_jaccard(a, b), SequenceMatcher(None, a, b).ratio())


def _brief(c: dict[str, Any], *, with_quote: bool = True) -> dict[str, Any]:
    """把快照形状的 criterion 转成提案输出的可序列化形态（Decimal → str）。"""
    from decimal import Decimal

    def _s(v: Any) -> str | None:
        return str(v) if v is not None else None

    quote = c.get("source_quote") or ""
    return {
        "id": c.get("id"),
        "code": c.get("code"),
        "name": c.get("name"),
        "category": c.get("category"),
        "score_type": c.get("score_type"),
        "is_veto": bool(c.get("is_veto")),
        "max_score": _s(c.get("max_score")),
        "source_page": c.get("source_page"),
        "source_quote": (quote[:120] + "…") if with_quote and len(quote) > 120 else (quote or None),
        "origin": c.get("origin", "document"),
        "status": c.get("status"),
    }


def deterministic_match(
    template: list[dict[str, Any]], document: list[dict[str, Any]]
) -> dict[str, Any]:
    """第一层：确定性匹配（纯代码）。

    硬过滤：category 与 score_type 必须相等；price 类天然 1:1。
    返回 {"pairs": [{template, document, match_method, similarity}],
          "unmatched_template": [...], "unmatched_document": [...]}。
    """
    used_doc: set[int] = set()
    used_tpl: set[int] = set()
    pairs: list[dict[str, Any]] = []

    def _pick(t: dict, d_list: list[dict]) -> dict | None:  # noqa: ANN001
        best: dict | None = None
        best_sim = -1.0
        tn = _norm_name(t["name"])
        for d in d_list:
            if d["id"] in used_doc:
                continue
            if d["category"] != t["category"] or d["score_type"] != t["score_type"]:
                continue
            dn = _norm_name(d["name"])
            if tn == dn:
                best, best_sim = d, 1.0
                break
            sim = name_similarity(tn, dn)
            if sim >= DETERMINISTIC_RATIO or bigram_jaccard(tn, dn) >= DETERMINISTIC_JACCARD:
                if sim > best_sim:
                    best, best_sim = d, sim
        return best

    # 非价格项：贪心取相似度最高的一对
    for t in template:
        if t["category"] == "price":
            continue
        d = _pick(t, document)
        if d is not None:
            tn, dn = _norm_name(t["name"]), _norm_name(d["name"])
            method = "exact" if tn == dn else "deterministic"
            pairs.append({
                "template": t, "document": d,
                "match_method": method,
                "similarity": round(1.0 if method == "exact" else name_similarity(tn, dn), 4),
            })
            used_doc.add(d["id"])
            used_tpl.add(t["id"])

    # 价格项 1:1（评分引擎假设每包单条 price formula）
    t_price = [t for t in template if t["category"] == "price"]
    d_price = [d for d in document if d["category"] == "price"]
    if t_price and d_price:
        t, d = t_price[0], d_price[0]
        used_doc.add(d["id"])
        used_tpl.add(t["id"])
        tn, dn = _norm_name(t["name"]), _norm_name(d["name"])
        method = "exact" if tn == dn else "deterministic"
        pairs.append({
            "template": t, "document": d,
            "match_method": method,
            "similarity": round(1.0 if method == "exact" else name_similarity(tn, dn), 4),
        })

    unmatched_template = [t for t in template if t["id"] not in used_tpl]
    unmatched_document = [d for d in document if d["id"] not in used_doc]
    return {
        "pairs": pairs,
        "unmatched_template": unmatched_template,
        "unmatched_document": unmatched_document,
    }


def llm_match(
    template: list[dict[str, Any]],
    document: list[dict[str, Any]],
    provider: str | None = None,
    model: str | None = None,
    data_class: str = "synthetic",
) -> list[dict[str, Any]]:
    """第二层：LLM 辅助匹配（封闭菜单）。返回与 deterministic_match 同构的 pairs。"""
    from app.llm.gateway import Gateway, get_gateway
    from app.llm.registry import get_provider
    from app.rules.model import MatchProposal
    from app.rules.prompts import MATCH_SYSTEM_PROMPT, MATCH_USER_TEMPLATE

    if not template or not document:
        return []

    def _line(c: dict[str, Any]) -> str:
        return f"{c['code']} | {c['name']} | {c['category']} | {c['score_type']} | {c.get('max_score') or ''}"

    user = MATCH_USER_TEMPLATE.format(
        template_text="\n".join(_line(c) for c in template),
        document_text="\n".join(_line(c) for c in document),
    )
    result: MatchProposal | None = None
    try:
        # 网关构建也必须在 try 内：未配置 provider 时按"LLM 辅助失败"降级为仅确定性匹配
        gateway = get_gateway() if provider is None and model is None else Gateway(get_provider(provider, model))
        result, _ = gateway.chat_json(
            MATCH_SYSTEM_PROMPT, user, MatchProposal, data_class=data_class
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM 辅助匹配失败，降级为仅确定性匹配：%s", exc)
        return []

    doc_by_code = {c["code"]: c for c in document}
    pairs: list[dict[str, Any]] = []
    seen_doc: set[int] = set()
    for p in result.pairs:
        d = doc_by_code.get(p.document_code)
        if d is None or d["id"] in seen_doc:
            continue
        if p.confidence < LLM_MIN_CONFIDENCE:
            continue
        seen_doc.add(d["id"])
        pairs.append({
            "template": next((t for t in template if t["code"] == p.template_code), None),
            "document": d,
            "match_method": "llm",
            "similarity": round(p.confidence, 4),
            "confidence": round(p.confidence, 4),
            "rationale": p.rationale,
        })
    return [p for p in pairs if p["template"] is not None]


def generate_match_proposal(
    merge_package_id: int,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """生成匹配提案（只读，不写业务行）。

    流程：确定性匹配 → 对未匹配集合跑 LLM 辅助（已配置真实模型时）→ 合并去重。
    返回 {merge_package_id, target_package_id, pairs, unmatched_template, unmatched_document}。
    """
    from app.db.models import RulePackage
    from app.db.session import session_scope
    from app.services.rule_service import load_criteria_for_snapshot

    with session_scope() as s:
        merge_pkg = s.get(RulePackage, merge_package_id)
        if merge_pkg is None or merge_pkg.merge_target_id is None:
            raise ValueError("该规则包不是合并模式的临时编译包（缺少 merge_target_id）")
        target_id = merge_pkg.merge_target_id
        template = load_criteria_for_snapshot(s, target_id)
        document = load_criteria_for_snapshot(s, merge_package_id)
        source_doc_id = merge_pkg.source_document_id

    result = deterministic_match(template, document)
    pairs: list[dict] = result["pairs"]
    used_doc_ids = {p["document"]["id"] for p in pairs}
    used_tpl_ids = {p["template"]["id"] for p in pairs}

    # LLM 兜底：只对未匹配集合配对（节省 token，且确定性结果优先）
    rest_tpl = [t for t in result["unmatched_template"]]
    rest_doc = [d for d in result["unmatched_document"]]
    data_class = _document_data_class(source_doc_id)
    llm_pairs = llm_match(
        rest_tpl, rest_doc, provider=provider, model=model, data_class=data_class
    )
    for p in llm_pairs:
        if p["document"]["id"] not in used_doc_ids and p["template"]["id"] not in used_tpl_ids:
            pairs.append(p)
            used_doc_ids.add(p["document"]["id"])
            used_tpl_ids.add(p["template"]["id"])

    # 提案输出：未匹配集合按最终配对结果重算
    matched_tpl = {p["template"]["id"] for p in pairs}
    matched_doc = {p["document"]["id"] for p in pairs}

    def _pair_out(p: dict) -> dict:
        return {
            "pair_id": f"{p['template']['id']}-{p['document']['id']}",
            "match_method": p["match_method"],
            "similarity": p["similarity"],
            "confidence": p.get("confidence"),
            "rationale": p.get("rationale"),
            "template": _brief(p["template"], with_quote=False),
            "document": _brief(p["document"]),
        }

    return {
        "merge_package_id": merge_package_id,
        "target_package_id": target_id,
        "pairs": [_pair_out(p) for p in pairs],
        "unmatched_template": [_brief(t, with_quote=False) for t in template if t["id"] not in matched_tpl],
        "unmatched_document": [_brief(d) for d in document if d["id"] not in matched_doc],
    }


def _document_data_class(document_id: int | None) -> str:
    if document_id is None:
        return "synthetic"
    from app.db.models import Document
    from app.db.session import session_scope

    with session_scope() as s:
        doc = s.get(Document, document_id)
        return doc.data_class if doc else "synthetic"