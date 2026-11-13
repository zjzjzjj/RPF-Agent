"""项目包导入 API：一个 zip（招标/ + 投标/公司名/）一键导入为评标项目。

同步预检（peek_zip）在建项目**之前**完成：结构不合法直接 4xx，
不产生项目脏数据；解压/入库/抽价在后台 job（import_bundle）里做，
进度走 `GET /jobs/{id}` / SSE `GET /jobs/{id}/events`。
"""
from __future__ import annotations

import shutil
import uuid
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app import config
from app.db.models import EvaluationRun, Project
from app.db.session import session_scope
from app.deps import get_actor, new_project_id
from app.errors import Conflict, ImportBundleError, NotFound, ZipTooLarge
from app.ingest.zip_intake import peek_zip
from app.services import audit_service, ingest_service

router = APIRouter(tags=["imports"])


def _save_zip(content: bytes, filename: str | None) -> Path:
    max_zip = config.import_max_zip_mb() * 1024 * 1024
    if len(content) > max_zip:
        raise ZipTooLarge(
            f"项目包超出大小限制（{config.import_max_zip_mb()} MB）",
            detail={"limit_mb": config.import_max_zip_mb()},
        )
    if not content:
        raise ImportBundleError("项目包为空")
    run_dir = config.import_dir() / f"imp_{uuid.uuid4().hex[:12]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    safe = ingest_service.sanitize_filename(filename or "bundle.zip")
    if not safe.lower().endswith(".zip"):
        safe += ".zip"
    zip_path = run_dir / safe
    zip_path.write_bytes(content)
    return zip_path


def _precheck(zip_path: Path) -> None:
    """同步结构预检；失败时清掉已落盘的暂存 zip（4xx 不留脏数据）后原样抛出。"""
    try:
        peek_zip(zip_path)
    except Exception:
        shutil.rmtree(zip_path.parent, ignore_errors=True)
        raise


@router.post("/projects/import-bundle")
def import_bundle_create(
    file: UploadFile = File(...),
    name: str | None = Form(None),
    project_code: str | None = Form(None),
    purchaser: str | None = Form(None),
    max_price: Decimal | None = Form(None),
    method: str = Form("comprehensive"),
    base_template_id: str | None = Form(None),
    actor: str = Depends(get_actor),
) -> JSONResponse:
    """新建项目并导入项目包。202 → {project_id, job_id}。"""
    content = file.file.read()
    zip_path = _save_zip(content, file.filename)
    _precheck(zip_path)  # 结构预检：失败抛 ImportBundleError 子类（4xx），并清掉暂存

    project_name = (name or Path(file.filename or "").stem or "导入项目").strip() or "导入项目"
    with session_scope() as s:
        project = Project(
            id=new_project_id(),
            name=project_name,
            project_code=project_code,
            purchaser=purchaser,
            max_price=max_price,
            method=method,
        )
        s.add(project)
        s.flush()
        project_id = project.id

    if base_template_id:
        from app.services import library_service

        library_service.instantiate_template(project_id, base_template_id, actor)

    from app.jobs.queue import submit

    job_id = submit(
        "import_bundle",
        {"project_id": project_id, "zip_path": str(zip_path), "mode": "create", "actor": actor},
        project_id=project_id,
    )
    audit_service.log(project_id, actor, "project.import_bundle_start", "project", project_id,
                      {"job_id": job_id, "mode": "create"})
    return JSONResponse(status_code=202, content={"project_id": project_id, "job_id": job_id})


@router.post("/projects/{project_id}/import-bundle")
def import_bundle_reimport(
    project_id: str,
    file: UploadFile = File(...),
    actor: str = Depends(get_actor),
) -> JSONResponse:
    """重新导入（覆盖输入数据）。项目已有评分运行时拒绝（409）。"""
    with session_scope() as s:
        project = s.get(Project, project_id)
        if project is None:
            raise NotFound("项目不存在")
        n_runs = s.execute(
            select(func.count(EvaluationRun.id)).where(EvaluationRun.project_id == project_id)
        ).scalar_one()
        if n_runs:
            raise Conflict(
                f"项目已有 {n_runs} 个评分运行，重新导入会改写历史口径；请先删除评分运行或新建项目"
            )

    content = file.file.read()
    zip_path = _save_zip(content, file.filename)
    _precheck(zip_path)

    from app.jobs.queue import submit

    job_id = submit(
        "import_bundle",
        {"project_id": project_id, "zip_path": str(zip_path), "mode": "reimport", "actor": actor},
        project_id=project_id,
    )
    audit_service.log(project_id, actor, "project.import_bundle_start", "project", project_id,
                      {"job_id": job_id, "mode": "reimport"})
    return JSONResponse(status_code=202, content={"project_id": project_id, "job_id": job_id})
