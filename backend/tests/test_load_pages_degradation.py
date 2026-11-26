"""_load_pages 逐文档降级语义（2026-11-17 run 125 实证修复）。

旧实现一家有一个坏附件就整家抬走：天兴（.xls 解析失败）、青云（.docx 嵌入
图片无法映射页码）的 400 页主标书明明可读，技术/商务分却全部被暂定 0 兜底。
新语义：坏附件只记入 unreadable_docs，可读文档照常送审；全部不可读才抛错，
且按读取故障（reading_incomplete，未定分待补读）定性。
"""
from __future__ import annotations

import hashlib
import zipfile

import pytest

from app.db.models import Bidder, Document, DocumentPage, Project
from app.db.session import session_scope
from app.llm.base import LLMError
from app.services import automatic_scoring_service as scoring


def _mk_bidder(pid: str, name: str) -> int:
    with session_scope() as s:
        if s.get(Project, pid) is None:
            s.add(Project(id=pid, name=pid))
        s.flush()
        bidder = Bidder(project_id=pid, name=name, bid_price=None, price_status="missing")
        s.add(bidder)
        s.flush()
        return bidder.id


def _add_doc(bidder_id: int, pid: str, *, filename: str, stored_path: str,
             parse_status: str = "parsed", page_texts: list[str] | None = None,
             page_count: int | None = None) -> int:
    with session_scope() as s:
        doc = Document(project_id=pid, bidder_id=bidder_id, kind="bid",
                       parse_status=parse_status, original_filename=filename,
                       stored_path=stored_path,
                       sha256=hashlib.sha256(filename.encode()).hexdigest(),
                       page_count=page_count if page_count is not None
                       else (len(page_texts) if page_texts else None))
        s.add(doc)
        s.flush()
        for i, text in enumerate(page_texts or [], 1):
            s.add(DocumentPage(document_id=doc.id, page_no=i, text=text, source="text"))
        return doc.id


def test_unreadable_attachment_does_not_torpedo_bidder(app_env) -> None:  # noqa: ANN001
    """坏附件（解析失败）只降级该文档；可读文档页面照常返回并留清单。"""
    bid = _mk_bidder("deg", "甲")
    txt = app_env / "bid.txt"
    txt.write_text("技术方案正文", encoding="utf-8")
    good = _add_doc(bid, "deg", filename="投标文件.txt", stored_path=str(txt),
                    page_texts=["技术方案正文"])
    bad = _add_doc(bid, "deg", filename="投标分项报价表.xls", stored_path="",
                   parse_status="failed")

    pages, data_class, unreadable = scoring._load_pages(bid)
    assert len(pages) == 1 and pages[0]["document_id"] == good
    assert [d["document_id"] for d in unreadable] == [bad]
    assert "投标分项报价表.xls" in unreadable[0]["filename"]
    assert "尚未成功解析" in unreadable[0]["reason"]
    assert data_class == "synthetic"


def test_docx_embedded_images_drops_only_that_doc(app_env) -> None:  # noqa: ANN001
    """青云场景：.docx 嵌入图片被拒读时只丢该文档，主标书照常送审。"""
    bid = _mk_bidder("deg2", "乙")
    txt = app_env / "main.txt"
    txt.write_text("主标书", encoding="utf-8")
    _add_doc(bid, "deg2", filename="投标文件.txt", stored_path=str(txt), page_texts=["主标书"])
    docx = app_env / "情况表.docx"
    with zipfile.ZipFile(docx, "w") as zf:
        zf.writestr("word/document.xml", "<w:document/>")
        zf.writestr("word/media/image1.png", b"\x89PNG fake")
    _add_doc(bid, "deg2", filename="投标人情况表.docx", stored_path=str(docx), page_texts=["情况表"])

    pages, _, unreadable = scoring._load_pages(bid)
    assert len(pages) == 1
    assert len(unreadable) == 1
    assert "DOCX" in unreadable[0]["reason"] or "图像" in unreadable[0]["reason"]


def test_partial_page_count_drops_doc(app_env) -> None:  # noqa: ANN001
    """解析页数不完整的文档降级为不可读附件，不再整家抬走。"""
    bid = _mk_bidder("deg3", "丙")
    txt = app_env / "a.txt"
    txt.write_text("正文", encoding="utf-8")
    _add_doc(bid, "deg3", filename="好.txt", stored_path=str(txt), page_texts=["正文"])
    _add_doc(bid, "deg3", filename="坏.txt", stored_path=str(txt),
             page_texts=["只有第1页"], page_count=3)

    pages, _, unreadable = scoring._load_pages(bid)
    assert len(pages) == 1
    assert len(unreadable) == 1 and "页数不完整" in unreadable[0]["reason"]


def test_all_unreadable_marks_reading_incomplete(app_env) -> None:  # noqa: ANN001
    """全部文档不可读才抛错，且定性为读取故障（未定分待补读），不是材料缺失。"""
    bid = _mk_bidder("deg4", "丁")
    _add_doc(bid, "deg4", filename="坏.xls", stored_path="", parse_status="failed")

    with pytest.raises(LLMError) as excinfo:
        scoring._load_pages(bid)
    assert excinfo.value.reading_incomplete is True
    assert "未能读取" in str(excinfo.value)
    # 收尾闭合按读取故障定格：未定分，不写暂定 0
    assert scoring._fallback_score_for({"score_type": "count", "max_score": "3"}) is None


def test_reading_caveat_mentions_attachment(app_env) -> None:  # noqa: ANN001
    """送审范围限制说明必须点名不可读附件（注入评审提示词的文案源）。"""
    bid = _mk_bidder("deg5", "戊")
    txt = app_env / "e.txt"
    txt.write_text("正文", encoding="utf-8")
    _add_doc(bid, "deg5", filename="好.txt", stored_path=str(txt), page_texts=["正文"])
    _add_doc(bid, "deg5", filename="缺.xls", stored_path="", parse_status="failed")

    pages, _, unreadable = scoring._load_pages(bid)
    assert unreadable
    # reading_caveat_for 在 _execute_locked 闭包里；这里锚定清单结构供其组装
    assert {"document_id", "filename", "reason"} <= set(unreadable[0])
