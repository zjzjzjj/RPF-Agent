# -*- coding: utf-8 -*-
"""评分侧消费：material_tags → 类别化材料索引文本（注入 prompt <material_pages>）。

输出形如：
【资格及证明文件】营业执照 p3、社保证明 p120-125
【报价】开标一览表 p2
索引缺失/覆盖不全时调用方回退 scoring_service 的关键词扫描（行为不变）。
另提供 page_material_tags（阶段4，2026-10-30）：业绩三态的页级标签先验。
"""
from __future__ import annotations

from sqlalchemy import select

from app.db.models import MaterialTag
from app.db.session import session_scope
from app.material_index.catalog import (
    CATEGORY_LABELS,
    EVIDENCE_TAG_WORDS,
    MATERIAL_CATEGORIES,
    normalize_tag,
)
from app.material_index.service import text_sha


def material_index_hint(document_ids: list[int], text_by_page: dict[tuple[int, int], str]) -> str:
    """按类别分组的页码索引文本；无有效标签返回空串（调用方回退）。

    text_by_page: (document_id, page_no) → 页文本（用于 stale 检测）。
    """
    if not document_ids:
        return ""
    with session_scope() as s:
        rows = s.execute(
            select(MaterialTag).where(
                MaterialTag.document_id.in_(document_ids),
                MaterialTag.seg_kind == "page",
            ).order_by(MaterialTag.page_no)
        ).scalars().all()
        for r in rows:
            s.expunge(r)
    if not rows:
        return ""

    # stale 页（文本已变）不进提示——宁缺勿错
    valid: list[MaterialTag] = []
    for r in rows:
        cur = text_by_page.get((int(r.document_id), int(r.page_no)))
        if cur is None:
            valid.append(r)  # 无从校验时保留
        elif not r.text_sha or r.text_sha == text_sha(cur):
            valid.append(r)
    if not valid:
        return ""

    by_cat: dict[str, dict[str, list[int]]] = {c: {} for c in MATERIAL_CATEGORIES}
    for r in valid:
        if r.category not in by_cat:
            continue
        tags = (r.tags_json or ["材料"])
        tag = str(tags[0]) if tags else "材料"
        by_cat[r.category].setdefault(tag, []).append(int(r.page_no))

    lines: list[str] = []
    for cat in MATERIAL_CATEGORIES:
        if cat == "other" or not by_cat[cat]:
            continue
        parts: list[str] = []
        for tag, pages in by_cat[cat].items():
            pages = sorted(set(pages))
            span = f"第{pages[0]}页" if len(pages) == 1 else f"第{pages[0]}-{pages[-1]}页"
            parts.append(f"{tag} {span}")
        lines.append(f"【{CATEGORY_LABELS[cat]}】" + "、".join(parts[:12]))
    return "；".join(lines)


def page_material_tags(
    document_id: int,
    page_nos: set[int] | list[int],
    text_by_page: dict[int, str],
) -> dict[int, dict[str, tuple[str, float]]]:
    """页级强标签 → 三态证据词命中（阶段4，2026-10-30）：{page_no: {mat: (tag, conf)}}。

    只取 rule 层且 confidence >= CONF_STRONG 的页级标签（与入库时"页内标题行
    强命中"同口径——标题盖章是确定性证据，弱命中/LLM 判层不采信）。tags_json
    逐词 normalize_tag（合同复印件→合同、验收报告→验收、用户证明→业主证明）
    后与 EVIDENCE_TAG_WORDS 子串匹配。text_sha 失效剔除（text_by_page 有该页
    文本且 sha 不匹配 → 页已重解析、标签作废；无文本则无从校验、保留）。
    失败/无行返回 {}（调用方三态退回无先验口径）。
    """
    if document_id is None or not page_nos:
        return {}
    try:
        from app.material_index.rules import CONF_STRONG

        with session_scope() as s:
            rows = s.execute(
                select(MaterialTag).where(
                    MaterialTag.document_id == int(document_id),
                    MaterialTag.seg_kind == "page",
                    MaterialTag.page_no.in_(sorted(set(int(n) for n in page_nos))),
                    MaterialTag.confidence >= CONF_STRONG,
                    MaterialTag.method == "rule",
                )
            ).scalars().all()
            for r in rows:
                s.expunge(r)
    except Exception:  # noqa: BLE001 —— 标签先验失败不阻塞三态判定
        return {}
    out: dict[int, dict[str, tuple[str, float]]] = {}
    for r in rows:
        no = int(r.page_no)
        cur = text_by_page.get(no)
        if cur is not None and r.text_sha and r.text_sha != text_sha(cur):
            continue  # 页文本已变 → 标签失效，宁缺勿错
        for raw in r.tags_json or []:
            tag = normalize_tag(str(raw))
            conf = float(r.confidence or 0.0)
            for mat, words in EVIDENCE_TAG_WORDS.items():
                if not any(w in tag for w in words):
                    continue
                slot = out.setdefault(no, {})
                if mat not in slot or conf > slot[mat][1]:
                    slot[mat] = (tag, conf)
    return out
