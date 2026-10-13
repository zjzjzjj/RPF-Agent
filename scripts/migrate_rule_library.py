#!/usr/bin/env python
"""一次性幂等迁移：通用评分规则库（S0）的数据库结构变更。

变更内容（对应 docs/通用评分规则库设计方案.md §4.1）：
- rule_packages  新增 origin / template_id / template_hash / library_version
- criteria       新增 origin / template_code / template_hash / library_version /
                 override_json / signoff_json / p_source
                 status CHECK 扩充 needs_signoff / retired

SQLite 不支持 ALTER CHECK，criteria 采用"建新表 → 复制 → 删旧表 → 改名"的标准重建流程。
执行前自动备份原库到 <db>.bak-<时间戳>。脚本幂等：已迁移则直接退出。

用法：
    python scripts/migrate_rule_library.py            # 迁移默认 data/bid_eval.db
    BID_EVAL_DB_PATH=/path/to.db python scripts/migrate_rule_library.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _db_path() -> Path:
    env = os.environ.get("BID_EVAL_DB_PATH")
    if env:
        return Path(env)
    return PROJECT_ROOT / "data" / "bid_eval.db"


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def _already_migrated(con: sqlite3.Connection) -> bool:
    """criteria 的 status CHECK 是否已包含 needs_signoff。"""
    rows = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='criteria'"
    ).fetchall()
    if not rows or not rows[0][0]:
        return False
    return "needs_signoff" in rows[0][0] and "origin" in rows[0][0]


def _migrate_rule_packages(con: sqlite3.Connection) -> None:
    cols = _columns(con, "rule_packages")
    ddl = [
        (
            "origin",
            "ALTER TABLE rule_packages ADD COLUMN origin VARCHAR(16) NOT NULL DEFAULT 'document' "
            "CHECK (origin IN ('document','template','merged'))",
        ),
        ("template_id", "ALTER TABLE rule_packages ADD COLUMN template_id VARCHAR(64)"),
        ("template_hash", "ALTER TABLE rule_packages ADD COLUMN template_hash VARCHAR(32)"),
        ("library_version", "ALTER TABLE rule_packages ADD COLUMN library_version VARCHAR(32)"),
        ("merge_target_id", "ALTER TABLE rule_packages ADD COLUMN merge_target_id INTEGER REFERENCES rule_packages(id)"),
    ]
    for name, stmt in ddl:
        if name not in cols:
            con.execute(stmt)
            print(f"  [ok] rule_packages.{name} 已添加")
        else:
            print(f"  [skip] rule_packages.{name} 已存在")


def _ensure_override_records(con: sqlite3.Connection) -> None:
    """S1：override_records 表（覆盖审计）。"""
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='override_records'"
    ).fetchall()
    if rows:
        print("  [skip] override_records 已存在")
        return
    con.execute(
        """
        CREATE TABLE override_records (
            id INTEGER NOT NULL,
            package_id INTEGER NOT NULL,
            merge_package_id INTEGER,
            action VARCHAR(16) NOT NULL,
            template_criterion_id INTEGER,
            document_criterion_id INTEGER,
            match_method VARCHAR(16),
            similarity NUMERIC(6, 4),
            won_source VARCHAR(2),
            lost_source VARCHAR(2),
            document_id INTEGER,
            document_node_id VARCHAR(512),
            document_page INTEGER,
            quote TEXT,
            confirmed_by VARCHAR(64),
            reason TEXT,
            created_at DATETIME NOT NULL,
            CONSTRAINT pk_override_records PRIMARY KEY (id),
            CONSTRAINT fk_override_records_package_id_rule_packages FOREIGN KEY(package_id) REFERENCES rule_packages (id),
            CONSTRAINT fk_override_records_merge_package_id_rule_packages FOREIGN KEY(merge_package_id) REFERENCES rule_packages (id),
            CONSTRAINT fk_override_records_template_criterion_id_criteria FOREIGN KEY(template_criterion_id) REFERENCES criteria (id),
            CONSTRAINT fk_override_records_document_criterion_id_criteria FOREIGN KEY(document_criterion_id) REFERENCES criteria (id),
            CONSTRAINT fk_override_records_document_id_documents FOREIGN KEY(document_id) REFERENCES documents (id)
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS ix_override_records_package_id ON override_records (package_id)"
    )
    print("  [ok] override_records 表已创建")


def _rebuild_criteria(con: sqlite3.Connection) -> None:
    """重建 criteria 表：带新列 + 扩充后的 status CHECK（SQLite 无法 ALTER CHECK）。"""
    new_sql = """
CREATE TABLE criteria_new (
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
    origin VARCHAR(16) NOT NULL DEFAULT 'document',
    template_code VARCHAR(64),
    template_hash VARCHAR(32),
    library_version VARCHAR(32),
    override_json JSON,
    signoff_json JSON,
    p_source VARCHAR(2),
    status VARCHAR(16) NOT NULL,
    edit_json JSON,
    confidence FLOAT,
    sort_order INTEGER NOT NULL,
    CONSTRAINT pk_criteria PRIMARY KEY (id),
    CONSTRAINT uq_criteria_package_code UNIQUE (package_id, code),
    CONSTRAINT ck_criteria_category_valid CHECK (category IN ('qualification','compliance','price','technical','commercial')),
    CONSTRAINT ck_criteria_score_type_valid CHECK (score_type IN ('veto','formula','count','match','subjective')),
    CONSTRAINT ck_criteria_status_valid CHECK (status IN ('verified','needs_input','ambiguous','gap','conflict','invalid','needs_signoff','retired')),
    CONSTRAINT ck_criteria_origin_valid CHECK (origin IN ('template','document','manual')),
    CONSTRAINT fk_criteria_package_id_rule_packages FOREIGN KEY(package_id) REFERENCES rule_packages (id) ON DELETE CASCADE
)
"""
    con.execute("DROP TABLE IF EXISTS criteria_new")
    con.execute(new_sql)
    con.execute(
        """
        INSERT INTO criteria_new (
            id, package_id, code, category, name, description, max_score, weight,
            score_type, is_veto, veto_consequence, required_evidence, source_node_id,
            source_page, source_quote, status, edit_json, confidence, sort_order
        )
        SELECT id, package_id, code, category, name, description, max_score, weight,
               score_type, is_veto, veto_consequence, required_evidence, source_node_id,
               source_page, source_quote, status, edit_json, confidence, sort_order
        FROM criteria
        """
    )
    con.execute("DROP TABLE criteria")
    con.execute("ALTER TABLE criteria_new RENAME TO criteria")
    con.execute(
        "CREATE INDEX IF NOT EXISTS ix_criteria_package_id ON criteria (package_id)"
    )
    print("  [ok] criteria 表已重建（新列 + status CHECK 扩充）")


def main() -> int:
    db_path = _db_path()
    if not db_path.exists():
        print(f"数据库不存在：{db_path}，无需迁移。")
        return 0

    backup = db_path.with_name(f"{db_path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(db_path, backup)
    print(f"[backup] 已备份到 {backup}")

    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("BEGIN IMMEDIATE")
        if not _already_migrated(con):
            _rebuild_criteria(con)
        else:
            print("  [skip] criteria 表已是最新 schema")
        _migrate_rule_packages(con)
        _ensure_override_records(con)
        con.commit()
        print("迁移完成。")
        return 0
    except Exception as exc:  # noqa: BLE001
        con.rollback()
        print(f"迁移失败，已回滚：{exc}", file=sys.stderr)
        return 1
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())