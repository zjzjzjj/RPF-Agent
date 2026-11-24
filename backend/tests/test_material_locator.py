# -*- coding: utf-8 -*-
"""目录驱动材料页定位（2026-11-24 PR2）契约测试。

三路定位：目录条目（主）→ 详细评审索引指针 → material_tags/关键词兜底；
目录失败不阻断；纯文本计算（零 OCR/LLM）。为存在性核验提供页源——
"详细材料大多数不需要被阅读"（用户定案），本模块不读内容。
"""
from __future__ import annotations


def _page(n, text="", tags=None, doc=1, source="text", blank=False):  # noqa: ANN001, ANN202
    return {"document_id": doc, "page_no": n, "text": text,
            "source": source, "known_blank": blank, "material_tags": tags or []}


def test_toc_dotted_windows() -> None:
    """点线目录（不限关键词）→ 各材料类窗口按条目切分。"""
    from app.engine.material_locator import locate_material_sections

    toc = _page(1, "投标文件目录\n" + "\n".join([
        "一、业绩证明材料………30",
        "二、管理体系认证证书………45",
        "三、合同复印件………60",
        "四、增值税专用发票………70",
    ]))
    pages = [toc] + [_page(n) for n in range(2, 90)]
    out = locate_material_sections(pages)
    assert out["performance"] and out["performance"][0].method == "toc"
    assert out["performance"][0].start == 30
    assert out["performance"][0].end == 44, "窗口到下一条目-1"
    assert out["cert"] and out["cert"][0].start == 45
    assert out["contract"] and out["contract"][0].start == 60
    assert out["invoice"] and out["invoice"][0].start == 70


def test_plain_toc_line_with_keyword() -> None:
    """无点线目录行靠扩充词表收（旧 _TOC_KEYWORDS 缺 业绩/证书——PR2 缺口）。"""
    from app.engine.material_locator import locate_material_sections

    toc = _page(1, "投标文件目录\n资质证书复印件 128\n项目业绩汇总 140\n人员情况表 150")
    pages = [toc] + [_page(n) for n in range(2, 160)]
    out = locate_material_sections(pages)
    assert out["cert"] and out["cert"][0].start == 128
    assert out["performance"] and out["performance"][0].start == 140
    assert out["personnel"] and out["personnel"][0].start == 150


def test_tag_and_keyword_fallback() -> None:
    """无目录：material_tags 页合并成 run；纯关键词页次之。"""
    from app.engine.material_locator import locate_material_sections

    pages = [
        _page(1, "封面"),
        _page(10, "合同扫描件", tags=["合同复印件"]),
        _page(11, "合同扫描件", tags=["合同复印件"]),
        _page(20, "增值税专用发票 发票代码 12345 开票日期 2024"),
        _page(30, "其他内容"),
    ]
    out = locate_material_sections(pages)
    contracts = [w for w in out["contract"] if w.method == "tag"]
    assert contracts and contracts[0].page_nos == [10, 11], out["contract"]
    invoices = [w for w in out["invoice"] if w.method == "keyword"]
    assert invoices and 20 in invoices[0].page_nos


def test_index_pointer_route() -> None:
    """详细评审索引表"见投标文件（X-Y）页"指针 → index 窗口。"""
    from app.engine.material_locator import locate_material_sections

    idx = _page(1, "详细评审索引表\n合同材料 见投标文件（30-35）页\n1")
    pages = [idx] + [_page(n) for n in range(2, 40)]
    out = locate_material_sections(pages)
    hits = [w for w in out["contract"] if w.method == "index"]
    assert hits and hits[0].start == 30 and hits[0].end == 35, out["contract"]


def test_kind_filter_and_index_merge() -> None:
    from app.engine.material_locator import (
        locate_material_sections, material_page_index,
    )

    toc = _page(1, "目录\n一、发票材料………10\n二、验收材料………20")
    pages = [toc] + [_page(n) for n in range(2, 30)]
    out = locate_material_sections(pages, kinds=("invoice",))
    assert list(out) == ["invoice"]
    idx = material_page_index(pages)
    assert idx["invoice"] == set(range(10, 20))
    assert idx["acceptance"] == set(range(20, 30))


def test_locator_failure_silent() -> None:
    """目录残缺/异常 → 空结果静默，不阻断（用户定案）。"""
    from app.engine.material_locator import (
        locate_material_sections, material_page_index,
    )

    assert locate_material_sections([]) != {}
    assert material_page_index([_page(1, "")]) == {}


def test_locator_is_pure_text_no_ocr() -> None:
    """定位只读文本层/标签——不得触发任何渲染/OCR 调用。"""
    from app.engine import material_locator

    calls = []
    import app.evidence.renderer as renderer

    orig = getattr(renderer, "render_page_png", None)
    renderer.render_page_png = lambda *a, **k: calls.append(1)
    try:
        toc = _page(1, "目录\n一、业绩材料………10")
        material_locator.locate_material_sections([toc] + [_page(n) for n in range(2, 15)])
    finally:
        renderer.render_page_png = orig
    assert not calls, "定位禁止调用渲染/OCR"
