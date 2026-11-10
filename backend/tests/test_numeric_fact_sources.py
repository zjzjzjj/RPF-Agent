"""Scalar reproduction must use the right field, unit and exact amount."""
from decimal import Decimal

import pytest

from app.engine.auto_review import AutoReviewDecision, _numeric_values, _validate_fact_sources
from app.llm.base import LLMError


@pytest.mark.parametrize("text,field,expected", [
    ("承诺响应时间2小时，提供3年质保。", "response_hours", {"2"}),
    ("承诺响应时间2小时，提供3年质保。", "warranty_months", {"36"}),
    ("近三年类似业绩，质保期未作承诺。", "warranty_months", set()),
    ("售后响应时间为三十分钟，保修期为二十四个月。", "response_hours", {"0.5"}),
    ("售后响应时间为三十分钟，保修期为二十四个月。", "warranty_months", {"24"}),
    ("质保期按日计为365天。", "warranty_months", set()),
    ("响应承诺为1天内到场。", "response_hours", {"24"}),
    ("质保期\n24个月", "warranty_months", {"24"}),
    ("投标总价120万元，投标保证金3万元，税率6%。", "bid_price", {"1200000"}),
    ("投标总价120万元（其中保证金3万元），税率6%。", "bid_price", {"1200000"}),
    ("投标保证金人民币30000元，年度报价10万元。", "bid_price", set()),
    ("投标总价人民币壹佰贰拾万伍仟元陆角柒分。", "bid_price", {"1205000.67"}),
    ("投标总价为1,250,000.00元，税率13%。", "bid_price", {"1250000"}),
    ("投标总价（万元）120", "bid_price", {"1200000"}),
    ("注册资本500万", "registered_capital", {"5000000"}),
    ("注册资本为人民币500万元，投标总价120万元。", "registered_capital", {"5000000"}),
    ("投标报价200万元；分项合计195万元。", "price_items_total", {"1950000"}),
    ("第一年度报价120万元，第二年度报价150万元。", "yearly_bid_price_1", {"1200000"}),
    ("第一年度报价120万元，第二年度报价150万元。", "yearly_bid_price_2", {"1500000"}),
    ("投标总价4017400元，第一年度报价未提供。", "yearly_bid_price_1", set()),
    ("投标总价4017400元，第一年度报价120万元。", "bid_price", {"4017400"}),
])
def test_scalar_values_are_reproduced_from_their_own_units_and_labels(text, field, expected):
    assert _numeric_values(text, field) == {Decimal(value) for value in expected}


def test_warranty_cannot_use_nearby_response_hours_as_months():
    quote = "承诺响应时间2小时，提供3年质保，售后服务措施详列如下。"
    reference = {"document_id": 1, "page_no": 1, "quote": quote}
    result = AutoReviewDecision(verdict="pass", reasoning="已核验售后承诺", evidence=[{**reference, "kind": "text"}],
                                facts={"warranty_months": "2", "fact_evidence": {"warranty_months": [reference]}})
    with pytest.raises(LLMError, match="复算"):
        _validate_fact_sources(result)
    result.facts["warranty_months"] = "36"
    _validate_fact_sources(result)


def test_annual_price_cannot_borrow_another_years_amount():
    quote = "第一年度报价120万元，第二年度报价150万元，报价均包含税费。"
    reference = {"document_id": 1, "page_no": 1, "quote": quote}
    result = AutoReviewDecision(verdict="pass", reasoning="核对年度报价", evidence=[{**reference, "kind": "text"}],
                                facts={"yearly_bid_price_1": "1500000", "fact_evidence": {"yearly_bid_price_1": [reference]}})
    with pytest.raises(LLMError, match="复算"):
        _validate_fact_sources(result)
    result.facts["yearly_bid_price_1"] = "1200000"
    _validate_fact_sources(result)
