# -*- coding: utf-8 -*-
"""材料审核视觉大模型兜底（2026-11-23 优化②）离线契约测试。

红线锁定：否决/资格/符合性格 VL 输出 fail 必须拒收（AI 不否决）；分数越界/
契约不合法/涉密拦截 → 维持暂定（天然安全态）；预算每格/每家上限。
run 151 后契约：一页一图多图单请求（≤max_images 页图）；拒收类错误不重试；
兜底在 worker 线程并行执行。
"""
from __future__ import annotations

from decimal import Decimal

from app.engine import vl_fallback
from app.llm.base import LLMError


class FakeResult:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeGateway:
    def __init__(self, content: str = "", exc: Exception | None = None) -> None:
        self.content = content
        self.exc = exc
        self.calls: list[dict] = []

    def chat(self, system, user, *, images=None, data_class="synthetic", **kw):  # noqa: ANN001, ANN202
        self.calls.append({"system": system, "user": user, "images": images,
                           "data_class": data_class})
        if self.exc is not None:
            raise self.exc
        return FakeResult(self.content)


PAGE_IMAGES = [
    {"data_url": "data:image/png;base64,AA", "document_id": 1, "page_no": 1},
    {"data_url": "data:image/png;base64,BB", "document_id": 1, "page_no": 2},
    {"data_url": "data:image/png;base64,CC", "document_id": 1, "page_no": 3},
]

CRIT = {"name": "供货业绩", "description": "每提供1个案例得2分，最高6分", "max_score": "6",
        "score_type": "count"}
VERDICT_CRIT = {"name": "签章检查", "description": "投标文件须加盖公章", "score_type": "veto"}


def test_budget_per_cell_and_bidder() -> None:
    b = vl_fallback.Budget(max_per_cell=2, max_per_bidder=3)
    assert b.consume(1, "c1") == (True, "")
    assert b.consume(1, "c1") == (True, "")
    ok, why = b.consume(1, "c1")
    assert not ok and why == "cell_budget_exhausted"
    assert b.consume(1, "c2") == (True, "")
    ok, why = b.consume(1, "c3")
    assert not ok and why == "bidder_budget_exhausted"


def test_rescue_scored_happy_path_page_images() -> None:
    gw = FakeGateway(
        '{"verdict":"pass","score":4,"observations":[{"document_id":1,"page_no":2,'
        '"observation":"第2页供货合同一份，金额80万"}],"reasoning":"2个案例各2分"}')
    res = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw)
    assert res.kind == "scored"
    assert res.score == Decimal("4")
    assert gw.calls[0]["images"] == [p["data_url"] for p in PAGE_IMAGES]
    # 页码对照必须进提示词（VL 看到的是页图，页码靠映射告知）
    assert "第1张图=文档1第1页" in gw.calls[0]["user"]
    assert gw.calls[0]["data_class"] == "synthetic"


def test_rescue_verdict_fail_rejected() -> None:
    """红线：否决/资格/符合性格 VL 输出 fail 一律拒收（AI 不否决）。"""
    gw = FakeGateway(
        '{"verdict":"fail","score":null,"observations":[{"document_id":1,"page_no":1,'
        '"observation":"未见公章"}],"reasoning":"不符合","clues":["未见签章"]}')
    res = vl_fallback.rescue(VERDICT_CRIT, PAGE_IMAGES, is_verdict=True, gateway=gw)
    assert res.kind == "failed"
    assert res.reason == "vl_verdict_rejected"
    assert res.clues == ["未见签章"], "线索保留供人工"
    assert len(gw.calls) == 1, "单次尝试即拒收（本层不重试）"


def test_rescue_verdict_clue_only() -> None:
    gw = FakeGateway(
        '{"verdict":"pass","score":null,"observations":[{"document_id":1,"page_no":1,'
        '"observation":"投标函上有红色印章痕迹"}],"reasoning":"已见签章"}')
    res = vl_fallback.rescue(VERDICT_CRIT, PAGE_IMAGES, is_verdict=True, gateway=gw)
    assert res.kind == "clue_only"
    assert res.score is None


def test_rescue_score_out_of_range_rejected() -> None:
    for bad in ("8", "-1", "null"):
        gw = FakeGateway(
            f'{{"verdict":"pass","score":{bad},"observations":[{{"document_id":1,'
            '"page_no":1,"observation":"x"}],"reasoning":"r"}')
        res = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw)
        assert res.kind == "failed" and res.reason == "vl_no_valid_score", bad
    # 非数字 score → 契约校验拒收（同样维持暂定）
    gw = FakeGateway('{"verdict":"pass","score":"many","observations":[{"document_id":1,'
                     '"page_no":1,"observation":"x"}],"reasoning":"r"}')
    res = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw)
    assert res.kind == "failed" and res.reason.startswith("vl_contract_invalid")


def test_rescue_contract_invalid() -> None:
    gw = FakeGateway('{"verdict":"pass"}')  # 无 observations
    res = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw)
    assert res.kind == "failed" and res.reason == "vl_no_observations"
    gw2 = FakeGateway('随便说点什么')
    res2 = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw2)
    assert res2.kind == "failed" and res2.reason.startswith("vl_contract_invalid")


def test_rescue_rejected_no_retry() -> None:
    """HTTP 400 类拒收（transient=False）：单次调用即失败，不重试（run 151 实证）。"""
    gw = FakeGateway(exc=LLMError("模型服务商拒绝请求（HTTP 400）", transient=False))
    res = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw)
    assert res.kind == "failed" and res.reason == "vl_call_rejected"
    assert len(gw.calls) == 1


def test_rescue_transient_exhausted() -> None:
    """瞬时错误（timeout 等，provider 内部已重试耗尽）→ 维持暂定。"""
    gw = FakeGateway(exc=LLMError("模型调用失败：Request timed out.", transient=True))
    res = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw)
    assert res.kind == "failed" and res.reason.startswith("vl_call_failed")
    assert len(gw.calls) == 1


def test_rescue_gateway_error_maintains_fallback() -> None:
    gw = FakeGateway(exc=RuntimeError("data_class confidential 被拦"))
    res = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw)
    assert res.kind == "failed" and res.reason.startswith("vl_call_failed")


def test_rescue_disabled_switch() -> None:
    import os

    os.environ["BID_EVAL_VL_FALLBACK"] = "0"
    try:
        res = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=FakeGateway("{}"))
        assert res.kind == "failed" and res.reason == "vl_fallback_disabled"
    finally:
        os.environ.pop("BID_EVAL_VL_FALLBACK", None)


def test_rescue_no_pages() -> None:
    res = vl_fallback.rescue(CRIT, [], gateway=FakeGateway("{}"))
    assert res.kind == "failed" and res.reason == "no_material_pages"


# ---- AS 接线：触发集、页图选择、outcome 形状 ---------------------------------

def _png_env(tmp_path, monkeypatch):  # noqa: ANN001, ANN202
    from app.evidence import renderer

    rendered: list[int] = []

    def fake_render_page_png(sha256, stored_path, page_no, scale=None, **kw):  # noqa: ANN001, ANN202
        from PIL import Image

        rendered.append(int(page_no))
        png = tmp_path / f"p{page_no}.png"
        if not png.exists():
            Image.new("RGB", (12, 12), (page_no * 9 % 255, 40, 90)).save(png)
        return png

    monkeypatch.setattr(renderer, "render_page_png", fake_render_page_png)
    return rendered


def test_maybe_vl_rescue_triggers_only_on_fallback(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    from app.services import automatic_scoring_service as asv
    from app.services.automatic_scoring_service import CellOutcome

    normal = CellOutcome("pass", "ok", Decimal("3"), [], [], "deterministic")
    out, err = asv._maybe_vl_rescue(1, 2, CRIT, normal, None, {}, vl_fallback.Budget())
    assert out is normal and err is None

    fb = CellOutcome("concern", "暂定", None, [], [], "fallback", 1,
                     {"fallback": {"kind": "provisional_zero"}})
    called = {}

    def fake_rescue(criterion, page_images, **kw):  # noqa: ANN001, ANN202
        called["yes"] = True
        return vl_fallback.VlResult("failed", reason="nope")

    monkeypatch.setattr(vl_fallback, "rescue", fake_rescue)
    _png_env(tmp_path, monkeypatch)
    pages = [{"document_id": 9, "page_no": 1, "stored_path": "x.pdf", "sha256": "s"}]
    out, err = asv._maybe_vl_rescue(1, 2, CRIT, fb, None,
                                   {2: (pages, "synthetic")}, vl_fallback.Budget())
    assert called.get("yes"), "fallback 族 outcome 必须触发 VL 尝试"
    assert out is fb and err is None


def test_maybe_vl_rescue_page_cap_and_named_first(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    from app.services import automatic_scoring_service as asv
    from app.services.automatic_scoring_service import CellOutcome

    rendered = _png_env(tmp_path, monkeypatch)
    captured = {}

    def fake_rescue(criterion, page_images, **kw):  # noqa: ANN001, ANN202
        captured["pages"] = page_images
        return vl_fallback.VlResult("failed", reason="nope")

    monkeypatch.setattr(vl_fallback, "rescue", fake_rescue)
    fb = CellOutcome("concern", "暂定", None, [], [], "fallback", 1,
                     {"fallback": {"kind": "reading_incomplete",
                                   "unreadable_pages": [{"document_id": 9, "page_no": 7,
                                                         "stored_path": "x.pdf",
                                                         "sha256": "s"}]}})
    pages = [{"document_id": 9, "page_no": n, "stored_path": "x.pdf", "sha256": "s"}
             for n in range(1, 13)]  # 12 页候选
    asv._maybe_vl_rescue(1, 2, CRIT, fb, None,
                         {2: (pages, "synthetic")}, vl_fallback.Budget())
    assert captured["pages"][0]["page_no"] == 7, "fallback 点名的不可读页优先"
    assert len(captured["pages"]) == 8, "每请求页图上限 8"
    assert rendered[0] == 7


def test_maybe_vl_rescue_scored_outcome_shape(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    from app.services import automatic_scoring_service as asv
    from app.services.automatic_scoring_service import CellOutcome

    _png_env(tmp_path, monkeypatch)
    monkeypatch.setattr(vl_fallback, "rescue", lambda *a, **k: vl_fallback.VlResult(
        "scored", score=Decimal("4"),
        observations=[{"document_id": 9, "page_no": 1, "observation": "合同一份"}],
        reasoning="按细则 2 案例"))
    fb = CellOutcome("concern", "暂定", None, [], [], "fallback", 1,
                     {"fallback": {"kind": "value_extraction_unresolved"}})
    pages = [{"document_id": 9, "page_no": 1, "stored_path": "x.pdf", "sha256": "s"}]
    out, err = asv._maybe_vl_rescue(1, 2, CRIT, fb, None,
                                   {2: (pages, "synthetic")}, vl_fallback.Budget())
    assert err is None
    assert out.basis == "visual_fallback"
    assert out.score == Decimal("4")
    assert out.evidence and out.evidence[0]["kind"] == "visual"
    assert out.pages[0]["image_url"].startswith("data:image/png;base64,"), "页级 image_url 已就位"
    assert out.raw["visual_fallback"]["score"] == "4"


def test_vision_gateway_timeout_passed(monkeypatch) -> None:  # noqa: ANN001
    """VL 网关必须带 vl_timeout_s 超时（默认 90s），不陪跑 llm_timeout_s。"""
    from app import config
    from app.engine import auto_review

    seen = {}

    def fake_gateway(timeout=None):  # noqa: ANN001, ANN202
        seen["timeout"] = timeout
        return FakeGateway(
            '{"verdict":"pass","score":2,"observations":[{"document_id":1,"page_no":1,'
            '"observation":"x"}],"reasoning":"r"}')

    monkeypatch.setattr(auto_review, "get_vision_gateway", fake_gateway)
    res = vl_fallback.rescue(CRIT, PAGE_IMAGES)
    assert res.kind == "scored"
    assert seen["timeout"] == config.vl_timeout_s() == 90.0


def test_valid_cell_visual_fallback() -> None:
    from app.db.models import ScoreRecord
    from app.services.automation_service import _valid_cell

    def rec(final, visual=True):  # noqa: ANN001, ANN202
        return ScoreRecord(
            review_status="auto_confirmed", machine_status="pass",
            final_score=Decimal(final), ai_reasoning="按细则定分",
            ai_raw_json={"auto_review": {
                "state": "completed", "basis": "visual_fallback",
                "reasoning": "按细则定分",
                "visual_evidence": ([{"document_id": 1, "page_no": 1, "observation": "x"}]
                                    if visual else []),
            }},
        )

    crit = {"score_type": "count", "max_score": "6"}
    assert _valid_cell(rec("4"), crit, [])
    assert not _valid_cell(rec("8"), crit, []), "越界分拒收"
    assert not _valid_cell(rec("-1"), crit, []), "负分拒收"
    assert not _valid_cell(rec("4", visual=False), crit, []), "无视觉证据拒收"


# ---- run152 观测性补：错误路径留痕与统一日志 ---------------------------------

def test_vl_note_attaches_to_error() -> None:
    from app.services import automatic_scoring_service as asv

    err = RuntimeError("boom")
    out = asv._vl_note(None, {"visual_fallback": "failed", "reason": "vl_no_valid_score",
                              "reasoning": "模型：扫描件无法判定"}, err)
    assert out is None
    assert err.vl_fallback_note["reason"] == "vl_no_valid_score"
    assert err.vl_fallback_note["reasoning"] == "模型：扫描件无法判定"


def test_closure_fallback_records_vl_note() -> None:
    from app.db.models import ScoreRecord
    from app.llm.base import LLMError
    from app.services.automatic_scoring_service import _write_closure_fallback

    class _S:
        def add(self, x):  # noqa: ANN001, ANN202
            return None

    err = LLMError("需要视觉核验但未提供可读取原页图像")
    err.vl_fallback_note = {"visual_fallback": "failed", "reason": "vl_no_valid_score",
                            "reasoning": "模型：页图不清晰，无法核验金额", "clues": []}
    row = ScoreRecord()
    _write_closure_fallback(_S(), row, {"score_type": "count"}, err)
    assert any(w.get("verdict_source") == "visual" for w in row.warnings_json),         "错误路径 VL 失败原因必须进 warnings（run152 漏洞）"
    assert row.ai_raw_json["auto_review"]["visual_fallback_note"]["reason"] == "vl_no_valid_score"
    assert "模型：页图不清晰" in row.warnings_json[1]["reasoning"]


def test_rescue_failed_logs_unified(tmp_path, monkeypatch, caplog) -> None:  # noqa: ANN001
    import logging

    from app.services import automatic_scoring_service as asv
    from app.services.automatic_scoring_service import CellOutcome

    _png_env(tmp_path, monkeypatch)
    monkeypatch.setattr(vl_fallback, "rescue", lambda *a, **k: vl_fallback.VlResult(
        "failed", reason="vl_no_valid_score", observations=[{}],
        reasoning="模型给了 unverifiable"))
    fb = CellOutcome("concern", "暂定", None, [], [], "fallback", 1,
                     {"fallback": {"kind": "provisional_zero"}})
    pages = [{"document_id": 9, "page_no": 1, "stored_path": "x.pdf", "sha256": "s"}]
    with caplog.at_level(logging.WARNING, logger="app.services.automatic_scoring_service"):
        out, _ = asv._maybe_vl_rescue(1, 2, CRIT, fb, None,
                                      {2: (pages, "synthetic")}, vl_fallback.Budget())
    assert "VL 兜底未定分" in caplog.text and "vl_no_valid_score" in caplog.text
    note = out.raw["visual_fallback_note"]
    assert note["reasoning"] == "模型给了 unverifiable", "reasoning 必须落进留痕"
