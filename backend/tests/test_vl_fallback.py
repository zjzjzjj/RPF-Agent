# -*- coding: utf-8 -*-
"""材料审核视觉大模型兜底（2026-11-23 优化②）离线契约测试。

红线锁定：否决/资格/符合性格 VL 输出 fail 必须拒收（AI 不否决）；分数越界/
契约不合法/涉密拦截 → 维持暂定（天然安全态）；预算每格/每家上限。
run 151 后契约：一页一图多图单请求（≤max_images 页图）；拒收类错误不重试；
兜底在 worker 线程并行执行。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

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

CRIT = {"name": "供货业绩", "description": "每提供1个案例得2分，最高6分；须提供业绩证明及近六个月社保缴纳证明", "max_score": "6",
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


def test_rescue_facts_happy_path_page_images() -> None:
    """VL 报事实、代码算分（2026-11-24）：计分格输出面=facts，禁止给分。"""
    gw = FakeGateway(
        '{"verdict":"pass","facts":[{"kind":"contract","name":"供货合同A",'
        '"amount_yuan":"800000","sign_date":"2024-03-01","has_contract":true,'
        '"has_invoice":null,"has_acceptance":null,'
        '"evidence":[{"document_id":1,"page_no":2,"observation":"第2页供货合同一份，金额80万"}]}],'
        '"observations":[{"document_id":1,"page_no":2,"observation":"第2页供货合同"}],'
        '"reasoning":"见合同原件"}')
    res = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw)
    assert res.kind == "facts"
    assert res.facts and res.facts[0]["amount_yuan"] == "800000"
    assert res.facts[0]["has_invoice"] is None
    assert not hasattr(res, "score") or getattr(res, "score", None) is None, "模型不再定分"
    assert gw.calls[0]["images"] == [p["data_url"] for p in PAGE_IMAGES]
    # 页码对照必须进提示词（VL 看到的是页图，页码靠映射告知）
    assert "第1张图=文档1第1页" in gw.calls[0]["user"]
    assert gw.calls[0]["data_class"] == "synthetic"


def test_rescue_accepts_null_appraisal_flag_for_contract_and_person_facts():
    gateway = FakeGateway(
        '{"verdict":"pass","facts":['
        '{"kind":"contract","name":"运维合同A","amount_yuan":"800000","appraisal_is_lookup":null,'
        '"evidence":[{"document_id":1,"page_no":1,"observation":"原页合同金额80万元"}]},'
        '{"kind":"person","name":"张三","appraisal_is_lookup":null,'
        '"evidence":[{"document_id":1,"page_no":1,"observation":"张三，驻场工程师"}]}]}')
    result = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gateway)
    assert result.kind == "facts"
    assert [fact["kind"] for fact in result.facts] == ["contract", "person"]


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


def test_rescue_facts_sanitize_rejects_bad_cases() -> None:
    """自洽性拒收——拒案例不拒整格（2026-11-24）。"""
    # 金额非法的 contract 案例丢弃；全部被丢 → vl_no_facts
    gw = FakeGateway(
        '{"verdict":"pass","facts":[{"kind":"contract","name":"A","amount_yuan":"many",'
        '"evidence":[{"document_id":1,"page_no":1,"observation":"x"}]}],"reasoning":"r"}')
    res = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw)
    assert res.kind == "failed" and res.reason == "vl_no_facts"
    # 矛盾回退：观察说"未见发票"却 has_invoice=true → 回退 null（案例保留）
    gw2 = FakeGateway(
        '{"verdict":"pass","facts":[{"kind":"contract","name":"A","amount_yuan":"800000",'
        '"has_invoice":true,'
        '"evidence":[{"document_id":1,"page_no":1,"observation":"合同一份，未见发票"}]}],'
        '"reasoning":"r"}')
    res2 = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw2)
    assert res2.kind == "facts"
    assert res2.facts[0]["has_invoice"] is None, "矛盾回退 null"
    # 证据页越界且清空 → 丢案
    gw3 = FakeGateway(
        '{"verdict":"pass","facts":[{"kind":"contract","name":"A","amount_yuan":"800000",'
        '"evidence":[{"document_id":99,"page_no":999,"observation":"x"}]}],"reasoning":"r"}')
    res3 = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw3)
    assert res3.kind == "failed" and res3.reason == "vl_no_facts"
    # false 归一 None（未见≠没有，防 VL 误杀）
    gw4 = FakeGateway(
        '{"verdict":"pass","facts":[{"kind":"contract","name":"A","amount_yuan":"800000",'
        '"has_acceptance":false,'
        '"evidence":[{"document_id":1,"page_no":1,"observation":"合同"}]}],"reasoning":"r"}')
    res4 = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw4)
    assert res4.facts[0]["has_acceptance"] is None


def test_rescue_contract_invalid() -> None:
    gw = FakeGateway('{"verdict":"pass"}')  # 计分格无 facts
    res = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw)
    assert res.kind == "failed" and res.reason == "vl_no_facts"
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


def _perf_criterion() -> dict:
    return {
        "code": "c25", "name": "投标产品业绩", "score_type": "count", "max_score": "2",
        "description": ("投标产品业绩：同类产品业绩，合同金额100万及以上，每个得1分，"
                        "最高得2分。【证明材料】须提供业绩合同、验收证明材料及合同配套的"
                        "至少一张发票复印件并加盖投标人公章，以上资料未提供的不得分。"),
        "params": {"field": "performance", "per_item": "1",
                   "min_amount_yuan": "1000000"},
    }


def _empty_inv():
    from app.engine.facts import BidFacts
    from app.engine.material_inventory import MaterialInventory

    return MaterialInventory(bidder_id=2, pages=[], full_text="", facts=BidFacts(text=""),
                             contracts=[], performance_section_found=True)


def _fact_case(name: str, amount: str, page: int) -> dict:
    return {"kind": "contract", "name": name, "amount_yuan": amount,
            "has_contract": True, "has_invoice": True, "has_acceptance": True,
            "evidence": [{"document_id": 9, "page_no": page, "observation": "三件套齐"}]}


def test_maybe_vl_rescue_facts_rescore_outcome(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """VL 报事实、代码算分：facts → 派生 inventory → 确定性算分定格。"""
    from app.services import automatic_scoring_service as asv
    from app.services.automatic_scoring_service import CellOutcome

    _png_env(tmp_path, monkeypatch)
    monkeypatch.setattr(vl_fallback, "rescue", lambda *a, **k: vl_fallback.VlResult(
        "facts", facts=[_fact_case("运维项目A", "1200000", 1)],
        observations=[], reasoning="图上见三件套"))
    fb = CellOutcome("concern", "未核实", Decimal("0"), [], [], "fallback", 1,
                     {"fallback": {"kind": "material_unverified", "value": Decimal("0"),
                                   "unverified_records": [], "material_pages": []}})
    pages = [{"document_id": 9, "page_no": 1, "stored_path": "x.pdf", "sha256": "s"}]
    out, err = asv._maybe_vl_rescue(1, 2, _perf_criterion(), fb, None,
                                   {2: (pages, "synthetic")}, vl_fallback.Budget(),
                                   inventory_factory=lambda bid: _empty_inv())
    assert err is None
    assert out.basis == "visual_fallback"
    assert out.score == Decimal("1.00"), "1 个过闸案例 × 1 分（代码算分，非模型给分）"
    assert out.evidence and out.evidence[0]["kind"] == "visual"
    assert out.pages[0]["image_url"].startswith("data:image/png;base64,"), "页级 image_url 已就位"
    assert out.raw["visual_fallback"]["facts"], "事实留痕"


def test_onsite_case_annexes_rank_before_unrelated_pages():
    from app.services.automatic_scoring_service import _material_recovery_pages

    criterion = {"name": "两名驻场运维工程师实施或服务经验", "score_type": "count",
                 "params": {"field": "performance", "person_scope": "member"}}
    originals = {
        214: "2.10.2 项目团队人员情况表",
        253: "2.10.2.4.3 卢晓军的社保证明\n社会保险参保证明",
        254: "2.10.2.4.4 吴家隆的社保证明\n社会保险参保证明",
        255: "2.10.2.5 两名项目驻场运维工程师的类似项目实施/服务经验证明\n"
             "两名项目驻场运维工程师卢晓军和吴家隆，曾参加两个项目。\n合同一",
        258: "工作职能 项目人员 参与项目阶段\n业务顾问 卢晓军\n开发工程师 吴家隆",
        262: "2.10.2.5.2 两名项目驻场运维工程师的类似项目实施/服务经验证明2\n合同二",
        267: "附件2 姓名 部门 项目中任职\n卢晓军 项目实施顾问\n吴家隆 项目技术人员",
        268: "2.10.3 到位率承诺",
        280: "工作职能 参与项目阶段 卢晓军 吴家隆",
    }
    pages = [{"document_id": 69, "page_no": number, "text": content} for number, content in originals.items()]
    ranked = [page["page_no"] for page in _material_recovery_pages(
        criterion, pages, {"kind": "material_review_pending"}, None)]
    assert {255, 258, 262, 267}.issubset(ranked[:7])
    assert ranked.index(280) > ranked.index(267)


def test_material_review_recovers_proof_across_distinct_batches(tmp_path, monkeypatch):
    from app.services import automatic_scoring_service as scoring

    _png_env(tmp_path, monkeypatch)
    monkeypatch.setenv("BID_EVAL_SCORING_OCR", "0")
    monkeypatch.setenv("BID_EVAL_VL_FALLBACK_MAX_IMAGES", "1")
    criterion = {**_perf_criterion(), "max_score": "1"}
    pages = [{"document_id": 9, "page_no": page, "stored_path": "x.pdf", "sha256": "s"}
             for page in (1, 2)]
    pending = scoring._material_review_pending(criterion, pages, "发票和合同须分别核验")
    pending.raw["fallback"]["candidate_pages"] = [
        {"document_id": 9, "page_no": 2}, {"document_id": 9, "page_no": 2}]
    reviewed = []

    def rescue(rule, images, **kwargs):
        page = images[0]["page_no"]
        reviewed.append(page)
        fact = _fact_case("运维项目A", "1200000", page)
        if page == 2:
            fact.update(amount_yuan=None, has_contract=None, has_acceptance=None)
        else:
            fact.update(has_invoice=None)
        return vl_fallback.VlResult("facts", facts=[fact])

    monkeypatch.setattr(vl_fallback, "rescue", rescue)
    result, error = scoring._maybe_vl_rescue(1, 2, criterion, pending, None,
                                            {2: (pages, "synthetic")}, vl_fallback.Budget(),
                                            inventory_factory=lambda _: _empty_inv())
    assert error is None
    assert reviewed == [2, 1]
    assert result.score == Decimal("1")
    assert {(e["document_id"], e["page_no"]) for e in result.evidence} == {(9, 1), (9, 2)}


def test_visual_proof_requires_correct_document_and_accepts_amountless_fragment():
    pages = [{"document_id": 1, "page_no": 7}, {"document_id": 2, "page_no": 7}]

    def make_fact(doc):
        return vl_fallback.VlFact(
            kind="contract", name="运维项目A", has_invoice=True,
            evidence=[{"document_id": doc, "page_no": 7, "observation": "发票备注列示项目名称"}])

    facts, dropped = vl_fallback._sanitize_facts([make_fact(9), make_fact(None), make_fact(1)], pages)
    assert dropped == 2
    assert facts[0]["amount_yuan"] is None
    assert facts[0]["evidence"][0]["document_id"] == 1


@pytest.mark.parametrize("source_changed", [False, True])
def test_material_resume_reuses_evidence_and_moves_to_unread_pages(tmp_path, monkeypatch, source_changed):
    from app.services import automatic_scoring_service as scoring

    _png_env(tmp_path, monkeypatch)
    monkeypatch.setenv("BID_EVAL_VL_FALLBACK_MAX_IMAGES", "1")
    monkeypatch.setenv("BID_EVAL_VL_FALLBACK_MAX_PER_CELL", "1")
    criterion = {**_perf_criterion(), "max_score": "1"}
    pages = [{"document_id": 9, "page_no": page, "stored_path": "x.pdf", "sha256": "s"}
             for page in (1, 2)]
    reviewed = []

    def rescue(rule, images, **kwargs):
        page = images[0]["page_no"]
        reviewed.append(page)
        fact = _fact_case("运维项目A", "1200000", page)
        if page == 1:
            fact["has_invoice"] = None
        else:
            fact.update(amount_yuan=None, has_contract=None, has_acceptance=None)
        return vl_fallback.VlResult("facts", facts=[fact])

    def review(checkpoint=None):
        pending = scoring._material_review_pending(criterion, pages, "须分别核验发票及合同")
        return scoring._maybe_vl_rescue(1, 2, criterion, pending, None, {2: (pages, "synthetic")},
                                        vl_fallback.Budget(), inventory_factory=lambda _: _empty_inv(),
                                        checkpoint=checkpoint)[0]

    monkeypatch.setattr(vl_fallback, "rescue", rescue)
    initial = review()
    assert initial.score is None and reviewed == [1]
    previous_image = initial.pages[0]
    checkpoint = {"visual_fallback_note": initial.raw["visual_fallback_note"],
                  "visual_evidence": [{**initial.evidence[0], "sha256": previous_image["sha256"],
                                       "image_sha256": previous_image["image_sha256"]}]}
    if source_changed:
        pages[0]["sha256"] = "changed"
    result = review(checkpoint)
    if source_changed:
        assert result.score is None and reviewed == [1, 1]
    else:
        assert result.score == 1 and reviewed == [1, 2]
        assert {entry["page_no"] for entry in result.evidence} == {1, 2}


def test_resumed_bounded_material_review_closes_verified_zero(tmp_path, monkeypatch):
    from app.services import automatic_scoring_service as scoring

    _png_env(tmp_path, monkeypatch)
    criterion = {**_perf_criterion(), "max_score": "1"}
    pages = [{"document_id": 9, "page_no": page, "stored_path": "x.pdf", "sha256": "s"}
             for page in range(1, 25)]
    checkpoint = {
        "visual_fallback_note": {
            "reviewed_pages": [{"document_id": 9, "page_no": page, "sha256": "s"}
                               for page in range(1, 25)],
            "facts": [],
        },
        "visual_evidence": [],
    }

    def unexpected(*args, **kwargs):
        raise AssertionError("completed checkpoint must not call vision again")

    monkeypatch.setattr(vl_fallback, "rescue", unexpected)
    pending = scoring._material_review_pending(criterion, pages, "核对业绩")
    result, error = scoring._maybe_vl_rescue(
        1, 2, criterion, pending, None, {2: (pages, "synthetic")}, vl_fallback.Budget(),
        inventory_factory=lambda _: _empty_inv(), checkpoint=checkpoint,
    )
    assert error is None
    assert result.score == 0
    assert result.verdict == "fail"
    assert result.basis == "visual_fallback"

def test_material_resume_rechecks_omitted_certificate_page_once(tmp_path, monkeypatch):
    from app.services import automatic_scoring_service as scoring

    _png_env(tmp_path, monkeypatch)
    monkeypatch.setenv("BID_EVAL_VL_FALLBACK_MAX_IMAGES", "1")
    monkeypatch.setenv("BID_EVAL_VL_FALLBACK_MAX_PER_CELL", "1")
    criterion = {**CRIT, "params": {"field": "cmmi"}, "max_score": "2"}
    pages = [{"document_id": 9, "page_no": page, "stored_path": "x.pdf", "sha256": "s"}
             for page in (1, 2)]
    requests = []

    def rescue(rule, images, **kwargs):
        requests.append((images[0]["page_no"], rule.get("_recheck_certificate_pages")))
        return vl_fallback.VlResult("failed", reason="vl_no_facts")

    def review(checkpoint=None):
        pending = scoring._material_review_pending(criterion, pages, "核对证书有效期")
        result, _ = scoring._maybe_vl_rescue(1, 2, criterion, pending, None,
                                            {2: (pages, "synthetic")}, vl_fallback.Budget(),
                                            checkpoint=checkpoint)
        return {"visual_fallback_note": result.raw["visual_fallback_note"]}

    monkeypatch.setattr(vl_fallback, "rescue", rescue)
    first = review()
    second = review(first)
    review(second)
    assert requests == [(1, None), (1, [(9, 1)]), (2, None)]


def test_confirmed_missing_platform_query_finalizes_zero(tmp_path, monkeypatch):
    from app.services import automatic_scoring_service as scoring

    _png_env(tmp_path, monkeypatch)
    criterion = {
        "code": "c44", "name": "管理体系认证", "score_type": "count", "max_score": "3",
        "description": ("ISO9001、ISO27001、ISO20000 每项1分；须提供有效证书复印件并加盖公章，"
                        "并提供全国认证认可信息公共服务平台 cx.cnca.cn 查询结果截图。"),
        "required_evidence": "证书复印件及 cx.cnca.cn 查询结果截图",
        "params": {"field": "certificates", "per_item": "1", "max_items": "3"},
    }
    pages = [{"document_id": 9, "page_no": 1, "stored_path": "x.pdf", "sha256": "s"}]
    fact = {
        "kind": "certificate", "name": "ISO9001质量管理体系认证证书",
        "has_cert_proof": True, "has_seal": True, "has_query_screenshot": None,
        "certificate_number": None, "appraisal_is_lookup": None,
        "evidence": [{"document_id": 9, "page_no": 1,
                      "observation": "质量管理体系认证证书复印件，加盖红色公章"}],
    }
    monkeypatch.setattr(vl_fallback, "rescue", lambda *a, **k: vl_fallback.VlResult(
        "facts", facts=[fact],
        observations=[{"document_id": 9, "page_no": 1,
                       "observation": "认证章节复核完毕，未见认证认可信息公共服务平台查询结果截图"}],
        reasoning="证书已核验，查询截图缺失"))
    pending = scoring._material_review_pending(criterion, pages, "补核查询结果截图")
    result, error = scoring._maybe_vl_rescue(
        1, 2, criterion, pending, None, {2: (pages, "synthetic")}, vl_fallback.Budget(),
        inventory_factory=lambda _: _empty_inv(),
    )
    assert error is None
    assert result.score == 0
    assert result.verdict == "fail"
    assert result.basis == "visual_fallback"

def test_material_resume_rescores_authenticated_checkpoint_before_calling_vision(tmp_path, monkeypatch):
    from app.services import automatic_scoring_service as scoring

    _png_env(tmp_path, monkeypatch)
    criterion = {**_perf_criterion(), "max_score": "1"}
    pages = [{"document_id": 9, "page_no": 1, "stored_path": "x.pdf", "sha256": "s"}]
    inputs = {2: (pages, "synthetic")}
    calls = []

    def rescue(*args, **kwargs):
        calls.append(True)
        return vl_fallback.VlResult("facts", facts=[_fact_case("运维项目A", "1200000", 1)])

    monkeypatch.setattr(vl_fallback, "rescue", rescue)
    first, _ = scoring._maybe_vl_rescue(
        1, 2, criterion, scoring._material_review_pending(criterion, pages, "核对业绩"),
        None, inputs, vl_fallback.Budget(),
    )
    source = first.pages[0]
    checkpoint = {"visual_fallback_note": first.raw["visual_fallback_note"],
                  "visual_evidence": [{**first.evidence[0], "sha256": source["sha256"],
                                       "image_sha256": source["image_sha256"]}]}
    result, _ = scoring._maybe_vl_rescue(
        1, 2, criterion, scoring._material_review_pending(criterion, pages, "核对业绩"),
        None, inputs, vl_fallback.Budget(), inventory_factory=lambda _: _empty_inv(), checkpoint=checkpoint,
    )
    assert result.score == Decimal(1)
    assert result.raw["visual_fallback"]["reason"] == "checkpoint_rescored"
    assert len(calls) == 1


def test_vision_gateway_timeout_passed(monkeypatch) -> None:  # noqa: ANN001
    """VL 网关必须带 vl_timeout_s 超时（默认 90s），不陪跑 llm_timeout_s。"""
    from app import config
    from app.engine import auto_review

    seen = {}

    def fake_gateway(timeout=None):  # noqa: ANN001, ANN202
        seen["timeout"] = timeout
        return FakeGateway(
            '{"verdict":"pass","facts":[{"kind":"contract","name":"A","amount_yuan":"800000",'
            '"has_contract":true,'
            '"evidence":[{"document_id":1,"page_no":1,"observation":"x"}]}],"reasoning":"r"}')

    monkeypatch.setattr(auto_review, "get_vision_gateway", fake_gateway)
    res = vl_fallback.rescue(CRIT, PAGE_IMAGES)
    assert res.kind == "facts"
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


def test_maybe_vl_rescue_material_pages_first(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """run153 实证：候选页必须材料页优先——封面/索引/目录噪声垫底。

    送审窗口选到封面+目录（索引中毒），模型反复"未见材料"弃权；
    解析期 material_tags 命中页排最前，_SUPPLEMENT_HINTS 关键词页次之，
    噪声页（toc/目录）最后；同档内保持页序。
    """
    from app.services import automatic_scoring_service as asv
    from app.services.automatic_scoring_service import CellOutcome

    _png_env(tmp_path, monkeypatch)
    captured = {}

    def fake_rescue(criterion, page_images, **kw):  # noqa: ANN001, ANN202
        captured["pages"] = page_images
        return vl_fallback.VlResult("failed", reason="probe")

    monkeypatch.setattr(vl_fallback, "rescue", fake_rescue)
    fb = CellOutcome("concern", "暂定", None, [], [], "fallback", 1,
                     {"fallback": {"kind": "provisional_zero"}})

    def page(n, tags=None, text=""):  # noqa: ANN001, ANN202
        return {"document_id": 9, "page_no": n, "stored_path": "x.pdf",
                "sha256": "s", "material_tags": tags or [], "text": text}

    pages = [
        page(1, tags=["目录", "toc"], text="目 录 ……"),
        page(2, tags=["营业执照"], text="营业执照"),
        page(3, text="封面 资格文件"),                       # 无标签无锚词
        page(4, text="投标人自2022年以来业绩合同一览…社保缴纳证明"),
        page(5, tags=["社保证明"], text="社会保险缴纳证明"),
        page(6, tags=["分隔页"], text="—"),
    ]
    out, _ = asv._maybe_vl_rescue(1, 2, CRIT, fb, None,
                                 {2: (pages, "synthetic")}, vl_fallback.Budget())
    assert out is fb
    order = [p["page_no"] for p in captured["pages"]]
    # CRIT=供货业绩（锚词含 业绩/案例/证明）：
    # 锚词命中倒序：4（业绩+社保+证明=3击）→ 5（社保证明=2击）→ 2/3（0击，
    # 有标签的2先于无标签3）→ 噪声(1,6)垫底。run154 实证：仅按页序会让
    # 文档最前的资格页占满名额，p200 的业绩页进不来。
    assert order[:2] == [4, 5], f"该格锚词命中页应最前：{order}"
    assert order[2:4] == [2, 3], f"零命中时有标签先于无标签：{order}"
    assert order[4:] == [1, 6], f"噪声页垫底且保页序：{order}"


# ---- 2026-11-24 契约加固 + 严口径翻案（run158 放水修正）-----------------------

def test_extract_json_lenient_fenced_and_trailing_comma() -> None:
    """JSON 容错：围栏/尾逗号/前后杂文不再整格白放弃（run155 c45 JSONDecodeError）。"""
    data = vl_fallback._extract_json(
        '结论如下：\n```json\n{"verdict":"pass","score":2,}\n```\n以上。')
    assert data["verdict"] == "pass"
    assert data["score"] == 2


def test_rescue_contract_invalid_retries_once_then_gives_up() -> None:
    """契约解析失败重试一次完整调用（同预算名额内，不额外扣预算）；仍败才放弃。"""
    gw = FakeGateway('完全不是 JSON')
    res = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw)
    assert res.kind == "failed" and res.reason.startswith("vl_contract_invalid")
    assert len(gw.calls) == 2, "契约失败只重试一次"


def test_rescue_contract_retry_recovers() -> None:
    """首次输出坏 JSON、重试输出好 JSON → 事实收取（run155 c45 型恢复）。"""

    class SeqGateway:
        def __init__(self, contents):  # noqa: ANN001, ANN202
            self._contents = list(contents)
            self.calls: list[dict] = []

        def chat(self, system, user, *, images=None, data_class="synthetic", **kw):  # noqa: ANN001, ANN202
            self.calls.append({})
            return FakeResult(self._contents.pop(0))

    good = ('{"verdict":"pass","facts":[{"kind":"contract","name":"B","amount_yuan":"300000",'
            '"has_contract":true,'
            '"evidence":[{"document_id":1,"page_no":1,"observation":"发票一份"}]}],'
            '"reasoning":"三件套齐"}')
    gw = SeqGateway(['坏掉的输出{', good])
    res = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw)
    assert res.kind == "facts" and res.facts[0]["amount_yuan"] == "300000"
    assert len(gw.calls) == 2


def test_maybe_vl_rescue_facts_never_downgrades(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """翻案只升不降（2026-11-24）：VL 事实重算 ≤ 基准 → 维持原 fallback 格；
    高于基准才升级 visual_fallback（"误否决不可接受"镜像）。"""
    from app.services import automatic_scoring_service as asv
    from app.services.automatic_scoring_service import CellOutcome

    _png_env(tmp_path, monkeypatch)
    criterion = dict(_perf_criterion(), max_score="6")
    pages = [{"document_id": 9, "page_no": 1, "stored_path": "x.pdf", "sha256": "s"}]

    def _fb():
        return CellOutcome("concern", "未核实", Decimal("2"), [], [], "fallback", 1,
                           {"fallback": {"kind": "material_unverified",
                                         "value": Decimal("2")}})

    # 事实重算 1 < 基准 2 → 不下修、不换格
    monkeypatch.setattr(vl_fallback, "rescue", lambda *a, **k: vl_fallback.VlResult(
        "facts", facts=[_fact_case("运维项目A", "1200000", 1)], reasoning="只见 1 例"))
    out, err = asv._maybe_vl_rescue(1, 2, criterion, _fb(), None,
                                    {2: (pages, "synthetic")}, vl_fallback.Budget(),
                                    inventory_factory=lambda bid: _empty_inv())
    assert err is None
    assert out.score == Decimal("2"), "VL 事实重算不得下修基准分（只升不降）"
    assert out.raw.get("visual_fallback_note", {}).get("visual_fallback") == "facts_no_improvement"
    # 事实重算 3 > 基准 2 → 升级翻案
    monkeypatch.setattr(vl_fallback, "rescue", lambda *a, **k: vl_fallback.VlResult(
        "facts", facts=[_fact_case(f"运维项目{c}", a, i + 1)
                        for i, (c, a) in enumerate(
                            [("A", "1200000"), ("B", "1300000"), ("C", "1400000")])],
        reasoning="翻案：3 例三件套齐"))
    out2, _ = asv._maybe_vl_rescue(1, 2, criterion, _fb(), None,
                                   {2: (pages, "synthetic")}, vl_fallback.Budget(),
                                   inventory_factory=lambda bid: _empty_inv())
    assert out2.basis == "visual_fallback"
    assert out2.score == Decimal("3.00"), "VL 事实重算更高则翻案计入"


# ---- 2026-11-25 翻案通道收口：person 归属/材料三态契约 -----------------------

def test_facts_person_keys_and_sanitize() -> None:
    # person_names + 人员材料三态解析；false 归一 None
    gw = FakeGateway(
        '{"verdict":"pass","facts":[{"kind":"contract","name":"A","amount_yuan":"800000",'
        '"person_names":["张三"],"has_work_proof":true,"has_social_security":false,'
        '"evidence":[{"document_id":1,"page_no":1,"observation":"业绩证明载明张三"}]}],'
        '"reasoning":"r"}')
    res = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw)
    assert res.kind == "facts"
    f = res.facts[0]
    assert f["person_names"] == ["张三"]
    assert f["has_work_proof"] is True
    assert f["has_social_security"] is None, "false 归一 None（防 VL 误杀）"
    # ②' 归属自洽：person_names 空 → has_work_proof=true 回退 null
    gw2 = FakeGateway(
        '{"verdict":"pass","facts":[{"kind":"contract","name":"B","amount_yuan":"800000",'
        '"person_names":[],"has_work_proof":true,'
        '"evidence":[{"document_id":1,"page_no":1,"observation":"x"}]}],"reasoning":"r"}')
    res2 = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw2)
    assert res2.facts[0]["has_work_proof"] is None, "不知为谁开的证明不算归属证据"
    # ② 矛盾回退：观察"未见社保"却 has_social_security=true
    gw3 = FakeGateway(
        '{"verdict":"pass","facts":[{"kind":"contract","name":"C","amount_yuan":"800000",'
        '"has_social_security":true,'
        '"evidence":[{"document_id":1,"page_no":1,"observation":"合同一份，未见社保"}]}],'
        '"reasoning":"r"}')
    res3 = vl_fallback.rescue(CRIT, PAGE_IMAGES, gateway=gw3)
    assert res3.facts[0]["has_social_security"] is None


def test_prompt_owner_proof_red_line() -> None:
    """prompt 必须写死 work_proof 红线与新键（业主证明≠业绩证明）。"""
    assert "person_names" in vl_fallback.SYSTEM_PROMPT
    assert "has_work_proof" in vl_fallback.SYSTEM_PROMPT
    assert "业主证明" in vl_fallback.SYSTEM_PROMPT and "不算" in vl_fallback.SYSTEM_PROMPT


def test_vl_budget_default_30_per_bidder() -> None:
    from app import config

    assert config.vl_fallback_max_per_bidder() == 30
