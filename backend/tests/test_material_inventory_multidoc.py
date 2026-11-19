# -*- coding: utf-8 -*-
"""多文档投标包的确定性材料取数（2026-11-18 run128 回归锚）。

项目包导入会把 1 页《开标一览表.pdf》作为独立文档排在主标书之前；旧实现取
"第一份 .pdf"当主标书，业绩表/人员表解析全打在 1 页文件上 → 合同恒 0。
"""
from decimal import Decimal

from app.engine import material_inventory as mi


def _page(doc_id: int, page_no: int, text: str, path: str) -> dict:
    return {"document_id": doc_id, "page_no": page_no, "source": "text",
            "text": text, "stored_path": path}


def test_table_parsers_use_largest_pdf_document(monkeypatch):
    calls = {}

    def fake_contracts(pages, pdf_path):
        calls["contracts"] = (list(pages), pdf_path)
        return []

    def fake_persons(pages, pdf_path):
        calls["persons"] = (list(pages), pdf_path)
        return []

    monkeypatch.setattr(mi, "_contracts_from_case_table", fake_contracts)
    monkeypatch.setattr(mi, "_persons_from_tables", fake_persons)
    pages = [
        _page(1, 1, "开标一览表 三年投标总价 1408740", "/x/open_bid.pdf"),
        _page(2, 1, "投标文件封面", "/x/main_bid.pdf"),
        _page(2, 2, "投标人类似项目业绩案例 序号 项目名称 合同金额", "/x/main_bid.pdf"),
    ]
    mi.build_inventory(9, pages)
    assert calls["contracts"][1] == "/x/main_bid.pdf"
    assert [p["page_no"] for p in calls["contracts"][0]] == [1, 2]
    assert calls["persons"][1] == "/x/main_bid.pdf"
    assert [p["page_no"] for p in calls["persons"][0]] == [1, 2]


def test_case_table_falls_back_to_header_page_when_index_section_empty(monkeypatch):
    from app.evidence import case_evidence
    from app.evidence.case_evidence import CaseRow, SectionGuess
    from app.services import scoring_service

    calls = []

    def fake_locate(recs, *args, **kwargs):
        return SectionGuess(10, 20, "index_offset", 0.5, 0)

    def fake_parse(pdf_path, section, recs, header_rows):
        calls.append((section.start, section.end))
        if section.start == 30:
            return [CaseRow(index=1, name="某运维项目", amount_yuan=Decimal("600000"),
                            sign_date=None, page=30, raw="1 | 某运维项目 | 600000")]
        return []

    monkeypatch.setattr(case_evidence, "locate_section", fake_locate)
    monkeypatch.setattr(case_evidence, "parse_case_table_at_section", fake_parse)
    monkeypatch.setattr(case_evidence, "link_case_blocks",
                        lambda rows, recs, section: rows)
    monkeypatch.setattr(scoring_service, "_cases_from_blocks",
                        lambda blocks: [{"name": "某运维项目", "amount_yuan": "600000"}])
    pages = [
        _page(2, 10, "业绩章节正文", "/x/main.pdf"),
        _page(2, 30, "业绩案例 序号 项目名称 合同金额 合同签订时间", "/x/main.pdf"),
    ]
    records = mi._contracts_from_case_table(pages, "/x/main.pdf")
    assert calls == [(10, 20), (30, 30)]
    assert len(records) == 1 and records[0].name == "某运维项目"
