"""项目 API 与工作台聚合。

工作台首屏只调 `/projects/{pid}/workbench`：各阶段状态 + 阻塞原因 + 待办计数。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app import config
from app.db.models import Bidder, Criterion, Document, EvaluationRun, Project, RulePackage, ScoreRecord
from app.db.session import session_scope
from app.deps import get_actor, new_project_id
from app.errors import NotFound
from app.schemas.project import ProjectCreate, ProjectOut, ProjectPatch, StageState, WorkbenchOut
from app.services import audit_service

router = APIRouter(tags=["projects"])

_RULE_READY = ("verified",)


def _get_project_or_404(s, project_id: str) -> Project:  # noqa: ANN001
    project = s.get(Project, project_id)
    if project is None:
        raise NotFound("项目不存在")
    return project


@router.post("/projects", response_model=ProjectOut)
def create_project(body: ProjectCreate, actor: str = Depends(get_actor)) -> ProjectOut:
    with session_scope() as s:
        project = Project(
            id=new_project_id(),
            name=body.name,
            project_code=body.project_code,
            purchaser=body.purchaser,
            max_price=body.max_price,
            method=body.method,
        )
        s.add(project)
        s.flush()
        out = ProjectOut.model_validate(project)
    audit_service.log(out.id, actor, "project.create", "project", out.id, {"name": body.name})
    return out


@router.get("/projects", response_model=list[ProjectOut])
def list_projects() -> list[ProjectOut]:
    with session_scope() as s:
        rows = s.execute(select(Project).order_by(Project.created_at.desc())).scalars().all()
        return [ProjectOut.model_validate(r) for r in rows]


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: str) -> ProjectOut:
    with session_scope() as s:
        return ProjectOut.model_validate(_get_project_or_404(s, project_id))


@router.delete("/projects/{project_id}")
def delete_project(project_id: str, actor: str = Depends(get_actor)) -> dict:
    """删除项目及全部关联数据（文档/规则/评分/报告），审计与复核留痕保留。"""
    from app.services import project_service

    counts = project_service.delete_project(project_id)
    return {"deleted": project_id, **counts}


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def patch_project(project_id: str, body: ProjectPatch, actor: str = Depends(get_actor)) -> ProjectOut:
    with session_scope() as s:
        project = _get_project_or_404(s, project_id)
        changed: dict = {}
        for field in ("name", "project_code", "purchaser", "max_price", "status"):
            value = getattr(body, field)
            if value is not None:
                setattr(project, field, value)
                changed[field] = str(value)
        s.flush()
        out = ProjectOut.model_validate(project)
    if changed:
        audit_service.log(project_id, actor, "project.patch", "project", project_id, changed)
    return out


@router.get("/projects/{project_id}/workbench", response_model=WorkbenchOut)
def get_workbench(project_id: str) -> WorkbenchOut:
    # 先回收僵尸运行：卡在 running 但任务已不在的运行会让整个项目永远显示"评分运行中"。
    from app.services.scoring_service import reconcile_stale_runs

    reconcile_stale_runs(project_id)
    with session_scope() as s:
        project = _get_project_or_404(s, project_id)
        stages: list[StageState] = []
        blockers: list[str] = []

        # 查询：招标文件 / 规则包（与 current_package 同口径：排除 superseded 与 merge 辅助包）
        tender_docs = (
            s.execute(
                select(Document).where(Document.project_id == project_id, Document.kind == "tender")
            )
            .scalars()
            .all()
        )
        parsed = [d for d in tender_docs if d.parse_status in ("parsed", "degraded")]
        failed = [d for d in tender_docs if d.parse_status == "failed"]
        pkg = (
            s.execute(
                select(RulePackage)
                .where(
                    RulePackage.project_id == project_id,
                    RulePackage.status != "superseded",
                    RulePackage.merge_target_id.is_(None),
                )
                .order_by(RulePackage.version.desc())
                .limit(1)
            )
            .scalar_one_or_none()
        )
        # 最新评分运行与待复核数（人工模式口径：pending/ai_suggested/conflict_flagged）
        run = (
            s.execute(
                select(EvaluationRun)
                .where(EvaluationRun.project_id == project_id)
                .order_by(EvaluationRun.id.desc())
                .limit(1)
            ).scalar_one_or_none()
        )
        pending_review = 0
        if run is not None:
            pending_review = s.execute(
                select(func.count(ScoreRecord.id)).where(
                    ScoreRecord.run_id == run.id,
                    ScoreRecord.review_status.in_(("pending", "ai_suggested", "conflict_flagged")),
                )
            ).scalar_one()

        # ① 规则库与招标文件：规则页同时承载招标文件上传与规则管理，合成一个阶段。
        # 状态优先级（首条命中）：解析失败 > 解析中 > 无规则包 > 已冻结 > 草稿待确认。
        if failed:
            tender_frag = f"{len(failed)} 份招标文件解析被拒绝"
        elif parsed:
            tender_frag = f"已解析 {len(parsed)} 份招标文件"
        elif tender_docs:
            tender_frag = "招标文件解析中"
        else:
            # 已有规则包时招标文件是可选："使用默认规则直接评分"不被一直催着上传
            tender_frag = "未上传招标文件（使用内置/模板规则）"

        unverified = 0
        if pkg is None:
            rules_frag = "尚未编译规则"
        elif pkg.status == "frozen":
            rules_frag = f"v{pkg.version} 已冻结（{(pkg.content_hash or '')[:8]}）"
        else:
            total = s.execute(
                select(func.count(Criterion.id)).where(
                    Criterion.package_id == pkg.id, Criterion.status != "retired"
                )
            ).scalar_one()
            unverified = s.execute(
                select(func.count(Criterion.id)).where(
                    Criterion.package_id == pkg.id,
                    Criterion.status.notin_(("verified", "retired")),
                )
            ).scalar_one()
            rules_frag = f"v{pkg.version} 草稿：{total - unverified}/{total} 已确认"

        if failed:
            stages.append(StageState(key="rules", label="规则库与招标文件", state="blocked",
                                     detail=f"{tender_frag}；{rules_frag}", todo_count=len(failed)))
            blockers.append("招标文件解析被拒绝，请更换文件或查看拒绝原因")
        elif tender_docs and not parsed:
            stages.append(StageState(key="rules", label="规则库与招标文件", state="active",
                                     detail=f"{tender_frag}；{rules_frag}",
                                     todo_count=len(tender_docs)))
        elif pkg is None:
            stages.append(StageState(
                key="rules", label="规则库与招标文件",
                state="active" if parsed else "pending",
                detail=f"{tender_frag}；{rules_frag}"))
        elif pkg.status == "frozen":
            stages.append(StageState(key="rules", label="规则库与招标文件", state="done",
                                     detail=f"{tender_frag}；{rules_frag}",
                                     optional=not tender_docs))
        else:
            stages.append(StageState(key="rules", label="规则库与招标文件", state="active",
                                     detail=f"{tender_frag}；{rules_frag}",
                                     todo_count=int(unverified)))
            if unverified:
                blockers.append(f"评分规则有 {unverified} 项待确认（未确认不能冻结）")

        # ② 投标人
        bidder_count = s.execute(
            select(func.count(Bidder.id)).where(Bidder.project_id == project_id)
        ).scalar_one()
        price_confirmed = s.execute(
            select(func.count(Bidder.id)).where(
                Bidder.project_id == project_id, Bidder.price_status == "confirmed"
            )
        ).scalar_one()
        if bidder_count == 0:
            stages.append(StageState(key="bidders", label="投标人", state="pending",
                                     detail="尚未添加投标人"))
        elif price_confirmed < bidder_count:
            stages.append(StageState(key="bidders", label="投标人", state="active",
                                     detail=f"报价已确认 {price_confirmed}/{bidder_count}",
                                     todo_count=int(bidder_count - price_confirmed)))
        else:
            stages.append(StageState(key="bidders", label="投标人", state="done",
                                     detail=f"{bidder_count} 家，报价全部已确认"))

        # ③ 评分结果：评分运行、复核与报告都在评分结果页，合成一个阶段。
        # 状态优先级：无运行 > 运行中 > 运行失败 > 人工复核待办（仅人工模式）> 报告定稿 > 可查看/可生成。
        if run is None:
            stages.append(StageState(key="results", label="评分结果", state="pending",
                                     detail="尚未发起评分"))
        elif run.status in ("queued", "running"):
            stages.append(StageState(key="results", label="评分结果", state="active",
                                     detail=f"评分运行中（#{run.id}）"))
        elif run.status == "failed":
            stages.append(StageState(key="results", label="评分结果", state="blocked",
                                     detail=f"运行 #{run.id} 失败"))
            blockers.append(f"评分运行 #{run.id} 失败，请查看运行详情")
        elif not config.auto_mode() and pending_review:
            # 复核不阻断：报告可先出草稿（未复核项在报告内逐条标注为疑问），复核后刷新即定稿。
            stages.append(StageState(key="results", label="评分结果", state="active",
                                     detail=f"{pending_review} 项待复核；可生成草稿报告",
                                     todo_count=int(pending_review)))
        elif project.status == "completed":
            stages.append(StageState(key="results", label="评分结果", state="done",
                                     detail="报告已定稿"))
        else:
            stages.append(StageState(key="results", label="评分结果", state="active",
                                     detail=f"运行 #{run.id} 完成；可查看结果，可生成报告"))

        return WorkbenchOut(project=ProjectOut.model_validate(project), stages=stages, blockers=blockers)
