"""法律层迁移脚本（scripts/migrate_law_layer.py）回归测试。

策略：用当前模型的元数据 DDL 建"迁移前状态"库（把两个 origin CHECK 里的 'law'
文本剥掉、documents 去掉 author 列），预置 S0 数据（origin='template' 行），
子进程跑迁移 → 断言 CHECK 扩充、author 列、**S0 列与数据逐列保留**、索引在、
FK 完整；二次运行三段 [skip]（幂等）。

注意：子进程 stdout 必须强制 UTF-8（Windows 管道默认 GBK，断言按 utf-8 解码）——
与 test_migration.py 同一教训。
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MIGRATE = REPO / "scripts" / "migrate_law_layer.py"


def _build_prelaw_db(db: Path) -> None:
    """建"迁移前"schema：当前模型 DDL − 'law' CHECK − documents.author。"""
    from sqlalchemy.dialects import sqlite
    from sqlalchemy.schema import CreateIndex, CreateTable

    import app.db.models  # noqa: F401 —— 注册全部表到 Base.metadata
    from app.db.base import Base

    assert Base.metadata.tables, "模型未注册到 Base.metadata"

    con = sqlite3.connect(str(db))
    try:
        for table in Base.metadata.sorted_tables:
            con.execute(str(CreateTable(table).compile(dialect=sqlite.dialect())))
            for index in table.indexes:  # index=True 生成的显式索引（CreateTable 不含它们）
                con.execute(str(CreateIndex(index).compile(dialect=sqlite.dialect())))

        # 倒推回迁移前：两个 origin CHECK 去掉 'law'（迁移脚本要做的正是反向操作）
        for tname in ("rule_packages", "criteria"):
            sql = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tname,)
            ).fetchone()[0]
            stripped = re.sub(r",\s*'law'", "", sql)
            assert stripped != sql, f"{tname} DDL 未含 'law'，夹具构造有误"
            new_sql = stripped.replace(f"CREATE TABLE {tname}", f"CREATE TABLE {tname}_pre", 1)
            idx = [
                r[0]
                for r in con.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                    (tname,),
                ).fetchall()
            ]
            con.execute(f"DROP TABLE {tname}")
            con.execute(new_sql)
            con.execute(f'ALTER TABLE "{tname}_pre" RENAME TO "{tname}"')
            for stmt in idx:
                con.execute(stmt)
        # documents 去掉 author 列（SQLite ≥3.35 直接 DROP COLUMN）
        con.execute("ALTER TABLE documents DROP COLUMN author")

        # 预置 S0 数据：1 项目 + 1 包 + 1 条带完整 S0 溯源的模板条目
        con.execute(
            "INSERT INTO projects (id, name, method, status, currency, created_at, updated_at)"
            " VALUES ('PRJ-PRE-0001', '迁移夹具', 'comprehensive', 'draft', 'CNY',"
            " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
        con.execute(
            "INSERT INTO rule_packages (id, project_id, version, status, origin, created_by,"
            " created_at, updated_at) VALUES (1, 'PRJ-PRE-0001', 1, 'draft', 'document',"
            " 'system', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
        con.execute(
            "INSERT INTO criteria (id, package_id, code, category, name, score_type, is_veto,"
            " status, origin, template_code, template_hash, library_version,"
            " override_json, signoff_json, p_source, sort_order)"
            " VALUES (1, 1, 'T1.1', 'qualification', '具有独立法人资格', 'veto', 1,"
            " 'verified', 'template', 'T1.1', 'hash123', '1.0.0',"
            " '{\"won\":\"P1\"}', '{\"by\":\"tester\"}', 'P5', 1)"
        )
        con.commit()
    finally:
        con.close()


def _run(db: Path) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    env = dict(os.environ)
    env["BID_EVAL_DB_PATH"] = str(db)
    env["PYTHONIOENCODING"] = "utf-8"  # 子进程中文输出按 utf-8 才能被断言
    return subprocess.run(
        [sys.executable, str(MIGRATE)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(REPO),
    )


@pytest.fixture()
def prelaw_db(tmp_path) -> Path:  # noqa: ANN001
    db = tmp_path / "prelaw.db"
    _build_prelaw_db(db)
    return db


def test_migration_expands_checks_and_keeps_s0_data(prelaw_db) -> None:  # noqa: ANN001
    proc = _run(prelaw_db)
    assert proc.returncode == 0, proc.stderr

    con = sqlite3.connect(str(prelaw_db))
    try:
        # 1) 两个 CHECK 含 'law'
        for tname in ("rule_packages", "criteria"):
            ddl = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tname,)
            ).fetchone()[0]
            assert re.search(r"origin IN \([^)]*'law'", ddl), f"{tname} CHECK 未扩 'law'"
        # 2) documents.author 存在
        assert "author" in {r[1] for r in con.execute("PRAGMA table_info(documents)")}
        # 3) S0 列逐列保留 + 数据完好（防"SELECT 漏列"坑）
        cols = {r[1] for r in con.execute("PRAGMA table_info(criteria)")}
        for c in ("origin", "template_code", "template_hash", "library_version",
                  "override_json", "signoff_json", "p_source", "status", "sort_order"):
            assert c in cols, c
        row = con.execute(
            "SELECT code, origin, p_source, status, template_code, template_hash,"
            " library_version, override_json, signoff_json FROM criteria"
        ).fetchone()
        assert row == ("T1.1", "template", "P5", "verified", "T1.1", "hash123",
                       "1.0.0", '{"won":"P1"}', '{"by":"tester"}'), row
        # 4) 行数不变
        assert con.execute("SELECT COUNT(*) FROM rule_packages").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM criteria").fetchone()[0] == 1
        # 5) 索引随重建重放
        idx = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "ix_rule_packages_project_id" in idx
        assert "ix_criteria_package_id" in idx
        # 6) FK 完整
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        con.close()


def test_migration_idempotent(prelaw_db) -> None:  # noqa: ANN001
    assert _run(prelaw_db).returncode == 0
    second = _run(prelaw_db)
    assert second.returncode == 0, second.stderr
    # 三段全部 skip 且不备份不动库
    assert second.stdout.count("已是最新 schema") == 3, second.stdout
    assert "[backup]" not in second.stdout


def test_migration_skips_when_s0_missing(tmp_path) -> None:  # noqa: ANN001
    """pre-S0 库 → 提示先跑 migrate_rule_library.py，exit 1，不写任何东西。"""
    db = tmp_path / "ancient.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE projects (id VARCHAR(64) PRIMARY KEY, name VARCHAR(255))")
    # 极简 criteria：无 origin 也无 needs_signoff（pre-S0 形态）
    con.execute(
        "CREATE TABLE criteria (id INTEGER PRIMARY KEY, package_id INTEGER NOT NULL,"
        " code VARCHAR(64), category VARCHAR(24), name VARCHAR(512), score_type VARCHAR(16),"
        " is_veto BOOLEAN, status VARCHAR(16), sort_order INTEGER)"
    )
    con.commit()
    con.close()

    proc = _run(db)
    assert proc.returncode == 1
    assert "migrate_rule_library" in (proc.stdout + proc.stderr)
