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


def test_index_text_weight_toc_vs_content():
    """目录/索引页降权（结构性判据，2026-10-27 run 58 目录中毒实证）。"""
    from app.engine.selection import index_text_weight

    toc = "\n".join(
        ["服务团队技术力量（20分）见投标文件（63~131）页",
         "投标方案评析（10分）………132",
         "评析（10分）详见第173页",
         "应急预案………136"])
    assert index_text_weight(toc) <= 0.4  # 强索引 → 封顶档
    content = (
        "驻场服务团队由项目经理1名、运维工程师6名组成，实行AB角轮岗。"
        "月度巡检计划表见附件，故障响应承诺30分钟内到达现场并出具处理单。")
    assert index_text_weight(content) == 1.0
    assert index_text_weight("") == 1.0


def test_tier_selection_prefers_content_over_index(monkeypatch):
    """同一关键词：索引 chunk 分数不得压过正文 chunk。"""
    from app.engine.selection import split_text_chunks, select_chunks_by_tier

    # 3 个 chunk：正文命中1词、索引命中多词（逐字含评分项名+见投标文件页引用）
    text = (
        "驻场服务团队技术力量由项目经理和运维工程师组成，分工明确。\n"
        + "x" * 600
        + "服务团队技术力量（20分）见投标文件（63~131）页\n服务团队健全合理性（5分）………64\n"
        + "服务团队技术力量职责分工（10分）详见第70页\n服务团队（20）………71\n"
        + "y" * 600)
    chunks = split_text_chunks(text, chunk_size=400)
    picked, meta = select_chunks_by_tier(
        chunks, (("scoring", ("服务团队", "技术力量", "健全")),), budget=1200)
    picked_text = "\n".join(t for _i, t in picked)
    assert "驻场服务团队技术力量由项目经理" in picked_text  # 正文入选


def test_vector_selection_downweights_toc_chunks(monkeypatch):
    """vector 路径目录降权：确定性向量下目录块余弦 1.0 高于正文 0.8，
    降权后正文块入选、目录块不入选（2026-10-29 run 60 补：服务器
    BID_EVAL_SELECTION=vector，此前只有关键词/证据地图路径有此降权）。"""
    from app.evidence import embed_client
    from app.engine.selection import select_chunks_by_vector

    def pad(s: str) -> str:
        return s + "x" * (100 - len(s))

    # 5 块各 100 字符；目录块(4)与正文块(2)远离，防目录块当选后把正文
    # 当邻居捎带入选。预算 220 只装得下 主命中块 + 1 邻居（100+100）。
    toc = pad("\n".join([
        "驻场服务团队技术力量（20分）见投标文件（63~131）页",
        "服务团队健全合理性（5分）………64",
        "团队职责分工（10分）详见第70页",
        "服务承诺（5分）………71",
    ]))
    chunks = [
        (0, pad("无关填充块零")),
        (1, pad("无关填充块一")),
        (2, pad("驻场服务团队由项目经理1名、运维工程师6名组成，AB角轮岗。")),
        (3, pad("无关填充块三")),
        (4, toc),
    ]
    assert all(len(t) == 100 for _i, t in chunks)

    def fake_embed_batch(texts):
        out = []
        for t in texts:
            if "见投标文件" in t:
                out.append([1.0, 0.0])    # 目录块：与查询完全对齐，余弦 1.0
            elif "驻场服务团队由项目经理" in t:
                out.append([0.8, 0.6])    # 正文块：余弦 0.8，与目录接近（已 L2 归一）
            else:
                out.append([0.0, 1.0])    # 无关块：余弦 0
        return out

    monkeypatch.setattr(embed_client, "embed_one", lambda t: [1.0, 0.0])
    monkeypatch.setattr(embed_client, "embed_batch", fake_embed_batch)

    out = select_chunks_by_vector(chunks, "驻场服务团队技术力量", budget=220)
    assert out is not None
    picked, meta = out
    picked_text = "\n".join(t for _i, t in picked)
    assert "驻场服务团队由项目经理" in picked_text   # 正文入选
    assert "见投标文件" not in picked_text           # 目录块被降权后排不进预算
    assert meta["ids"] == [1, 2], meta["ids"]  # 正文 + 左邻居


def test_ocr_config_defaults(monkeypatch) -> None:
    """OCR 护栏默认值：60 页会截断 334 页大标书（run 60 实证跳过 92 页
    扫描件），默认 350；材料救援 20→60。服务器可经 .env 单键回退。"""
    from app import config

    monkeypatch.delenv("BID_EVAL_OCR_MAX_PAGES", raising=False)
    monkeypatch.delenv("BID_EVAL_MATERIAL_OCR_MAX_PAGES", raising=False)
    assert config.ocr_max_pages() == 350
    assert config.material_ocr_max_pages() == 60
