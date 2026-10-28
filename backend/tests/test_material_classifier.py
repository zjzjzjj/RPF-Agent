"""材料分类规则层单测（纯文本，不碰 PDF/DB）。

覆盖：目录页→toc、四类标题命中、碎页继承、跨类段、表格/图片段归属、
LLM 兜底契约（FakeGateway）与失败降级。
"""
from __future__ import annotations

from app.material_index import llm_fallback, rules
from app.material_index.catalog import material_category_for


def _pages(*pairs: tuple[int, str, str]) -> list[rules.PageInput]:
    return [rules.PageInput(n, t, src) for n, t, src in pairs]


def test_toc_page_marked_other_toc() -> None:
    pages = _pages(
        (1, "投标文件格式………3\n资格证明文件………5\n技术方案………20\n商务部分………30\n报价表………40", "text"),
    )
    rows = rules.classify_pages(pages)
    page_rows = [r for r in rows if r.seg_kind == "page"]
    assert page_rows[0].category == "other"
    assert "toc" in page_rows[0].tags
    # 目录条目不产生跨类 text 段
    assert not [r for r in rows if r.seg_kind == "text"]


def test_title_line_hits_each_category() -> None:
    cases = [
        ("开标一览表\n项目名称 投标总价", "price"),
        ("资格证明文件\n营业执照", "qualification"),
        ("商务服务方案\n服务承诺", "commercial"),
        ("技术方案\n实施方案", "technical"),
    ]
    for text, want in cases:
        rows = rules.classify_pages(_pages((1, text, "text")))
        assert rows[0].category == want, (text, rows[0])
        assert rows[0].confidence >= rules.CONF_STRONG
        assert rows[0].tags


def test_node_title_chain_wins() -> None:
    pages = _pages((3, "本页正文无关键词，只有叙述。", "text"))
    rows = rules.classify_pages(pages, node_titles={3: ["商务服务方案"]})
    assert rows[0].category == "commercial"
    assert rows[0].confidence >= rules.CONF_NODE
    assert "章节标题链" in rows[0].basis


def test_sparse_page_inherits_strong_anchor() -> None:
    pages = _pages(
        (1, "技术方案\n本项目的架构设计说明。", "text"),
        (2, "（扫描图）", "ocr"),
        (3, "继续扫描页", "ocr"),
    )
    rows = rules.classify_pages(pages)
    by_no = {r.page_no: r for r in rows if r.seg_kind == "page"}
    assert by_no[2].category == "technical"
    assert by_no[2].confidence == rules.CONF_INHERIT
    assert "继承自 p1" in by_no[2].basis
    assert by_no[3].category == "technical"


def test_inherit_stops_at_next_strong_page() -> None:
    pages = _pages(
        (1, "技术方案", "text"),
        (2, "（扫描图）", "ocr"),
        (3, "资格证明文件", "text"),
        (4, "（扫描图）", "ocr"),
    )
    rows = rules.classify_pages(pages)
    by_no = {r.page_no: r for r in rows if r.seg_kind == "page"}
    assert by_no[2].category == "technical"
    assert by_no[4].category == "qualification"


def test_weak_hit_needs_two_keywords() -> None:
    # 单词命中 → 弱判（conf 0.5）
    rows = rules.classify_pages(_pages((1, "本页提到售后服务的响应时间。", "text")))
    assert rows[0].category == "commercial"
    assert rows[0].confidence == rules.CONF_WEAK_SINGLE
    # 零命中 → other（规则层未判出，交给 LLM）
    rows = rules.classify_pages(_pages((1, "完全无关的页面内容。", "text")))
    assert rows[0].category == "other"
    assert rows[0].confidence == 0.0
    assert rules.needs_llm(rows[0])


def test_cross_class_title_makes_text_segment() -> None:
    # 技术方案页里插了社保明细表 → text 段例外行
    pages = _pages((1, "技术方案\n实施方案\n社保证明\n人员社保缴纳明细如下。", "text"))
    rows = rules.classify_pages(pages)
    page_rows = [r for r in rows if r.seg_kind == "page"]
    text_rows = [r for r in rows if r.seg_kind == "text"]
    assert page_rows[0].category == "technical"
    assert text_rows and text_rows[0].category == "qualification"
    assert "社保证明" in text_rows[0].tags


def test_table_segment_inherits_page_or_caption() -> None:
    segs = [rules.LayoutSeg(1, "table", 0, (10, 10, 200, 100), 0.2)]
    pages = _pages((1, "开标一览表\n序号 分项名称 金额", "text"))
    rows = rules.classify_pages(pages, layout_segs=segs)
    table_rows = [r for r in rows if r.seg_kind == "table"]
    assert len(table_rows) == 1
    assert table_rows[0].category == "price"
    assert table_rows[0].bbox_json is not None
    assert table_rows[0].bbox_json["x0"] == 10

    # 无表名 → 继承页类别
    segs = [rules.LayoutSeg(2, "image", 0, (0, 0, 100, 100), 0.4)]
    pages = _pages((2, "技术方案\n架构设计说明。", "text"))
    rows = rules.classify_pages(pages, layout_segs=segs)
    img_rows = [r for r in rows if r.seg_kind == "image"]
    assert img_rows[0].category == "technical"
    assert img_rows[0].tags  # 继承页的 tag


# ---------------------------------------------------------------------------
# LLM 兜底（FakeGateway）
# ---------------------------------------------------------------------------
class _FakeResult:
    text = "{}"


class _FakeGateway:
    def __init__(self, payload: dict | Exception):
        self._payload = payload
        self.calls = 0

    def chat_json(self, system, user, out_model, **kw):  # noqa: ANN001
        self.calls += 1
        if isinstance(self._payload, Exception):
            raise self._payload
        return out_model.model_validate(self._payload), _FakeResult()


def test_llm_fallback_fills_unknown(monkeypatch) -> None:  # noqa: ANN001
    from app import config
    from app.llm import gateway as gw

    monkeypatch.setenv("BID_EVAL_LLM_PROVIDER", "openai")
    fake = _FakeGateway({
        "items": [
            {"page_no": 1, "category": "commercial", "tags": ["服务承诺"], "confidence": 0.8, "reason": "页内有服务承诺标题"},
        ]
    })
    monkeypatch.setattr(gw, "get_gateway", lambda: fake)
    rows = rules.classify_pages(_pages((1, "本页无关键词句。", "text")))
    assert rules.needs_llm(rows[0])
    llm_fallback.fill_unknown(1, rows, {1: "本页无关键词句。"})
    assert rows[0].category == "commercial"
    assert rows[0].method == "llm"
    assert rows[0].tags == ["服务承诺"]
    assert fake.calls == 1
    assert config.material_index_enabled()  # 顺带确认开关默认开


def test_llm_fallback_failure_degrades(monkeypatch) -> None:  # noqa: ANN001
    from app.llm import gateway as gw
    from app.llm.base import LLMError

    monkeypatch.setenv("BID_EVAL_LLM_PROVIDER", "openai")
    fake = _FakeGateway(LLMError("boom"))
    monkeypatch.setattr(gw, "get_gateway", lambda: fake)
    rows = rules.classify_pages(_pages((1, "无关键词。", "text")))
    llm_fallback.fill_unknown(1, rows, {1: "无关键词。"})
    assert rows[0].category == "other"
    assert "llm_failed" in rows[0].basis


def test_llm_fallback_skips_strong_and_toc(monkeypatch) -> None:  # noqa: ANN001
    from app.llm import gateway as gw

    monkeypatch.setenv("BID_EVAL_LLM_PROVIDER", "openai")
    fake = _FakeGateway({"items": []})
    monkeypatch.setattr(gw, "get_gateway", lambda: fake)
    pages = _pages(
        (1, "目录\n资格证明文件………3\n技术方案………8", "text"),
        (2, "开标一览表\n投标总价", "text"),
    )
    rows = rules.classify_pages(pages)
    llm_fallback.fill_unknown(1, rows, {1: pages[0].text, 2: pages[1].text})
    assert fake.calls == 0  # 强命中/目录页不送 LLM


def test_material_category_for_mapping() -> None:
    assert material_category_for({"category": "qualification"}) == "qualification"
    assert material_category_for({"category": "compliance"}) == "qualification"
    assert material_category_for({"category": "commercial"}) == "commercial"
    assert material_category_for({"category": "technical"}) == "technical"
    assert material_category_for({"category": "price"}) == "price"
    assert material_category_for({}) == "other"
