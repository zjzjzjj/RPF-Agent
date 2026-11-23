# -*- coding: utf-8 -*-
"""材料审核视觉大模型兜底（2026-11-23 优化②）离线契约测试。

红线锁定：否决/资格/符合性格 VL 输出 fail 必须拒收（AI 不否决）；分数越界/
契约不合法/涉密拦截 → 维持暂定（天然安全态）；预算每格/每家上限。
"""
from __future__ import annotations

from decimal import Decimal

from app.engine import vl_fallback


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


SEGMENTS = [{"image_path": "/tmp/s.png", "image_sha256": "ab", "data_url": "data:image/png;base64,AA",
             "page_numbers": [1, 2, 3]}]

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


def test_rescue_scored_happy_path() -> None:
    gw = FakeGateway(
        '{"verdict":"pass","score":4,"observations":[{"document_id":1,"page_no":2,'
        '"observation":"第2页供货合同一份，金额80万"}],"reasoning":"2个案例各2分"}')
    res = vl_fallback.rescue(CRIT, SEGMENTS, gateway=gw)
    assert res.kind == "scored"
    assert res.score == Decimal("4")
    assert res.observations[0]["page_no"] == 2
    assert gw.calls[0]["images"] == ["data:image/png;base64,AA"]
    assert gw.calls[0]["data_class"] == "synthetic"


def test_rescue_verdict_fail_rejected() -> None:
    """红线：否决/资格/符合性格 VL 输出 fail 一律拒收（AI 不否决）。"""
    gw = FakeGateway(
        '{"verdict":"fail","score":null,"observations":[{"document_id":1,"page_no":1,'
        '"observation":"未见公章"}],"reasoning":"不符合","clues":["未见签章"]}')
    res = vl_fallback.rescue(VERDICT_CRIT, SEGMENTS, is_verdict=True, gateway=gw)
    assert res.kind == "failed"
    assert res.reason == "vl_verdict_rejected"
    assert res.clues == ["未见签章"], "线索保留供人工"


def test_rescue_verdict_clue_only() -> None:
    gw = FakeGateway(
        '{"verdict":"pass","score":null,"observations":[{"document_id":1,"page_no":1,'
        '"observation":"投标函上有红色印章痕迹"}],"reasoning":"已见签章"}')
    res = vl_fallback.rescue(VERDICT_CRIT, SEGMENTS, is_verdict=True, gateway=gw)
    assert res.kind == "clue_only"
    assert res.score is None


def test_rescue_score_out_of_range_rejected() -> None:
    for bad in ("8", "-1", "null"):
        gw = FakeGateway(
            f'{{"verdict":"pass","score":{bad},"observations":[{{"document_id":1,'
            '"page_no":1,"observation":"x"}],"reasoning":"r"}')
        res = vl_fallback.rescue(CRIT, SEGMENTS, gateway=gw)
        assert res.kind == "failed" and res.reason == "vl_no_valid_score", bad
    # 非数字 score → 契约校验拒收（同样维持暂定）
    gw = FakeGateway('{"verdict":"pass","score":"many","observations":[{"document_id":1,'
                     '"page_no":1,"observation":"x"}],"reasoning":"r"}')
    res = vl_fallback.rescue(CRIT, SEGMENTS, gateway=gw)
    assert res.kind == "failed" and res.reason.startswith("vl_contract_invalid")


def test_rescue_contract_invalid() -> None:
    gw = FakeGateway('{"verdict":"pass"}')  # 无 observations
    res = vl_fallback.rescue(CRIT, SEGMENTS, gateway=gw)
    assert res.kind == "failed" and res.reason == "vl_no_observations"
    gw2 = FakeGateway('随便说点什么')
    res2 = vl_fallback.rescue(CRIT, SEGMENTS, gateway=gw2)
    assert res2.kind == "failed" and res2.reason.startswith("vl_contract_invalid")


def test_rescue_gateway_error_maintains_fallback() -> None:
    # 涉密闸门拦截（LLMError）/网络故障 → failed，调用方维持暂定
    gw = FakeGateway(exc=RuntimeError("data_class confidential 被拦"))
    res = vl_fallback.rescue(CRIT, SEGMENTS, gateway=gw)
    assert res.kind == "failed" and res.reason.startswith("vl_call_failed")


def test_rescue_disabled_switch() -> None:
    from app import config
    import os

    os.environ["BID_EVAL_VL_FALLBACK"] = "0"
    try:
        res = vl_fallback.rescue(CRIT, SEGMENTS, gateway=FakeGateway("{}"))
        assert res.kind == "failed" and res.reason == "vl_fallback_disabled"
    finally:
        os.environ.pop("BID_EVAL_VL_FALLBACK", None)


# ---- AS 接线：触发集与 outcome 形状 -------------------------------------------------

def test_maybe_vl_rescue_triggers_only_on_fallback(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    from app.services import automatic_scoring_service as asv
    from app.services.automatic_scoring_service import CellOutcome

    normal = CellOutcome("pass", "ok", Decimal("3"), [], [], "deterministic")
    out, err = asv._maybe_vl_rescue(1, 2, CRIT, normal, None, {}, vl_fallback.Budget())
    assert out is normal and err is None

    fb = CellOutcome("concern", "暂定", None, [], [], "fallback", 1,
                     {"fallback": {"kind": "provisional_zero"}})
    monkeypatch.setattr(asv, "_vl_note", lambda outcome, note: outcome)
    called = {}

    def fake_rescue(criterion, segments, **kw):  # noqa: ANN001, ANN202
        called["yes"] = True
        return vl_fallback.VlResult("failed", reason="nope")

    monkeypatch.setattr(vl_fallback, "rescue", fake_rescue)
    monkeypatch.setattr("app.evidence.renderer.render_long_image",
                        lambda *a, **k: SEGMENTS)
    pages = [{"document_id": 9, "page_no": 1, "stored_path": "x.pdf", "sha256": "s"}]
    out, err = asv._maybe_vl_rescue(1, 2, CRIT, fb, None,
                                   {2: (pages, "synthetic")}, vl_fallback.Budget())
    assert called.get("yes"), "fallback 族 outcome 必须触发 VL 尝试"
    assert out is fb and err is None


def test_maybe_vl_rescue_scored_outcome_shape(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    from app.services import automatic_scoring_service as asv
    from app.services.automatic_scoring_service import CellOutcome

    fb = CellOutcome("concern", "暂定", None, [], [], "fallback", 1,
                     {"fallback": {"kind": "value_extraction_unresolved"}})
    monkeypatch.setattr("app.evidence.renderer.render_long_image",
                        lambda *a, **k: SEGMENTS)
    monkeypatch.setattr(vl_fallback, "rescue", lambda *a, **k: vl_fallback.VlResult(
        "scored", score=Decimal("4"),
        observations=[{"document_id": 9, "page_no": 1, "observation": "合同一份"}],
        reasoning="按细则 2 案例"))
    pages = [{"document_id": 9, "page_no": 1, "stored_path": "x.pdf", "sha256": "s"}]
    out, err = asv._maybe_vl_rescue(1, 2, CRIT, fb, None,
                                   {2: (pages, "synthetic")}, vl_fallback.Budget())
    assert err is None
    assert out.basis == "visual_fallback"
    assert out.score == Decimal("4")
    assert out.evidence and out.evidence[0]["kind"] == "visual"
    assert out.pages[0].get("image_url", "").startswith("data:image/png;base64,")
    assert out.raw["visual_fallback"]["score"] == "4"


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
