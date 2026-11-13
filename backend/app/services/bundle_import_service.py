"""项目包导入编排：zip → 招标入库 + 投标人自动建档 + 逐公司双通道抽价。

输入格式（ingest.zip_intake 校验）：一个 zip 含 `招标/`（PDF/DOCX）与
`投标/公司名/`（严格一层公司文件夹，文件夹名=投标人名称）。

职责边界：
- 解压与结构识别 → app.ingest.zip_intake（安全与错误码都在那边）；
- 落盘/建档/排队解析 → ingest_service.save_upload（既有链路：parse_document →
  规则自动匹配 / 材料分类）；
- 抽价 → app.pricing.extractor（四通道）→ bidder_service.apply_extraction；
- 抽价失败**不阻断**：price_status="missing"，该公司不参与价格分
  （评分侧已有"无效报价"语义），job 照常成功并在 result 标注。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from sqlalchemy import delete, select

from app.db.models import (
    Bidder,
    BidderDocument,
    Document,
    EvaluationRun,
    EvidenceCitation,
)
from app.db.session import session_scope
from app.ingest.zip_intake import CompanyBundle, TenderFile, extract_bundle
from app.jobs.handlers import register
from app.jobs.queue import JobContext
from app.pricing.dto import cross_check_json, derive_status
from app.pricing.extractor import extract_company_price
from app.services import audit_service, bidder_service, ingest_service

log = logging.getLogger("bid_eval.bundle_import")

_PRICE_NAME_KEYS = ("开标一览表", "分项报价表", "报价表", "报价一览表", "分项报价")


def _file_role(path: Path, main_pdf: Path | None) -> str:
    if any(k in path.name for k in _PRICE_NAME_KEYS):
        return "price"
    if main_pdf is not None and path == main_pdf:
        return "main"
    return "other"


def _pick_main_pdf(files: tuple[Path, ...]) -> Path | None:
    pdfs = [f for f in files if f.suffix.lower() == ".pdf"]
    if not pdfs:
        return None
    named = [f for f in pdfs if "投标文件" in f.name]
    return max(named or pdfs, key=lambda f: f.stat().st_size)


def _purge_project_inputs(project_id: str) -> dict:
    """重新导入前清空旧输入（投标人走 delete_bidder 依赖序；招标侧文档直接清）。"""
    counts: dict[str, int] = {}
    bidder_ids = [b.id for b in bidder_service.list_bidders(project_id)]
    for bid in bidder_ids:
        counts.update(bidder_service.delete_bidder(bid, actor="auto"))
    files_to_remove: list[Path] = []
    with session_scope() as s:
        doc_rows = s.execute(
            select(Document.id, Document.stored_path).where(Document.project_id == project_id)
        ).all()
        doc_ids = [r[0] for r in doc_rows]
        files_to_remove = [Path(r[1]) for r in doc_rows if r[1]]
        if doc_ids:
            s.execute(delete(EvidenceCitation).where(EvidenceCitation.document_id.in_(doc_ids)))
            s.execute(delete(BidderDocument).where(BidderDocument.document_id.in_(doc_ids)))
            counts["documents"] = int(
                s.execute(delete(Document).where(Document.id.in_(doc_ids))).rowcount or 0
            )
    for f in files_to_remove:
        try:
            f.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("删除旧上传件失败：%s（%s）", f, exc)
    counts["bidders"] = len(bidder_ids)
    return counts


@register("import_bundle")
def import_bundle(ctx: JobContext, payload: dict) -> dict:
    project_id = str(payload["project_id"])
    zip_path = Path(payload["zip_path"])
    mode = str(payload.get("mode") or "create")
    actor = str(payload.get("actor") or "auto")

    ctx.start(total=3, message="解压并校验项目包")
    work = zip_path.parent / "extracted"
    layout = extract_bundle(zip_path, work)
    warnings = list(layout.warnings)

    if mode == "reimport":
        ctx.advance(message="清理旧数据")
        purge = _purge_project_inputs(project_id)
        log.info("重新导入清空旧输入：%s", purge)

    # ---- 招标侧入库（首个=主招标文件；其余=补遗） ----
    tender_docs: list[dict] = []
    for i, tf in enumerate(layout.tender_files):
        ctx.advance(message=f"招标文件入库（{i + 1}/{len(layout.tender_files)}）")
        content = tf.path.read_bytes()
        kind = "tender" if tf.is_primary else "addendum"
        doc_id, _job = ingest_service.save_upload(
            project_id=project_id,
            filename=tf.path.name,
            content=content,
            kind=kind,
        )
        tender_docs.append(
            {"document_id": doc_id, "filename": tf.path.name, "role": kind}
        )

    # ---- 投标人自动建档 + 文件入库 + 抽价 ----
    n = len(layout.companies)
    bidder_results: list[dict] = []
    for i, comp in enumerate(layout.companies):
        ctx.advance(message=f"投标文件入库（{i + 1}/{n}）：{comp.name}")
        bidder = bidder_service.create_bidder(project_id, comp.name)
        main_pdf = _pick_main_pdf(comp.files)
        files_meta: list[dict] = []
        for f in comp.files:
            role = _file_role(f, main_pdf)
            doc_id, _job = ingest_service.save_upload(
                project_id=project_id,
                filename=f.name,
                content=f.read_bytes(),
                kind="bid",
                bidder_id=bidder.id,
            )
            with session_scope() as s:
                s.add(BidderDocument(bidder_id=bidder.id, document_id=doc_id, role=role))
            files_meta.append({"document_id": doc_id, "filename": f.name, "role": role})

        ctx.advance(message=f"抽取报价（{i + 1}/{n}）：{comp.name}")
        extraction = extract_company_price(comp.files, comp.name)
        updated = bidder_service.apply_extraction(bidder.id, extraction, actor=actor)
        price = {
            "status": derive_status(extraction),
            "bid_price": str(extraction.bid_price) if extraction.bid_price is not None else None,
            "source": extraction.source or None,
            "basis": extraction.basis,
            "unit": extraction.unit,
            "tax_basis": extraction.tax_basis,
            "tax_rate": str(extraction.tax_rate) if extraction.tax_rate is not None else None,
            "upper": extraction.upper,
            "cross_check": cross_check_json(extraction),
            "items": [
                {"product": it.product, "price": str(it.price), "unit": it.unit}
                for it in extraction.items
            ],
        }
        if price["status"] == "missing":
            warnings.append(f"未抽取到投标报价（不参与价格分）：{comp.name}")
        bidder_results.append(
            {
                "bidder_id": updated.id,
                "name": comp.name,
                "files": files_meta,
                "price": price,
                "warnings": extraction.warnings,
            }
        )

    # 数据已复制进 uploads，解压暂存可清
    shutil.rmtree(work, ignore_errors=True)
    zip_path.unlink(missing_ok=True)
    try:
        zip_path.parent.rmdir()  # 暂存目录已空则一并移除（失败路径保留现场便于排查）
    except OSError:
        pass

    priced = sum(1 for b in bidder_results if b["price"]["status"] != "missing")
    result = {
        "project_id": project_id,
        "mode": mode,
        "wrapper_stripped": layout.wrapper_stripped,
        "tender_documents": tender_docs,
        "bidders": bidder_results,
        "counts": {
            "bidders": n,
            "priced": priced,
            "unpriced": n - priced,
        },
        "warnings": warnings,
    }
    audit_service.log(
        project_id, actor, "project.import_bundle", "project", project_id,
        {"counts": result["counts"], "mode": mode},
    )
    return result
