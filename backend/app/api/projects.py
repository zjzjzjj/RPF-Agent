"""项目 API 与工作台聚合。

工作台首屏只调 `/projects/{pid}/workbench`：各阶段状态 + 阻塞原因 + 待办计数。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

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

        # ① 招标文件
        tender_docs = (
            s.execute(
                select(Document).where(Document.project_id == project_id, Document.kind == "tender")
            )
            .scalars()
            .all()
        )
        parsed = [d for d in tender_docs if d.parse_status in ("parsed", "degraded")]
        failed = [d for d in tender_docs if d.parse_status == "failed"]
        if failed:
            stages.append(StageState(key="tender", label="招标文件", state="blocked",
                                     detail=f"{len(failed)} 份解析被拒绝", todo_count=len(failed)))
            blockers.append("招标文件解析被拒绝，请更换文件或查看拒绝原因")
        elif parsed:
            stages.append(StageState(key="tender", label="招标文件", state="done",
                                     detail=f"{len(parsed)} 份已解析"))
        elif tender_docs:
            stages.append(StageState(key="tender", label="招标文件", state="active",
                                     detail="解析中", todo_count=len(tender_docs)))
        else:
            stages.append(StageState(key="tender", label="招标文件", state="pending",
                                     detail="尚未上传招标文件"))

        # ② 规则包
        pkg = (
            s.execute(
                select(RulePackage)
                .where(RulePackage.project_id == project_id)
                .order_by(RulePackage.version.desc())
                .limit(1)
            )
            .scalar_one_or_none()
        )
        if pkg is None:
            stages.append(StageState(key="rules", label="评分规则", state="pending",
                                     detail="尚未编译规则"))
        elif pkg.status == "frozen":
            stages.append(StageState(key="rules", label="评分规则", state="done",
                                     detail=f"已冻结 v{pkg.version}（{pkg.content_hash or ''}）"))
        else:
            total = s.execute(
                select(func.count(Criterion.id)).where(Criterion.package_id == pkg.id)
            ).scalar_one()
            unverified = s.execute(
                select(func.count(Criterion.id)).where(
                    Criterion.package_id == pkg.id, Criterion.status != "verified"
                )
            ).scalar_one()
            stages.append(StageState(key="rules", label="评分规则", state="active",
                                     detail=f"v{pkg.version} 草稿：{total - unverified}/{total} 已确认",
                                     todo_count=int(unverified)))
            if unverified:
                blockers.append(f"评分规则有 {unverified} 项待确认（未确认不能冻结）")

        # ③ 投标人
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

        # ④ 评分运行
        run = (
            s.execute(
                select(EvaluationRun)
                .where(EvaluationRun.project_id == project_id)
                .order_by(EvaluationRun.id.desc())
                .limit(1)
            ).scalar_one_or_none()
        )
        if run is None:
            stages.append(StageState(key="evaluate", label="评分运行", state="pending",
                                     detail="尚未发起评分"))
        elif run.status in ("queued", "running"):
            stages.append(StageState(key="evaluate", label="评分运行", state="active",
                                     detail=f"运行中（#{run.id}）"))
        elif run.status == "failed":
            stages.append(StageState(key="evaluate", label="评分运行", state="blocked",
                                     detail=f"运行 #{run.id} 失败"))
            blockers.append(f"评分运行 #{run.id} 失败，请查看运行详情")
        else:
            stages.append(StageState(key="evaluate", label="评分运行", state="done",
                                     detail=f"运行 #{run.id} {run.status}"))

        # ⑤ 复核
        pending_review = 0
        if run is not None:
            pending_review = s.execute(
                select(func.count(ScoreRecord.id)).where(
                    ScoreRecord.run_id == run.id,
                    ScoreRecord.review_status.in_(("pending", "ai_suggested", "conflict_flagged")),
                )
            ).scalar_one()
        if run is None:
            stages.append(StageState(key="review", label="人工复核", state="pending", detail="—"))
        elif pending_review:
            stages.append(StageState(key="review", label="人工复核", state="active",
                                     detail=f"{pending_review} 项待复核", todo_count=int(pending_review)))
            # 复核不再是"阻塞"：报告可以先出草稿，把疑问标出，复核后再刷新。
            # 这里只提示"定稿前需处理"，不阻断任何后续步骤。
        else:
            stages.append(StageState(key="review", label="人工复核", state="done", detail="全部已复核"))

        # ⑥ 报告：有评分运行即可出**草稿**（未复核项在报告内逐条标注为疑问），
        # 全部复核完毕后同一入口即为定稿。不再要求"复核完成后可生成"。
        if run is None:
            stages.append(StageState(key="report", label="评标报告", state="pending",
                                     detail="发起评分后可生成"))
        elif run.status in ("queued", "running"):
            stages.append(StageState(key="report", label="评标报告", state="pending",
                                     detail="评分运行完成后可生成"))
        elif project.status == "completed" and not pending_review:
            stages.append(StageState(key="report", label="评标报告", state="done", detail="已定稿"))
        elif pending_review:
            stages.append(StageState(key="report", label="评标报告", state="active",
                                     detail=f"可生成草稿报告（{pending_review} 项待复核）",
                                     todo_count=0))
        else:
            stages.append(StageState(key="report", label="评标报告", state="active",
                                     detail="可生成定稿报告"))

        return WorkbenchOut(project=ProjectOut.model_validate(project), stages=stages, blockers=blockers)
