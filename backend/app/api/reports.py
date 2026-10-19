"""评标报告 API：JSON 与 HTML（可打印/另存 PDF）+ 正式评审报告（HTML/docx）。"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from app.services import report_service

router = APIRouter(tags=["reports"])


@router.get("/projects/{project_id}/report")
def get_report(project_id: str, run_id: int | None = None) -> dict:
    return report_service.build_report(project_id, run_id)


@router.get("/projects/{project_id}/report.html", response_class=HTMLResponse)
def get_report_html(project_id: str, run_id: int | None = None) -> HTMLResponse:
    report = report_service.build_report(project_id, run_id)
    return HTMLResponse(report_service.render_html(report))


@router.get("/projects/{project_id}/report/formal.html", response_class=HTMLResponse)
def get_formal_report_html(project_id: str, run_id: int | None = None) -> HTMLResponse:
    """正式评审报告（借鉴真实评审资料形态，可浏览器打印/另存 PDF）。"""
    report = report_service.build_formal_report(project_id, run_id)
    return HTMLResponse(report_service.render_formal_html(report))


@router.get("/projects/{project_id}/report/docx")
def get_formal_report_docx(project_id: str, run_id: int | None = None) -> Response:
    """正式评审报告 docx 下载（Word 可直接打开编辑/打印/签章）。"""
    report = report_service.build_formal_report(project_id, run_id)
    content = report_service.render_docx(report)
    filename = f"{project_id}_run{report['run']['id']}_评审报告.docx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.post("/projects/{project_id}/report/export")
def export_report(project_id: str, format: str = "html", run_id: int | None = None) -> dict:
    """导出接口：html（疑问清单版）/ formal.html / docx（正式评审报告）。"""
    if format == "html":
        return {"format": "html", "url": f"/api/projects/{project_id}/report.html"}
    if format == "formal_html":
        return {"format": "formal_html", "url": f"/api/projects/{project_id}/report/formal.html"}
    if format == "docx":
        report = report_service.build_formal_report(project_id, run_id)
        return {
            "format": "docx",
            "url": f"/api/projects/{project_id}/report/docx",
            "run_id": report["run"]["id"],
        }
    from app.errors import AppError

    raise AppError(f"导出格式 {format} 不支持（可选：html / formal_html / docx）")
