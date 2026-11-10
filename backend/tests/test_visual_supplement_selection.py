"""Visual requests with a fixed page set do not rescan unrelated text."""
from __future__ import annotations

import pytest

from app.engine import auto_review
from app.services import automatic_scoring_service as scoring


def pages():
    return [{"document_id": 77, "page_no": number, "stored_path": "synthetic.pdf",
             "sha256": "a" * 64, "page_count": 5, "text": "原页材料文字" * 30,
             "source": "text", "has_text_layer": True,
             "known_blank": number == 2, "unreadable": number == 3,
             "requires_vision": number == 4}
            for number in range(1, 6)]


@pytest.mark.parametrize(("requested", "exhaustive", "expected"), [
    ([(77, 1), (77, 2)], False, [1, 2, 3, 4]),
    ([(77, 1)], True, [1, 3, 4, 5]),
    ([], True, [1, 3, 4, 5]),
])
def test_fixed_visual_targets_retain_unreadable_pages_without_full_text_retrieval(
        monkeypatch, requested, exhaustive, expected):
    """Keep explicit blank pages, exhaustive precedence and all reading obligations."""
    original = pages()
    rendered = []

    class Image:
        def read_bytes(self):
            return b"synthetic rendered original page"

    def render(sha256, path, page_no, **kwargs):
        rendered.append(page_no)
        return Image()

    monkeypatch.setattr(auto_review, "select_candidate_pages",
                        lambda *args, **kwargs: pytest.fail("fixed visual targets must bypass text ranking"))
    monkeypatch.setattr("app.evidence.renderer.render_page_png", render)
    result = scoring._supplement_pages({"_visual_page_keys": requested}, original,
                                       visual=True, exhaustive=exhaustive)
    assert rendered == expected
    assert [page["page_no"] for page in result if page.get("image_url")] == expected
    assert [page["page_no"] for page in result] == [1, 2, 3, 4, 5]
    assert all(not page.get("image_url") for page in original)
    assert result[2]["unreadable"] and result[3]["requires_vision"]


def test_visual_without_fixed_targets_still_uses_ranked_pages_and_reading_obligations(monkeypatch):
    original = pages()
    ranked, rendered = [], []

    def select(rule, supplied, max_pages):
        ranked.append((len(supplied), max_pages))
        return [supplied[-1]]

    class Image:
        def read_bytes(self):
            return b"synthetic rendered original page"

    def render(sha256, path, page_no, **kwargs):
        rendered.append(page_no)
        return Image()

    monkeypatch.setattr(auto_review, "select_candidate_pages", select)
    monkeypatch.setattr("app.evidence.renderer.render_page_png", render)
    result = scoring._supplement_pages({}, original, visual=True)
    assert ranked == [(5, 8)]
    assert rendered == [3, 4, 5]
    assert [page["page_no"] for page in result if page.get("image_url")] == rendered


def test_ocr_still_ranks_text_even_when_visual_targets_are_present(monkeypatch):
    original = pages()
    ranked, recognized = [], []

    def select(rule, supplied, max_pages):
        ranked.append((len(supplied), max_pages))
        return [supplied[0]]

    def ocr(path, page_numbers, **kwargs):
        recognized.extend(page_numbers)
        return {number: "补读核验所得原文" * 20 for number in page_numbers}

    monkeypatch.setattr(auto_review, "select_candidate_pages", select)
    monkeypatch.setattr("app.parsing.pdf_pymupdf.ocr_pages_text", ocr)
    monkeypatch.setattr(scoring.config, "OCR_MIXED_PAGE_MAX_CHARS", 1000)
    result = scoring._supplement_pages({"_visual_page_keys": [(77, 5)]}, original, visual=False)
    assert ranked == [(5, 8)]
    assert recognized == [1, 3, 4]
    assert [page["page_no"] for page in result if page.get("supplemented")] == [1, 3, 4]
