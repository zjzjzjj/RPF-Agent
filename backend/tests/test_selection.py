"""公共选段模块（select_chunks_by_tier / split_text_chunks / derive_keywords）。"""
from __future__ import annotations

from app.engine.selection import (
    derive_keywords,
    select_chunks_by_tier,
    split_text_chunks,
)


def _mk_chunks(n: int, size: int = 100, filler: str = "正") -> list[tuple[int, str]]:
    return [(i, filler * size) for i in range(n)]


def test_under_budget_returns_all() -> None:
    chunks = _mk_chunks(5)
    picked, meta = select_chunks_by_tier(chunks, (("k", ("正",)),), 10_000)
    assert picked == chunks and meta["truncated"] is False and meta["chars"] == meta["total_chars"]


def test_keyword_chunks_and_neighbors_picked() -> None:
    # 10 块，只有第 8 块（尾部）含"售后服务"；预算 700 容纳命中块 + 相邻块
    chunks = [(i, "正" * 200) for i in range(10)]
    chunks[8] = (8, "售后服务方案很完善" + "正" * 190)
    picked, meta = select_chunks_by_tier(
        chunks, (("criterion", ("售后服务",)),), 700)
    ids = [i for i, _t in picked]
    assert 8 in ids, ids          # 关键词块入选（在尾部，不再被头部截断丢掉）
    assert 7 in ids or 9 in ids, ids  # 相邻块沾光入选
    assert meta["truncated"] is True


def test_budget_hard_limit() -> None:
    chunks = [(i, "正" * 300) for i in range(10)]
    picked, meta = select_chunks_by_tier(chunks, (("k", ("正",)),), 900)
    assert meta["chars"] <= 900
    assert all(len(t) <= 300 for _i, t in picked)


def test_order_preserved() -> None:
    # 命中块乱序出现（idx 2 与 idx 7），预算装得下 → 输出必须仍按原序
    chunks = [(i, "正" * 100) for i in range(10)]
    chunks[7] = (7, "售后保障" + "正" * 90)
    chunks[2] = (2, "售后承诺" + "正" * 90)
    picked, _meta = select_chunks_by_tier(chunks, (("k", ("售后",)),), 100_000)
    assert [i for i, _t in picked] == list(range(10))  # 全量且序不变


def test_diffuse_fallback_when_no_keywords() -> None:
    # 无任何关键词命中 + fallback_diffuse → 头尾散布而非只装头部
    chunks = _mk_chunks(20, size=200)
    picked, meta = select_chunks_by_tier(chunks, (("k", ("没有这个词",)),), 2000,
                                         fallback_diffuse=True)
    ids = [i for i, _t in picked]
    assert 0 in ids and 19 in ids, ids  # 头尾都有
    assert meta["chars"] <= 2000


def test_split_text_chunks() -> None:
    text = "\n".join(f"第{i}行 内容" * 10 for i in range(300))
    chunks = split_text_chunks(text, chunk_size=2000)
    assert sum(len(t) for _i, t in chunks) >= len(text) - 300  # 行边界切，不丢正文（换行归一损失）
    assert all(len(t) <= 4000 for _i, t in chunks)  # 无换行超长兜底硬切


def test_derive_keywords() -> None:
    kws = derive_keywords({
        "name": "售后服务方案",
        "required_evidence": "售后服务承诺函, 响应时间表、培训计划",
    })
    assert "售后服务方案" in kws
    assert "售后服务" in kws          # 去后缀主干
    assert "售后服务承诺函" in kws    # required_evidence 片段
    assert len(kws) == len(set(kws))  # 去重


def test_derive_keywords_empty_falls_back() -> None:
    assert derive_keywords({"name": "评分项"}) != ()
    assert derive_keywords({"name": ""}) == ()
