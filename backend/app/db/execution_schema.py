"""Explicit additive migration and read-only checks for automatic executor V2."""
from __future__ import annotations

from sqlalchemy import UniqueConstraint, inspect, select

EXECUTION_SCHEMA_VERSION = 2


def _models():
    from app.db.models import (
        ExecutionSchemaVersion, LLMRequestDailyBudget, ReviewCollectionBatch, ReviewCollectionSet,
    )
    return (ReviewCollectionSet, ReviewCollectionBatch, LLMRequestDailyBudget, ExecutionSchemaVersion)


def validate_execution_schema(bind) -> None:
    """Never create or repair tables as an application-start side effect."""
    inspector = inspect(bind)
    required_base = ("evaluation_runs", "documents", "document_pages", "score_records",
                     "evidence_citations", "criteria", "bidders", "jobs")
    missing = [name for name in required_base if not inspector.has_table(name)]
    for model in _models():
        table = model.__table__
        if not inspector.has_table(table.name):
            missing.append(table.name)
            continue
        existing = {col["name"] for col in inspector.get_columns(table.name)}
        if set(table.columns.keys()) - existing:
            missing.append(table.name + "(columns)")
        actual_unique = {frozenset(item["column_names"]) for item in inspector.get_unique_constraints(table.name)}
        required_unique = {frozenset(item.columns.keys()) for item in table.constraints if isinstance(item, UniqueConstraint)}
        if required_unique - actual_unique:
            missing.append(table.name + "(unique)")
    if not missing:
        from app.db.models import ExecutionSchemaVersion
        connection = bind.connect() if hasattr(bind, "connect") else None
        try:
            reader = connection if connection is not None else bind
            version = reader.execute(select(ExecutionSchemaVersion.version).where(
                ExecutionSchemaVersion.version == EXECUTION_SCHEMA_VERSION,
            )).scalar_one_or_none()
            if version is None:
                missing.append("execution_schema_versions(2)")
        finally:
            if connection is not None:
                connection.close()
    if missing:
        raise RuntimeError("执行器数据库结构未迁移：" + ", ".join(missing)
                           + "；请先执行 scripts/migrate_execution_v2.py，禁止应用启动时隐式迁移")


def upgrade_execution_schema(engine) -> None:
    """Only append new tables; existing scores, citations and inputs are untouched."""
    from app.db.models import ExecutionSchemaVersion

    with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        for model in _models():
            model.__table__.create(bind=connection, checkfirst=True)
        exists = connection.execute(select(ExecutionSchemaVersion.version).where(
            ExecutionSchemaVersion.version == EXECUTION_SCHEMA_VERSION,
        )).scalar_one_or_none()
        if exists is None:
            connection.execute(ExecutionSchemaVersion.__table__.insert().values(version=EXECUTION_SCHEMA_VERSION))
        validate_execution_schema(connection)
