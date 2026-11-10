# -*- coding: utf-8 -*-
"""评分类别的确定性校正。

根因（2026-10-21 真实项目 PRJ-20261021-A1G9）：`category` 完全由 LLM 自报，
招标文件「1、商务部分（10 分）」下的业绩/认证两条被标成 technical——排名/报告/
前端全按 category 分桶，商务桶因此恒 0；`merger` 按 category 硬过滤又导致模板的
commercial 项在合并时被整条弃用。

类别其实不需要猜：评分项的 `source_node_id` 指向的节点标题就是「1、商务部分
（10 分）」「2、技术部分（40 分）」「3、报价部分（50 分）」这类章节标题，逐级
前缀回退查标题关键词即可确定。这里只做"有把握才改"，全部改动留痕。
"""
from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy import select

from app.db.models import Criterion, DocNode, DocumentPage, ReviewRecord
from app.db.session import session_scope
from app.parsing.normalize import normalize_no_ws

log = logging.getLogger("bid_eval.rules.category_map")

# 顺序即优先级：同一标题命中多个关键词时取先声明者。
# 关键词必须足够"章节标题化"，避免正文里的"商务报价"之类叙述误触发。
CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("price", ("报价部分", "价格部分", "报价评分", "报价分", "价格分", "投标报价得分", "评审价格")),
    ("commercial", ("商务部分", "商务分", "商务标", "商务项")),
    ("technical", ("技术部分", "技术分", "技术标", "技术项", "服务部分")),
    ("qualification", ("资格审查", "资格要求", "资格条件", "资格文件", "资格性")),
    ("compliance", ("符合性审查", "符合性", "响应性审查", "形式审查")),
]

# 标题行长度上限（"3、报价部分（50 分）"=11 字；24 足以容纳带序号的章节标题）
_MAX_TITLE_LEN = 24
# 含句读的行是 prose，不是标题
_PROSE_MARKS = ("。", "，", "；")
# 通道 B：从引文位置向前最多回扫的行数（防跨章节误配）
_MAX_BACKSCAN_LINES = 60


def category_from_titles(titles: Iterable[str | None]) -> str | None:
    """标题链 → 类别。titles[0] 最深（自身标题），祖先排后。"""
    for title in titles:
        if not title:
            continue
        t = title.strip()
        if not t or len(t) > _MAX_TITLE_LEN:
            continue
        if any(mark in t for mark in _PROSE_MARKS):
            continue  # 「本项目的商务部分包括…，具体见下表」这类正文句子不是标题
        for cat, kws in CATEGORY_KEYWORDS:
            if any(k in t for k in kws):
                return cat
    return None


def load_node_titles(document_id: int) -> dict[str, str]:
    """document 全部节点的 {node_id: title}（一次加载，供链式回退）。"""
    with session_scope() as s:
        rows = s.execute(
            select(DocNode.node_id, DocNode.title).where(DocNode.document_id == document_id)
        ).all()
    return {nid: (title or "") for nid, title in rows}


def category_from_node_chain(by_id: dict[str, str], node_id: str | None) -> str | None:
    """通道 A：node_id 逐级前缀回退查标题（`…:I5:N1` → `…:I5` → `…:SEC6` …）。"""
    if not node_id or node_id.startswith("法条://"):
        return None
    chain: list[str] = []
    cur = node_id
    while cur:
        title = by_id.get(cur)
        if title:
            chain.append(title)
        if ":" not in cur:
            break
        cur = cur.rsplit(":", 1)[0]
    return category_from_titles(chain)


def category_from_quote_position(
    pages: list[tuple[int, str]], quote: str, page_no: int | None
) -> str | None:
    """通道 B：在引文所在页向前回扫最近的类别标题行（node 定位失败时的兜底）。"""
    q = normalize_no_ws(quote or "")[:40]
    if not q:
        return None
    wanted = {page_no} if page_no else set()
    for pno, text in pages:
        if wanted and pno not in wanted:
            continue
        norm = normalize_no_ws(text or "")
        pos = norm.find(q)
        if pos < 0:
            continue
        lines = (text or "").split("\n")
        acc = 0
        line_idx = max(0, len(lines) - 1)
        for idx, line in enumerate(lines):
            acc += len(normalize_no_ws(line))
            if acc >= pos:
                line_idx = idx
                break
        for j in range(line_idx, max(-1, line_idx - _MAX_BACKSCAN_LINES), -1):
            line = lines[j].strip()
            if not line or len(line) > _MAX_TITLE_LEN:
                continue
            cat = category_from_titles([line])
            if cat:
                return cat
        return None
    return None


def load_page_texts(document_id: int, around: set[int] | None = None) -> list[tuple[int, str]]:
    """document 的页文本（around 给定时只取邻域页，控制内存）。"""
    with session_scope() as s:
        q = select(DocumentPage.page_no, DocumentPage.text).where(
            DocumentPage.document_id == document_id
        )
        if around:
            pages: set[int] = set()
            for p in around:
                pages.update({p, p - 1, p + 1})
            q = q.where(DocumentPage.page_no.in_({p for p in pages if p > 0}))
        rows = s.execute(q).all()
    return [(int(p), text or "") for p, text in rows]


def plan_category_fixes(
    criteria: list[Criterion],
    document_id: int,
    node_titles: dict[str, str] | None = None,
    page_texts: list[tuple[int, str]] | None = None,
) -> list[tuple[int, str, str, str]]:
    """计算应改的类别 → [(criterion_id, 旧, 新, 依据)]。只读，不动库。"""
    out: list[tuple[int, str, str, str]] = []
    for c in criteria:
        if c.origin not in ("document", "template"):
            continue  # 法规/人工条目不自动改
        if not c.source_node_id and not (c.source_quote or "").strip():
            continue
        cat = None
        basis = ""
        if c.source_node_id:
            cat = category_from_node_chain(node_titles or {}, c.source_node_id)
            if cat:
                basis = f"节点标题（{c.source_node_id}）"
        if cat is None and page_texts is not None:
            cat = category_from_quote_position(page_texts, c.source_quote or "", c.source_page)
            if cat:
                basis = "引文所在页的章节标题"
        if not cat or cat == c.category:
            continue
        out.append((c.id, c.category, cat, basis))
    return out


_AUTO_REASON_PREFIX = "自动校正"


def manual_edited_criterion_ids(package_id: int) -> set[int]:
    """被人工改动过的评分项——自动校正不得触碰。

    自动校正自身的留痕（reason 以"自动校正"开头）不算人工改动：否则在
    confirm 时用真实用户名调用 normalize，下一次就会把自己的修正当成人工决定。
    """
    with session_scope() as s:
        rows = s.execute(
            select(ReviewRecord.subject_id).where(
                ReviewRecord.subject_type == "criterion",
                ReviewRecord.actor != "auto",
                ReviewRecord.action.in_(("edit", "override", "accept", "confirm")),
                ~ReviewRecord.reason.startswith(_AUTO_REASON_PREFIX),
            )
        ).all()
    return {int(r[0]) for r in rows if str(r[0]).isdigit()}


def apply_category_from_nodes(package_id: int, actor: str = "auto") -> int:
    """对包内评分项按章节标题校正 category（幂等、全留痕）。返回改动条数。"""
    with session_scope() as s:
        from app.db.models import RulePackage

        pkg = s.get(RulePackage, package_id)
        if pkg is None or pkg.source_document_id is None:
            return 0
        doc_id = int(pkg.source_document_id)
        crits = s.execute(
            select(Criterion).where(
                Criterion.package_id == package_id,
                Criterion.status != "retired",
                Criterion.origin.in_(("document", "template")),
            )
        ).scalars().all()
        crit_list = list(crits)
    if not crit_list:
        return 0

    node_titles = load_node_titles(doc_id)
    pages = load_page_texts(doc_id, around={c.source_page for c in crit_list if c.source_page})
    manual = manual_edited_criterion_ids(package_id)
    fixes = [
        f for f in plan_category_fixes(crit_list, doc_id, node_titles, pages)
        if f[0] not in manual
    ]
    if not fixes:
        return 0

    by_id = {c.id: c for c in crit_list}
    with session_scope() as s:
        for crit_id, old, new, basis in fixes:
            crit = s.get(Criterion, crit_id)
            if crit is None or crit.category == new:
                continue
            crit.category = new
            edit_json = dict(crit.edit_json or {})
            edit_json["category_from_node"] = {"old": old, "new": new, "basis": basis}
            crit.edit_json = edit_json
            s.add(crit)
            s.add(ReviewRecord(
                subject_type="criterion",
                subject_id=str(crit_id),
                action="edit",
                actor=actor,
                reason=f"自动校正：按招标文件章节标题修正评分类别（{basis}）",
                old_value_json={"category": old},
                new_value_json={"category": new},
            ))
            log.info("category 校正：criterion %s %s→%s（%s）", crit_id, old, new, basis)
    return len(fixes)


def in_memory_category_fixes(entries: list[dict], document_id: int) -> dict[int, str]:
    """只读内存校正：{criterion_id: 新 category}（合并匹配前的防御，不写库）。

    DB 的权威修复在 compile/normalize（apply_category_from_nodes）；这里兜住
    "没来得及自愈的包"——类别硬过滤会让模板条目直接落空被弃用。
    """
    if not entries or document_id <= 0:
        return {}
    node_titles = load_node_titles(document_id)
    pages = load_page_texts(
        document_id, around={e.get("source_page") for e in entries if e.get("source_page")}
    )
    out: dict[int, str] = {}
    for e in entries:
        cat = None
        if e.get("source_node_id"):
            cat = category_from_node_chain(node_titles, e["source_node_id"])
        if cat is None and e.get("source_quote"):
            cat = category_from_quote_position(pages, e["source_quote"], e.get("source_page"))
        if cat and cat != e.get("category"):
            out[e["id"]] = cat
    return out


def apply_category_to_extracted(criteria: list, document_id: int) -> int:
    """编译期（落库前）校正 ExtractedCriterion.category。

    必须在 `enrich_extracted`/`_persist_criterion` 之前跑：`category=="price"`
    是价格公式落盘的门槛，类别错了整条价格链路失效。此时还没有 source_node_id，
    走通道 B（引文位置回扫章节标题）。
    """
    if not criteria:
        return 0
    pages = load_page_texts(document_id)
    changed = 0
    for c in criteria:
        cat = category_from_quote_position(pages, c.quote or "", getattr(c, "page_hint", None))
        if cat and cat != c.category:
            log.info("category 编译期校正：%s %s→%s", c.name, c.category, cat)
            c.category = cat
            changed += 1
    return changed
