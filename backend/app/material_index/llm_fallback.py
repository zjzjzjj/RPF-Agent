# -*- coding: utf-8 -*-
"""材料分类·LLM 兜底层：只接手规则层未判出的页（conf<0.5）。

哲学与 rescue_material_pages 一致：分类是增强不是主链——LLM 失败降级
other/llm_failed，job 照常成功；配额类错误也不让 job 崩（记入 llm_errors）。
规则强命中（≥0.85）绝不送 LLM，省调用且保住可复算性。
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app import config
from app.llm.base import LLMError
from app.material_index import rules
from app.material_index.catalog import MATERIAL_CATEGORIES
from app.parsing.ocr_tags import clean_ocr_tags

log = logging.getLogger("bid_eval.material_index.llm")

# 每批页数与输入预算
_BATCH_PAGES = 10
_PAGE_CHARS = 600
PROMPT_VERSION = "classify-1"

_SYSTEM = """你是投标文件材料分类员。给定投标文件若干页的文本片段，判断每页属于哪一类材料：

- qualification：资格及证明文件（营业执照、资质证书、社保证明、纳税证明、审计报告、承诺函、资格声明等）
- commercial：商务服务（商务偏离、服务承诺、售后、培训、运维服务、业绩/类似项目、合同/验收/发票等）
- technical：技术方案（实施方案、技术路线、架构、质量保证、应急预案、项目管理、进度计划等）
- price：报价（开标一览表、报价表、分项报价、投标总价、报价函等）
- other：封面、目录、无法判断

要求：
1. 每页输出一条，page_no 必须原样回填；
2. tags 给 ≤5 个细粒度标签（如"社保证明""开标一览表"），没有就空列表；
3. confidence 0~1：只凭零星词命中就低分，页内有明确材料标题才高分；
4. reason ≤50 字，引用页内原文关键词作依据。
只输出 JSON。"""


class ClassifyItem(BaseModel):
    page_no: int
    category: str = Field(pattern="^(qualification|commercial|technical|price|other)$")
    tags: list[str] = Field(default_factory=list, max_length=5)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(default="", max_length=120)


class ClassifyBatchOut(BaseModel):
    items: list[ClassifyItem]


def fill_unknown(
    document_id: int,
    rows: list[rules.TagRow],
    text_by_page: dict[int, str],
) -> list[rules.TagRow]:
    """对 needs_llm 的页级行就地改写（category/tags/method/confidence/basis）。

    返回同一列表（原地更新）。LLM 不可用/失败 → 保持 other，仅标注 basis。
    """
    unknown = [r for r in rows if rules.needs_llm(r)]
    if not unknown:
        return rows
    if not config.llm_provider():
        return rows  # 未配置 LLM：纯规则，不标 llm_failed（不是失败）

    from app.llm.gateway import get_gateway

    try:
        gateway = get_gateway()
    except Exception:  # noqa: BLE001 —— 未配置等 AppError
        return rows

    max_pages = config.classify_llm_max_pages()
    unknown = unknown[:max_pages]
    llm_errors: list[str] = []

    for start in range(0, len(unknown), _BATCH_PAGES):
        batch = unknown[start : start + _BATCH_PAGES]
        user = _build_prompt(batch, text_by_page)
        try:
            out, _res = gateway.chat_json(
                _SYSTEM, user, ClassifyBatchOut,
                data_class="synthetic", prompt_version=PROMPT_VERSION,
            )
        except LLMError as exc:
            llm_errors.append(str(exc)[:200])
            log.warning("分类 LLM 兜底失败（文档 %s）：%s", document_id, exc)
            for r in batch:
                r.basis = (r.basis or "") + " | llm_failed"
            continue
        except Exception:  # noqa: BLE001 —— 配置类 AppError 等
            log.warning("分类 LLM 兜底不可用（文档 %s）", document_id, exc_info=True)
            for r in batch:
                r.basis = (r.basis or "") + " | llm_unavailable"
            continue

        by_no = {it.page_no: it for it in out.items}
        for r in batch:
            it = by_no.get(r.page_no)
            if it is None:
                r.basis = (r.basis or "") + " | llm_missed"
                continue
            if it.category not in MATERIAL_CATEGORIES:
                r.basis = (r.basis or "") + " | llm_bad_category"
                continue
            r.category = it.category
            r.tags = [str(t)[:32] for t in (it.tags or [])][:5]
            r.method = "llm"
            r.confidence = min(max(float(it.confidence), 0.0), 1.0)
            r.basis = f"LLM：{(it.reason or '')[:50]}"
    return rows


def _build_prompt(batch: list[rules.TagRow], text_by_page: dict[int, str]) -> str:
    parts = ["请对下列页面分类："]
    for r in batch:
        text = clean_ocr_tags(text_by_page.get(r.page_no, "") or "")[:_PAGE_CHARS]
        parts.append(f"\n--- page_no={r.page_no} ---\n{text or '（本页无文本）'}")
    return "\n".join(parts)
