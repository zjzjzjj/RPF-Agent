"""审计流水：全局粗粒度动作记录（复核留痕见 review_records）。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db.models import AuditLog
from app.db.session import session_scope


def log(
    project_id: str | None,
    actor: str,
    action: str,
    subject_type: str | None = None,
    subject_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    with session_scope() as s:
        s.add(
            AuditLog(
                project_id=project_id,
                actor=actor,
                action=action,
                subject_type=subject_type,
                subject_id=subject_id,
                detail_json=detail or {},
            )
        )


def list_logs(project_id: str, limit: int = 200) -> list[AuditLog]:
    with session_scope() as s:
        rows = (
            s.execute(
                select(AuditLog)
                .where(AuditLog.project_id == project_id)
                .order_by(AuditLog.id.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        for r in rows:
            s.expunge(r)
        return list(rows)
