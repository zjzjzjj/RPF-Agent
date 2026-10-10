"""评标报告 API：JSON 与 HTML（可打印/另存 PDF）。"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.services import report_service

router = APIRouter(tags=["reports"])


@router.get("/projects/{project_id}/report")
def get_report(project_id: str, run_id: int | None = None) -> dict:
    return report_service.build_report(project_id, run_id)


@router.get("/projects/{project_id}/report.html", response_class=HTMLResponse)
def get_report_html(project_id: str, run_id: int | None = None) -> HTMLResponse:
    report = report_service.build_report(project_id, run_id)
    return HTMLResponse(report_service.render_html(report))


@router.post("/projects/{project_id}/report/export")
def export_report(project_id: str, format: str = "html") -> dict:
    """导出接口：MVP 只实现 html；docx/pdf 返回明确的未实现错误（不静默失败）。"""
    if format != "html":
        from app.errors import AppError

        raise AppError(f"导出格式 {format} 尚未实现（MVP 仅支持 html；接口已预留）")
    return {"format": "html", "url": f"/api/projects/{project_id}/report.html"}
