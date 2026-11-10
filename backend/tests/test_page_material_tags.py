"""page_material_tags 直测（阶段4审查 P2 补强，2026-10-30）。

覆盖 hints.page_material_tags 的 DB 查询与过滤逻辑：
- 只取 seg_kind=page 且 method=rule 且 confidence>=CONF_STRONG
- text_sha 失效剔除（页文本已变则标签作废，宁缺勿错）
- 同页同 mat 多标签取最高置信
- 无命中返回 {}
此前仅在 classify_materials 注入 tag_pages 间接验证，DB 侧无直测。
"""
from __future__ import annotations

from app.db.models import Document, MaterialTag, Project
from app.db.session import session_scope
from app.material_index.hints import page_material_tags
from app.material_index.rules import CONF_STRONG
from app.material_index.service import text_sha


def _mk_doc(pid: str) -> int:
    from sqlalchemy import select

    with session_scope() as s:
        s.add(Project(id=pid, name="标签直测"))
        s.flush()
        s.add(Document(project_id=pid, kind="bid", original_filename="b.pdf",
                       stored_path="b.pdf", sha256="2" * 64, parse_status="parsed"))
        s.flush()
        doc_id = s.execute(
            select(Document.id).where(Document.project_id == pid)
        ).scalar_one()
    return doc_id


def _add_tag(doc_id: int, page: int, tags: list[str], conf: float,
             method: str = "rule", sha: str | None = None) -> None:
    with session_scope() as s:
        s.add(MaterialTag(document_id=doc_id, page_no=page, seg_kind="page",
                          category="commercial", tags_json=tags, method=method,
                          confidence=conf, text_sha=sha))


def test_page_material_tags_valid_hit(app_env) -> None:  # noqa: ANN001
    doc_id = _mk_doc("PRJ-TAG-1")
    _add_tag(doc_id, 5, ["合同复印件"], CONF_STRONG + 0.05)
    out = page_material_tags(doc_id, {5}, {5: "任意页文本"})
    assert out.get(5, {}).get("contract", (None, 0))[0] == "合同"  # 归一后命中


def test_page_material_tags_excludes_llm_and_low_conf(app_env) -> None:  # noqa: ANN001
    doc_id = _mk_doc("PRJ-TAG-2")
    _add_tag(doc_id, 6, ["发票"], CONF_STRONG + 0.05, method="llm")   # method 过滤
    _add_tag(doc_id, 7, ["发票"], CONF_STRONG - 0.05)                 # conf 过滤
    out = page_material_tags(doc_id, {6, 7}, {})
    assert 6 not in out and 7 not in out


def test_page_material_tags_stale_sha_dropped(app_env) -> None:  # noqa: ANN001
    doc_id = _mk_doc("PRJ-TAG-3")
    _add_tag(doc_id, 8, ["验收"], CONF_STRONG + 0.05, sha=text_sha("旧文本"))
    # 提供新文本 → sha 不匹配 → 标签作废
    out = page_material_tags(doc_id, {8}, {8: "重解析后的新文本"})
    assert 8 not in out
    # 不提供文本 → 无从校验 → 保留
    out2 = page_material_tags(doc_id, {8}, {})
    assert 8 in out2


def test_page_material_tags_conf_max_per_material(app_env) -> None:  # noqa: ANN001
    doc_id = _mk_doc("PRJ-TAG-4")
    # 同页两条 invoice 标签，取最高置信那条（用不同 tag 词区分）
    _add_tag(doc_id, 9, ["发票"], CONF_STRONG + 0.02)
    with session_scope() as s:
        s.add(MaterialTag(document_id=doc_id, page_no=9, seg_kind="page",
                          seg_index=1, category="commercial", tags_json=["发票"],
                          method="rule", confidence=CONF_STRONG + 0.15))
    out = page_material_tags(doc_id, {9}, {})
    assert out[9]["invoice"][1] == CONF_STRONG + 0.15  # 最高置信胜出


def test_page_material_tags_empty_when_no_match(app_env) -> None:  # noqa: ANN001
    doc_id = _mk_doc("PRJ-TAG-5")
    _add_tag(doc_id, 10, ["开标一览表"], CONF_STRONG + 0.05)  # 非三态证据词
    out = page_material_tags(doc_id, {10}, {})
    assert out == {} or 10 not in out
