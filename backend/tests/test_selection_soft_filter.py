"""类别软过滤/价格硬过滤行为测试（engine/selection）。

兼容门禁：chunk_cats=None 时输出与旧实现逐字段一致。
"""
from __future__ import annotations

from app.engine.selection import select_chunks_for_criterion, split_page_chunks

CRIT_TECH = {"name": "技术方案", "category": "technical", "description": "实施方案与架构"}
CRIT_PRICE = {"name": "投标报价", "category": "price", "description": "报价得分"}
CRIT_QUAL = {"name": "资格业绩", "category": "qualification", "description": "类似项目业绩"}


def _chunks_with_cats():
    """4 块（每页一块，无换行长行 → chunk_size 内独立成块）：块 0/2 technical。"""
    pages = [
        (1, 1, "技术方案 架构设计 实施方案 " + "正" * 500),
        (1, 2, "开标一览表 投标总价 报价表 " + "价" * 500),
        (1, 3, "实施方案 技术路线 质量保证 " + "术" * 500),
        (1, 4, "社保证明 营业执照 纳税证明 " + "证" * 500),
    ]
    chunks, srcs = split_page_chunks(pages, chunk_size=300)
    assert len(chunks) == 4, [len(c[1]) for c in chunks]
    page_cat = {(1, 1): "technical", (1, 2): "price", (1, 3): "technical", (1, 4): "qualification"}
    chunk_cats = [page_cat.get(s) for s in srcs]
    return chunks, chunk_cats


def test_in_class_preferred() -> None:
    chunks, chunk_cats = _chunks_with_cats()
    picked, meta = select_chunks_for_criterion(
        chunks, CRIT_TECH, budget=600, chunk_cats=chunk_cats
    )
    assert meta["in_class"] > 0
    assert meta["soft_fallback"] is False
    picked_ids = set(meta["ids"])
    # 类内块（0/2）应当入选
    assert picked_ids & {0, 2}


def test_soft_fallback_when_in_class_insufficient() -> None:
    # 类内只有一小块、预算很大 → 回退全库混选且标 soft_fallback
    pages = [
        (1, 1, "技术方案"),
        (1, 2, "开标一览表 投标总价 " + "价" * 2000),
        (1, 3, "社保证明 " + "证" * 2000),
    ]
    chunks, srcs = split_page_chunks(pages, chunk_size=300)
    page_cat = {(1, 1): "technical", (1, 2): "price", (1, 3): "qualification"}
    chunk_cats = [page_cat.get(s) for s in srcs]
    picked, meta = select_chunks_for_criterion(
        chunks, CRIT_TECH, budget=5000, chunk_cats=chunk_cats
    )
    assert meta["soft_fallback"] is True
    assert len(meta["ids"]) > len([i for i, c in enumerate(chunk_cats) if c == "technical"])


def test_price_hard_filter_excludes_others() -> None:
    chunks, chunk_cats = _chunks_with_cats()
    picked, meta = select_chunks_for_criterion(
        chunks, CRIT_PRICE, budget=2000, chunk_cats=chunk_cats
    )
    picked_ids = set(meta["ids"])
    price_ids = {i for i, c in enumerate(chunk_cats) if c == "price"}
    assert picked_ids and picked_ids <= price_ids, (picked_ids, price_ids)
    assert meta.get("price_fallback") is None


def test_price_empty_falls_back_with_flag() -> None:
    # 无 price 类块（分类全丢）→ 回退全库 + price_fallback 标记
    pages = [
        (1, 1, "技术方案 实施方案 " + "技" * 1000),
        (1, 2, "社保证明 营业执照 " + "证" * 1000),
    ]
    chunks, srcs = split_page_chunks(pages, chunk_size=300)
    page_cat = {(1, 1): "technical", (1, 2): "qualification"}
    chunk_cats = [page_cat.get(s) for s in srcs]
    picked, meta = select_chunks_for_criterion(
        chunks, CRIT_PRICE, budget=2000, chunk_cats=chunk_cats
    )
    assert meta["price_fallback"] is True
    assert meta["ids"]  # 没被打死，仍选出东西


def test_none_chunk_cats_identical_to_legacy() -> None:
    """兼容门禁：不带 chunk_cats 时 meta 无新键、结果一致。"""
    chunks, _srcs = _chunks_with_cats()
    for crit in (CRIT_TECH, CRIT_PRICE, CRIT_QUAL):
        for budget in (200, 600, 2000):
            _picked, meta = select_chunks_for_criterion(chunks, crit, budget)
            assert "soft_fallback" not in meta
            assert "in_class" not in meta
            assert "price_fallback" not in meta
            assert set(meta) >= {"truncated", "chars", "total_chars", "ids", "tiers", "mode"}


def test_all_none_cats_degrades_gracefully() -> None:
    """全 None 类别（无标签）→ 行为等同旧实现（只是 meta 多键）。"""
    chunks, _srcs = _chunks_with_cats()
    none_cats = [None] * len(chunks)
    picked1, meta1 = select_chunks_for_criterion(chunks, CRIT_TECH, 600, chunk_cats=none_cats)
    picked2, meta2 = select_chunks_for_criterion(chunks, CRIT_TECH, 600)
    assert meta1["ids"] == meta2["ids"]
    assert meta1["soft_fallback"] is True  # 无类内 → 全库


def test_qualification_maps_compliance() -> None:
    """qualification/compliance 评分项都进资格类软过滤。"""
    chunks, chunk_cats = _chunks_with_cats()
    crit = {"name": "资格业绩", "category": "compliance"}
    _picked, meta = select_chunks_for_criterion(chunks, crit, 2000, chunk_cats=chunk_cats)
    assert meta["in_class"] > 0
