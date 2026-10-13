"""项目删除服务：按外键依赖序手动清理（SQLite RESTRICT 外键真实生效）。

删除顺序（每张 RESTRICT 表都要在父行之前清掉）：
  1 evidence_citations（criterion_id 指向本项目包的 criteria——compile 期引用）
  2 score_records（bidder_id/criterion_id RESTRICT）
  3 evaluation_runs（project_id RESTRICT）
  4 override_records（package_id/merge_package_id RESTRICT）
  5 criteria（删除后 levels/checks/price_formulas/package_validations 级联）
  6 rule_packages 中 merge 辅助包（merge_target_id 自引用 RESTRICT，先删）
  7 rule_packages（parent_version_id 自引用 RESTRICT）
  8 documents（project_id RESTRICT；其 pages/nodes/bidder_documents 级联）
  9 bidders（project_id RESTRICT）
 10 jobs（纯字符串列，运维数据，一并清）
 11 projects 本体

audit_logs / review_records **保留**：全局合规留痕（无外键约束），删除动作本身
也会写一条 audit（project.delete）。物理文件（上传件、渲染缓存）在事务提交后
删除，失败仅记日志，不影响删除结果。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from sqlalchemy import delete, select

from app.db.models import (
    Bidder,
    Criterion,
    Document,
    EvidenceCitation,
    EvaluationRun,
    Job,
    OverrideRecord,
    Project,
    RulePackage,
    ScoreRecord,
)
from app.db.session import session_scope
from app.errors import NotFound

log = logging.getLogger("bid_eval.project_delete")


def delete_project(project_id: str) -> dict:
    """删除项目及全部关联数据；不存在抛 NotFound。返回各表删除计数。"""
    files_to_remove: list[Path] = []
    counts: dict[str, int] = {}

    with session_scope() as s:
        project = s.get(Project, project_id)
        if project is None:
            raise NotFound("项目不存在")

        files_to_remove = [
            Path(row[0])
            for row in s.execute(select(Document.stored_path).where(
                Document.project_id == project_id)).all() if row[0]
        ]
        package_ids = [
            row[0] for row in s.execute(
                select(RulePackage.id).where(RulePackage.project_id == project_id)).all()
        ]
        run_ids = [
            row[0] for row in s.execute(
                select(EvaluationRun.id).where(
                    EvaluationRun.project_id == project_id)).all()
        ]

        def _count(result) -> int:  # noqa: ANN001
            return int(result.rowcount or 0)

        if package_ids:
            counts["evidence_citations"] = _count(s.execute(
                delete(EvidenceCitation).where(EvidenceCitation.criterion_id.in_(
                    select(Criterion.id).where(Criterion.package_id.in_(package_ids))))))
        counts["score_records"] = _count(s.execute(
            delete(ScoreRecord).where(ScoreRecord.run_id.in_(run_ids))))
        counts["evaluation_runs"] = _count(s.execute(
            delete(EvaluationRun).where(EvaluationRun.project_id == project_id)))
        if package_ids:
            counts["override_records"] = _count(s.execute(
                delete(OverrideRecord).where(
                    OverrideRecord.package_id.in_(package_ids)
                    | OverrideRecord.merge_package_id.in_(package_ids))))
            counts["criteria"] = _count(s.execute(
                delete(Criterion).where(Criterion.package_id.in_(package_ids))))
            counts["rule_packages_merge_aux"] = _count(s.execute(
                delete(RulePackage).where(
                    RulePackage.project_id == project_id,
                    RulePackage.merge_target_id.isnot(None))))
            counts["rule_packages"] = _count(s.execute(
                delete(RulePackage).where(RulePackage.project_id == project_id)))
        counts["documents"] = _count(s.execute(
            delete(Document).where(Document.project_id == project_id)))
        counts["bidders"] = _count(s.execute(
            delete(Bidder).where(Bidder.project_id == project_id)))
        counts["jobs"] = _count(s.execute(
            delete(Job).where(Job.project_id == project_id)))

        s.delete(project)
        counts["projects"] = 1

    # 事务已提交：审计留痕（audit_logs 保留，不随项目删除）+ 物理文件清理
    from app.services import audit_service

    audit_service.log(project_id, "auto", "project.delete", "project",
                      project_id, counts)

    render_root = _render_root()
    for f in files_to_remove:
        try:
            Path(f).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("删除上传文件失败：%s（%s）", f, exc)
    if render_root.is_dir():
        for d in render_root.iterdir():
            shutil.rmtree(d, ignore_errors=True)

    return counts


def _render_root() -> Path:
    from app import config

    return config.render_dir()
