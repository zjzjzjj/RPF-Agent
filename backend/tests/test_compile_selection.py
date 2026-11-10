"""章节感知选取的单元测试（纯函数，无需夹具）。

背景：真实招标文件的"评标办法/评分细则"通常排在文档最后（浙江中烟某项目在第 89-97 页，
全文 6 万字符）。旧的盲目截前 30k 字符会把它整段丢掉，编译出来只有资格与否决条款，
价格分/技术分/商务分一条都抽不到。这里钉住"评分章节必须入选"这条底线。
"""
from __future__ import annotations

from app.rules.compiler import COMPILE_TIERS, select_compile_text


def _pages(*specs: tuple[int, str]) -> list[tuple[int, str]]:
    return list(specs)


def test_short_document_passes_through_untouched():
    """预算内的文档原样返回，行为与改动前一致（不得引入回归）。"""
    pages = _pages((1, "招标公告"), (2, "投标人须知"), (3, "评标办法"))
    text, meta = select_compile_text(pages, budget=10_000)
    assert text == "招标公告\n投标人须知\n评标办法"
    assert meta["truncated"] is False
    assert meta["chars"] == meta["total_chars"]


def test_scoring_section_at_tail_survives_truncation():
    """核心回归：评分办法在文档末尾时，必须在预算内被保住。"""
    pages = _pages(
        *[(i, "投标人须知 " + "资格要求 否决 符合性审查 " * 40) for i in range(1, 60)],
        (89, "第六部分 评标办法 五、评标细则 商务部分（9 分）"),
        (90, "技术部分（31 分）主观分 客观分 运维方案 0-9"),
        (91, "价格分 报价部分 最佳报价 每低 1 个百分点扣 0.4 分"),
    )
    text, meta = select_compile_text(pages, budget=5_000)
    assert meta["truncated"] is True
    assert "评标细则" in text
    assert "最佳报价" in text
    assert 89 in meta["pages"] and 91 in meta["pages"]


def test_continuation_page_without_keywords_is_pulled_in():
    """续页自身零命中，仍应因紧邻命中页而入选。

    真实场景：第 90 页写"符合性审查"标题与条件 (1)，条件 (2)-(15) 全在第 91 页，
    第 91 页不含任何关键词——只按本页命中排序会把续页挤掉，条件就断了。
    """
    filler = "用户需求书 " + "技术要求 " * 60
    pages = _pages(
        *[(i, filler) for i in range(1, 20)],
        (30, "2.2 符合性审查（形式审查） 投标人存在以下情形之一的，否决其投标："),
        (31, "（2）投标文件未按要求加盖公章的；（3）未填写商务技术偏离表的；"),
    )
    _text, meta = select_compile_text(pages, budget=2_500)
    assert 30 in meta["pages"], "命中页应入选"
    assert 31 in meta["pages"], "零命中续页应随相邻命中页入选"


def test_output_is_in_page_order():
    """选取结果必须按页序拼接——引文要能在 document_pages 上逐字定位。"""
    pages = _pages(
        (1, "招标公告 资格要求"),
        (2, "用户需求书 " + "技术要求 " * 30),
        (3, "评标办法 评分细则 最佳报价"),
        (4, "合同条款 " + "违约责任 " * 30),
    )
    text, meta = select_compile_text(pages, budget=400)
    assert meta["pages"] == sorted(meta["pages"])
    # 文本中先后顺序与页序一致
    if "招标公告" in text and "评标办法" in text:
        assert text.index("招标公告") < text.index("评标办法")


def test_budget_is_respected():
    """选取不得超出预算。"""
    pages = _pages(*[(i, "评标办法 评分细则 " + "内容" * 200) for i in range(1, 40)])
    text, meta = select_compile_text(pages, budget=3_000)
    assert len(text) <= 3_000
    assert meta["chars"] <= 3_000


def test_tiers_cover_scoring_before_eligibility():
    """评分档位必须排在资格档位之前——预算不够时先保评分办法。"""
    names = [name for name, _kw in COMPILE_TIERS]
    assert names[0] == "scoring"
    assert "eligibility" in names


def test_find_node_for_quote_tolerates_soft_line_break(app_env):
    """PDF 软换行不得导致节点定位失败。

    真实场景（浙江中烟项目实测）：节点文本里换行，normalize 后成为空格；模型引文
    跨行逐字复制、不含空格。朴素的子串包含会因这一个空格失配——引文核验通过了，
    节点却定位不到，整条规则被降级为 needs_input（26 条里 19 条栽在这里）。
    这里与证据核验的 ws_insensitive 口径对齐。
    """
    from app.db.models import DocNode, Document, Project
    from app.db.session import session_scope
    from app.rules.compiler import find_node_for_quote

    with session_scope() as s:
        s.add(Project(id="P1", name="t"))
        s.flush()  # documents.project_id 是外键，必须先落库
        doc = Document(
            project_id="P1",
            kind="tender",
            original_filename="t.pdf",
            stored_path="t.pdf",
            sha256="0" * 64,
            parse_status="parsed",
        )
        s.add(doc)
        s.flush()
        doc_id = doc.id
        s.add(
            DocNode(
                document_id=doc_id,
                node_id="n1",
                kind="item",
                page=1,
                depth=4,
                text="（6）投标报价超过招标文件中的最高限价的，以及采用工程量清单招标的工程项目\n超过清单分项报价的；",
            )
        )

    quote = (
        "（6）投标报价超过招标文件中的最高限价的，"
        "以及采用工程量清单招标的工程项目超过清单分项报价的；"
    )
    assert find_node_for_quote(doc_id, 1, quote) == "n1"
    assert find_node_for_quote(doc_id, 1, "这段原文里没有的内容") is None


def test_split_for_extraction_respects_chunk_size() -> None:
    """按行切块：每块不超预算、内容不丢失、不顺断句子。"""
    from app.rules.compiler import split_for_extraction

    text = "\n".join(f"第{i}条 这是招标文件的第 {i} 条内容。" for i in range(1, 61))
    chunks = split_for_extraction(text, chunk_chars=300)
    assert len(chunks) > 1
    assert all(len(c) <= 300 + 200 for c in chunks)  # 行粒度允许单行溢出余量
    # 内容完整：拼接后每条都在
    joined = "".join(chunks)
    for i in (1, 30, 60):
        assert f"第{i}条" in joined
    # 每块以行边界结束（不把一行劈成两半）
    assert all(c.endswith("\n") or c == chunks[-1] for c in chunks[:-1])


def test_split_for_extraction_no_split_when_short() -> None:
    from app.rules.compiler import split_for_extraction

    assert split_for_extraction("短文本", chunk_chars=1000) == ["短文本"]
    assert split_for_extraction("任意长度", chunk_chars=0) == ["任意长度"]


def test_split_for_extraction_hard_splits_long_line() -> None:
    from app.rules.compiler import split_for_extraction

    chunks = split_for_extraction("A" * 250, chunk_chars=100)
    assert all(len(c) <= 100 for c in chunks)
    # 内容不丢（切分按行处理，会补一个换行符）
    assert sum(len(c) for c in chunks) in (250, 251)
    assert "".join(chunks).replace("\n", "") == "A" * 250


def test_dedupe_extracted_across_chunks() -> None:
    """分块边界重复抽到的同一条规则只保留一次（名称规范化后比较）。"""
    from app.rules.compiler import dedupe_extracted
    from app.rules.model import ExtractedCriterion

    def mk(name: str, category: str = "qualification") -> ExtractedCriterion:
        return ExtractedCriterion(name=name, category=category, score_type="veto", is_veto=True)

    items = [
        mk("具有独立法人资格"),
        mk("具有独立 法人资格"),      # 空白差异 → 视为同一条
        mk("具有独立法人资格", "compliance"),  # 类别不同 → 保留
        mk("未被列入失信名单"),
    ]
    out = dedupe_extracted(items)
    assert len(out) == 3
    assert out[0].name == "具有独立法人资格"
    assert {c.category for c in out} == {"qualification", "compliance"}


def test_dedupe_keeps_same_name_subitems_with_different_scores() -> None:
    """同名不同分值的子项行（评标细则表空名列形态）不得误杀。"""
    from decimal import Decimal

    from app.rules.compiler import dedupe_extracted
    from app.rules.model import ExtractedCriterion

    def mk(name: str, score: str | None) -> ExtractedCriterion:
        return ExtractedCriterion(
            name=name, category="technical", score_type="subjective",
            max_score=Decimal(score) if score else None,
        )

    items = [
        mk("服务保障情况", "2"),
        mk("服务保障情况", "3"),   # 子项，旧版被误杀
        mk("服务保障情况", "3"),   # 与上条全同 → 真重复，丢
        mk("服务保障情况", "2"),   # 与首条全同 → 真重复，丢
    ]
    out = dedupe_extracted(items)
    assert [str(c.max_score) for c in out] == ["2", "3"]


def test_dedupe_merges_missing_score_into_scored_twin() -> None:
    """同名一有一无分值：保留有分值的，并入缺失方的描述/引文。"""
    from decimal import Decimal

    from app.rules.compiler import dedupe_extracted
    from app.rules.model import ExtractedCriterion

    a = ExtractedCriterion(name="业绩实力", category="commercial", score_type="count",
                           max_score=None, description="旧描述")
    b = ExtractedCriterion(name="业绩实力", category="commercial", score_type="count",
                           max_score=Decimal("6"), quote="逐字原文")
    out = dedupe_extracted([a, b])
    assert len(out) == 1
    assert str(out[0].max_score) == "6"
    assert out[0].description == "旧描述"
    assert out[0].quote == "逐字原文"

    # 反序（有分值在前）：仍保一条，且分值不丢
    out2 = dedupe_extracted([b.model_copy(), a.model_copy()])
    assert len(out2) == 1
    assert str(out2[0].max_score) == "6"

def test_dedupe_keeps_same_name_different_score_type() -> None:
    """同名但评分类型不同（如客观计分 vs 主观）视为不同条目。"""
    from decimal import Decimal

    from app.rules.compiler import dedupe_extracted
    from app.rules.model import ExtractedCriterion

    items = [
        ExtractedCriterion(name="服务团队", category="technical", score_type="count",
                           max_score=Decimal("2")),
        ExtractedCriterion(name="服务团队", category="technical", score_type="subjective",
                           max_score=Decimal("2")),
    ]
    out = dedupe_extracted(items)
    assert len(out) == 2


def test_split_for_extraction_protected_span_not_cut() -> None:
    """protected_spans 区间（评标细则章节）不被 chunk 边界切开。"""
    from app.rules.compiler import split_for_extraction

    head = "\n".join(f"第{i}条 前置条款内容。" for i in range(1, 40))
    scoring = "评标细则\n" + "\n".join(
        f"序号{i} 评审因素{i} 客观分 0-{i % 5 + 1} 细则内容第{i}行。" for i in range(1, 30))
    text = head + "\n" + scoring
    span = (head.__len__() + 1, len(text))
    chunks = split_for_extraction(text, chunk_chars=300, protected_spans=[span])
    # 评分细则整段出现在同一块
    whole = next((c for c in chunks if "评标细则" in c), None)
    assert whole is not None
    assert "细则内容第1行" in whole and "细则内容第29行" in whole
    # 内容不丢、原文可复原（换行保留）
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_split_for_extraction_no_span_behavior_unchanged() -> None:
    """不传 protected_spans：与旧版逐字节一致（回归锚）。"""
    from app.rules.compiler import split_for_extraction

    text = "\n".join(f"第{i}条 内容。" for i in range(1, 30))
    chunks = split_for_extraction(text, chunk_chars=200)
    assert all(len(c) <= 200 + 80 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_protected_scoring_span_locates_last_title() -> None:
    from app.rules.compiler import _protected_scoring_span

    text = "目录\n评标办法见第 90 页\n" + "x" * 100 + "\n第六部分 评标办法\n评标细则\n1、商务部分（10 分）"
    span = _protected_scoring_span(text)
    assert span is not None
    assert text[span[0]:].startswith("评标细则")  # 最后一个标题命中处
    assert _protected_scoring_span("没有任何章节标题的文本") is None
