"""迁移脚本回归测试：旧 schema 的 SQLite 库 → 新列 + status CHECK 扩充 + 数据保留。

用子进程跑 scripts/migrate_rule_library.py（设置 BID_EVAL_DB_PATH 指向临时旧库）。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIGRATE = REPO / "scripts" / "migrate_rule_library.py"

# 旧 schema（与迁移前 dev 库一致：criteria 无新列、status CHECK 无 needs_signoff）
OLD_CRITERIA_SQL = """
CREATE TABLE criteria (
    id INTEGER NOT NULL,
    package_id INTEGER NOT NULL,
    code VARCHAR(64) NOT NULL,
    category VARCHAR(24) NOT NULL,
    name VARCHAR(512) NOT NULL,
    description TEXT,
    max_score NUMERIC(10, 2),
    weight NUMERIC(10, 2),
    score_type VARCHAR(16) NOT NULL,
    is_veto BOOLEAN NOT NULL,
    veto_consequence VARCHAR(24),
    required_evidence TEXT,
    source_node_id VARCHAR(512),
    source_page INTEGER,
    source_quote TEXT,
    status VARCHAR(16) NOT NULL,
    edit_json JSON,
    confidence FLOAT,
    sort_order INTEGER NOT NULL,
    CONSTRAINT pk_criteria PRIMARY KEY (id),
    CONSTRAINT uq_criteria_package_code UNIQUE (package_id, code),
    CONSTRAINT ck_criteria_category_valid CHECK (category IN ('qualification','compliance','price','technical','commercial')),
    CONSTRAINT ck_criteria_score_type_valid CHECK (score_type IN ('veto','formula','count','match','subjective')),
    CONSTRAINT ck_criteria_status_valid CHECK (status IN ('verified','needs_input','ambiguous','gap','conflict','invalid')),
    CONSTRAINT fk_criteria_package_id_rule_packages FOREIGN KEY(package_id) REFERENCES rule_packages (id) ON DELETE CASCADE
)
"""
OLD_PACKAGES_SQL = """
CREATE TABLE rule_packages (
    id INTEGER NOT NULL,
    project_id VARCHAR(64) NOT NULL,
    version INTEGER NOT NULL,
    status VARCHAR(16) NOT NULL,
    content_hash VARCHAR(32),
    snapshot_json JSON,
    parent_version_id INTEGER,
    source_document_id INTEGER,
    compiled_by VARCHAR(64),
    model VARCHAR(128),
    prompt_version VARCHAR(64),
    created_by VARCHAR(64) NOT NULL,
    confirmed_by VARCHAR(64),
    confirmed_at DATETIME,
    frozen_by VARCHAR(64),
    frozen_at DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT pk_rule_packages PRIMARY KEY (id),
    CONSTRAINT uq_rule_packages_project_version UNIQUE (project_id, version),
    CONSTRAINT ck_rule_packages_status_valid CHECK (status IN ('draft','confirmed','frozen','superseded')),
    CONSTRAINT fk_rule_packages_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id),
    CONSTRAINT fk_rule_packages_parent_version_id_rule_packages FOREIGN KEY(parent_version_id) REFERENCES rule_packages (id),
    CONSTRAINT fk_rule_packages_source_document_id_documents FOREIGN KEY(source_document_id) REFERENCES documents (id)
)
"""


def _make_old_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE projects (id VARCHAR(64) PRIMARY KEY, name VARCHAR(255))")
    con.execute(OLD_PACKAGES_SQL)
    con.execute(OLD_CRITERIA_SQL)
    con.execute("INSERT INTO projects (id, name) VALUES ('PRJ-OLD-0001', '旧项目')")
    con.execute(
        "INSERT INTO rule_packages (id, project_id, version, status, created_by, created_at, updated_at) "
        "VALUES (1, 'PRJ-OLD-0001', 1, 'frozen', 'system', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
    )
    con.execute(
        "INSERT INTO criteria (id, package_id, code, category, name, max_score, score_type,"
        " is_veto, status, sort_order) "
        "VALUES (1, 1, '1.1', 'price', '报价分', 100, 'formula', 0, 'verified', 1)"
    )
    con.commit()
    con.close()


def test_migration_script(tmp_path) -> None:  # noqa: ANN001
    db = tmp_path / "old.db"
    _make_old_db(db)

    env = dict(os.environ)
    env["BID_EVAL_DB_PATH"] = str(db)
    # Windows 管道下子进程 stdout 默认按 locale（GBK）编码，而断言按 utf-8 解码——
    # 强制子进程 UTF-8 输出，测试在中文 Windows 上才是确定性的。
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(MIGRATE)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(REPO),
    )
    assert proc.returncode == 0, proc.stderr

    con = sqlite3.connect(str(db))
    pkg_cols = {r[1] for r in con.execute("PRAGMA table_info(rule_packages)")}
    crit_cols = {r[1] for r in con.execute("PRAGMA table_info(criteria)")}
    for col in ("origin", "template_id", "template_hash", "library_version"):
        assert col in pkg_cols, col
    for col in ("origin", "template_code", "template_hash", "library_version",
                "override_json", "signoff_json", "p_source"):
        assert col in crit_cols, col
    ddl = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='criteria'"
    ).fetchone()[0]
    assert "needs_signoff" in ddl
    assert "retired" in ddl
    assert "origin" in ddl
    # 数据保留
    assert con.execute("SELECT COUNT(*) FROM criteria").fetchone()[0] == 1
    row = con.execute("SELECT id, code, status FROM criteria").fetchone()
    assert row == (1, "1.1", "verified")
    con.close()

    # 幂等：再次执行应直接退出成功，且不重建 criteria（criteria 已是最新 schema）
    proc = subprocess.run(
        [sys.executable, str(MIGRATE)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,  # 复用上方已设 PYTHONIOENCODING=utf-8 的 env
        cwd=str(REPO),
    )
    assert proc.returncode == 0, proc.stderr
    assert "criteria 表已是最新 schema" in proc.stdout
    assert "override_records 已存在" in proc.stdout