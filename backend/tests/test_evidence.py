"""证据定位与核验单测：三级回退、bbox、造假引文识别。"""
from __future__ import annotations

import pytest

from app.evidence.locator import DocumentSource, locate
from app.parsing.normalize import normalize

from .conftest import upload_pdf, wait_job

pytestmark = pytest.mark.usefixtures("app_env")

# 引文与页码取自真实生成的样例招标文件（见 generate_sample_tender.py）
REAL_QUOTE = "投标人须具有独立法人资格，注册资本不低于人民币1000万元。"
PRICE_QUOTE = "价格分=40×（评标基准价÷评标价），计算结果保留两位小数"


@pytest.fixture()
def tender_document_id(client, samples_dir):
    """上传并解析样例招标文件，返回 document_id。"""
    pdf = next((samples_dir / "tender").glob("*招标文件.pdf"))
    pid = client.post("/api/projects", json={"name": "证据测试项目"}).json()["id"]
    out = upload_pdf(client, pid, pdf, "tender")
    job = wait_job(client, out["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    return out["document_id"]


def test_locate_exact_hit(tender_document_id):
    with DocumentSource(tender_document_id) as src:
        result = locate(REAL_QUOTE, src)
    assert result.found
    assert result.verified
    assert result.method == "exact"
    assert result.page_no == 3
    assert result.bbox and result.page_size
    # top-left 原点坐标，且框在页面范围内
    x0, y0, x1, y1 = result.bbox[0]
    assert 0 <= x0 < x1 <= result.page_size[0]
    assert 0 <= y0 < y1 <= result.page_size[1]


def test_locate_normalized_equivalent(tender_document_id):
    """全角/引号差异不影响核验（归一化等价）。"""
    variant = "投标人须具有独立法人资格，注册资本不低于人民币１０００万元。"  # 全角数字
    with DocumentSource(tender_document_id) as src:
        result = locate(variant, src)
    assert result.found and result.verified


def test_locate_prefix_fallback(tender_document_id):
    """后半段被改写：前缀回退命中但不置 verified。"""
    rewritten = REAL_QUOTE[:-6] + "（判据被模型改写了）"
    with DocumentSource(tender_document_id) as src:
        result = locate(rewritten, src)
    assert result.found
    assert result.method == "prefix_fallback"
    assert not result.verified


def test_locate_fabricated_quote(tender_document_id):
    """引用造假：找不到任何匹配 → 无候选路径进入复核。"""
    fake = "本投标人注册资本不低于人民币5000万元且享有免税特权三年。"
    with DocumentSource(tender_document_id) as src:
        result = locate(fake, src)
    assert not result.found
    assert not result.verified


def test_locate_too_short_quote(tender_document_id):
    with DocumentSource(tender_document_id) as src:
        result = locate("投标报价", src)
    assert not result.found and not result.verified


def test_verifier_writes_verified(client, tender_document_id):
    """verifier 是 verified 的唯一写入点；页码以核验为准。"""
    from app.evidence.verifier import create_citation, verify_citation

    cit_id = create_citation(
        tender_document_id, PRICE_QUOTE, page_hint=4, role="criterion_source", verify=False
    )
    cit = verify_citation(cit_id)
    assert cit is not None
    assert cit.verified
    assert cit.verify_method == "exact"
    assert cit.page_no == 4
    assert cit.bbox_json


def test_verifier_fabricated(tender_document_id):
    from app.evidence.verifier import create_citation, verify_citation

    cit_id = create_citation(
        tender_document_id, "评标委员会可以自由调整各分项权重。", role="criterion_source"
    )
    cit = verify_citation(cit_id)
    assert cit is not None
    assert not cit.verified


def test_normalize_quote_still_matches(normalize_check=True):
    assert normalize("价格分＝４０×") == "价格分=40x"
