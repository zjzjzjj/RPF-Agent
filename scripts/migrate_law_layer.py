#!/usr/bin/env python
"""一次性幂等迁移：法律层（P0）的数据库结构变更。

变更内容（对应 docs/通用评分规则库设计方案.md §4.4）：
- rule_packages  origin CHECK 扩充 'law'（法律通用规则包导入）
- criteria       origin CHECK 扩充 'law'
- documents      新增 author 列（文档作者元数据，串通检测信号源）

SQLite 不支持 ALTER CHECK。本脚本采用「读原 DDL → 文本替换 CHECK → 建新表 →
SELECT * 复制 → 删旧表 → 改名 → 重建索引」的重建流程：SELECT * 天然保留
全部现存列（含 S0 的 origin/p_source 等），不会漏列。
执行前自动备份原库到 <db>.bak-<时间戳>。三段独立幂等，二次运行全部 [skip]。

前置：库需已通过 scripts/migrate_rule_library.py（S0）迁移；未迁移则提示先跑。

用法：
    py -3 scripts/migrate_law_layer.py
    BID_EVAL_DB_PATH=/path/to.db py -3 scripts/migrate_law_layer.py
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _db_path() -> Path:
    import os

    env = os.environ.get("BID_EVAL_DB_PATH")
    if env:
        return Path(env)
    return PROJECT_ROOT / "data" / "bid_eval.db"


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def _table_sql(con: sqlite3.Connection, table: str) -> str | None:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] if row and row[0] else None


def _s0_migrated(con: sqlite3.Connection) -> bool:
    """S0 迁移完成的标志：criteria DDL 已含 needs_signoff 与 origin。"""
    sql = _table_sql(con, "criteria")
    if not sql:
        return False
    return "needs_signoff" in sql and "origin" in sql


def _rebuild_check(con: sqlite3.Connection, table: str) -> bool:
    """给指定表的 origin CHECK 追加 'law'。已包含则 [skip]。返回是否有变更。"""
    tbl_sql = _table_sql(con, table)
    if tbl_sql is None:
        print(f"  [error] 表不存在：{table}")
        return False
    if "'law'" in tbl_sql:
        print(f"  [skip] {table}.origin CHECK 已含 'law'")
        return False

    pattern = re.compile(r"(origin IN \([^)]*\))")

    def _append(m: re.Match[str]) -> str:
        inner = m.group(1)[len("origin IN (") : -1]  # 去掉 origin IN ( ) 两层
        return f"origin IN ({inner}, 'law')"

    new_sql, n = pattern.subn(_append, tbl_sql, count=1)
    if n != 1:
        raise RuntimeError(f"{table} 的 DDL 中找不到 origin CHECK，拒绝继续")

    idx_sqls = [
        r[0]
        for r in con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (table,),
        ).fetchall()
    ]

    new_table = f"{table}_new"
    con.execute(f'DROP TABLE IF EXISTS "{new_table}"')
    # 只替换 CREATE TABLE 后的表名（兼容 CREATE TABLE x 与 CREATE TABLE "x" 两种形态，
    # dev 库 criteria 即为带引号形态）；表内自引用 FK（REFERENCES x）保持原名——
    # 改名回原名后依然正确。
    m = re.match(r'CREATE TABLE\s+"?' + re.escape(table) + r'"?', new_sql)
    if not m:
        raise RuntimeError(f"{table} 的 DDL 开头无法识别，拒绝继续")
    ddl = f"CREATE TABLE {new_table}" + new_sql[m.end() :]
    con.execute(ddl)
    con.execute(f'INSERT INTO "{new_table}" SELECT * FROM "{table}"')
    con.execute(f'DROP TABLE "{table}"')
    con.execute(f'ALTER TABLE "{new_table}" RENAME TO "{table}"')
    for stmt in idx_sqls:
        con.execute(stmt)  # 表级 UNIQUE 随 DDL 重建；显式 CREATE INDEX 需重放
    print(f"  [ok] {table} 表已重建（origin CHECK 扩充 'law'，索引已重建）")
    return True


def _ensure_documents_author(con: sqlite3.Connection) -> bool:
    if "author" in _columns(con, "documents"):
        print("  [skip] documents.author 已存在")
        return False
    con.execute("ALTER TABLE documents ADD COLUMN author VARCHAR(255)")
    print("  [ok] documents.author 已添加")
    return True


def main() -> int:
    db_path = _db_path()
    if not db_path.exists():
        print(f"数据库不存在：{db_path}，无需迁移。")
        return 0

    con = sqlite3.connect(str(db_path))
    try:
        if not _s0_migrated(con):
            print(
                "库尚未完成 S0 迁移（criteria 缺 origin/needs_signoff）。\n"
                "请先运行：py -3 scripts/migrate_rule_library.py",
                file=sys.stderr,
            )
            return 1

        # 预检：三段是否都已迁移（都已迁移则不备份、不进事务）
        pending = []
        for table in ("rule_packages", "criteria"):
            sql = _table_sql(con, table)
            if sql is None or "'law'" not in sql:
                pending.append(table)
        if "author" not in _columns(con, "documents"):
            pending.append("documents.author")
        if not pending:
            for target in ("rule_packages", "criteria", "documents.author"):
                print(f"  [skip] {target} 已是最新 schema")
            print("无需迁移。")
            return 0

        # WAL 模式下最近写入在 -wal 文件里，先 checkpoint 折入主库再备份，
        # 否则 copy2 只拷 .db 会丢最近数据（备份必须先于任何事务）。
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            print("[checkpoint] WAL 已并入主库")
        except sqlite3.OperationalError:
            print("[checkpoint] 跳过（数据库被占用，按现状备份）", file=sys.stderr)
        con.commit()

        backup = db_path.with_name(f"{db_path.name}.bak-law-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(db_path, backup)
        print(f"[backup] 已备份到 {backup}")

        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("BEGIN IMMEDIATE")
        _rebuild_check(con, "rule_packages")
        _rebuild_check(con, "criteria")
        _ensure_documents_author(con)
        con.commit()
        print("迁移完成。")
        return 0
    except Exception as exc:  # noqa: BLE001
        try:
            con.rollback()
        except Exception:  # noqa: BLE001
            pass
        print(f"迁移失败，已回滚：{exc}", file=sys.stderr)
        return 1
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
