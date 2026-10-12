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
