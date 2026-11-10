"""Additive migration is explicit, repeatable and leaves business rows intact."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from app.db.execution_schema import _models, upgrade_execution_schema, validate_execution_schema


def _database(tmp_path):
    path = tmp_path / "before.db"
    from app.db.base import Base
    new_names = {model.__tablename__ for model in _models()}
    engine = create_engine("sqlite:///" + path.as_posix())
    Base.metadata.create_all(engine, tables=[table for table in Base.metadata.sorted_tables if table.name not in new_names])
    engine.dispose()
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE historical_results (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        con.execute("INSERT INTO historical_results VALUES (1, 'preserve-this-result')")
    return path


def test_additive_migration_preserves_rows_and_is_idempotent(tmp_path):
    path = _database(tmp_path)
    engine = create_engine("sqlite:///" + path.as_posix())
    try:
        with pytest.raises(RuntimeError, match="未迁移"):
            validate_execution_schema(engine)
        upgrade_execution_schema(engine)
        upgrade_execution_schema(engine)
        validate_execution_schema(engine)
        with engine.connect() as con:
            assert con.execute(text("SELECT * FROM historical_results")).all() == [(1, "preserve-this-result")]
            assert con.execute(text("SELECT version FROM execution_schema_versions")).all() == [(2,)]
        inspector = inspect(engine)
        assert any(set(item["column_names"]) == {"collection_id", "batch_key"}
                   for item in inspector.get_unique_constraints("review_collection_batches"))
    finally:
        engine.dispose()


def test_migration_failure_rolls_back_new_tables(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + _database(tmp_path).as_posix())
    models = _models()
    def broken(*args, **kwargs):
        raise RuntimeError("injected DDL failure")
    monkeypatch.setattr(models[1].__table__, "create", broken)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            upgrade_execution_schema(engine)
        assert not inspect(engine).has_table("review_collection_sets")
        with engine.connect() as con:
            assert con.execute(text("SELECT COUNT(*) FROM historical_results")).scalar_one() == 1
    finally:
        engine.dispose()


def test_production_start_validates_without_implicit_creation(tmp_path, monkeypatch):
    from app.db import session as db
    path = _database(tmp_path)
    monkeypatch.setenv("BID_EVAL_DB_PATH", str(path))
    monkeypatch.setenv("BID_EVAL_ENV", "production")
    monkeypatch.setenv("BID_EVAL_DB_AUTO_INIT", "0")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    db.reset_engine()
    try:
        with pytest.raises(RuntimeError, match="未迁移"):
            db.initialize_application_database()
        assert not inspect(db.get_engine()).has_table("review_collection_sets")
    finally:
        db.reset_engine()


def test_migration_script_takes_consistent_backup(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "migrate_execution_v2.py"
    spec = importlib.util.spec_from_file_location("execution_migrator_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    database = _database(tmp_path)
    backup = module.migrate(database)
    assert backup.is_file()
    with sqlite3.connect(backup) as con:
        assert con.execute("SELECT payload FROM historical_results").fetchone()[0] == "preserve-this-result"
        assert con.execute("SELECT name FROM sqlite_master WHERE name='review_collection_sets'").fetchone() is None
    assert module.migrate(database, check_only=True) is None
