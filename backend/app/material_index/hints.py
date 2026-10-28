# -*- coding: utf-8 -*-
"""评分侧消费：material_tags → 类别化材料索引文本（注入 prompt <material_pages>）。

输出形如：
【资格及证明文件】营业执照 p3、社保证明 p120-125
【报价】开标一览表 p2
索引缺失/覆盖不全时调用方回退 scoring_service 的关键词扫描（行为不变）。
"""
from __future__ import annotations

from sqlalchemy import select

from app.db.models import MaterialTag
from app.db.session import session_scope
from app.material_index.catalog import CATEGORY_LABELS, MATERIAL_CATEGORIES
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
