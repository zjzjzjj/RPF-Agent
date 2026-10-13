#!/usr/bin/env python
"""一次性幂等迁移：全自动模式（review_status 扩展 'auto_confirmed'）。

变更内容：
- score_records.review_status CHECK 扩充 'auto_confirmed'
  （全自动模式 BID_EVAL_AUTO_MODE=1 下 LLM 裁决/确定性判定的终态，见
  docs/decisions/0003-full-auto-mode.md）

SQLite 不支持 ALTER CHECK。本脚本采用「读原 DDL → 文本替换 CHECK → 建新表 →
SELECT * 复制 → 删旧表 → 改名 → 重建索引」的重建流程。执行前自动备份原库到
<db>.bak-auto-<时间戳>。幂等，二次运行 [skip]。

用法：
    py -3 scripts/migrate_auto_review.py
    BID_EVAL_DB_PATH=/path/to.db py -3 scripts/migrate_auto_review.py
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


def _table_sql(con: sqlite3.Connection, table: str) -> str | None:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] if row and row[0] else None


def _rebuild_review_status_check(con: sqlite3.Connection, table: str = "score_records") -> bool:
    """给 review_status CHECK 追加 'auto_confirmed'。已包含则 [skip]。"""
    tbl_sql = _table_sql(con, table)
    if tbl_sql is None:
        print(f"  [error] 表不存在：{table}")
        return False
    if "'auto_confirmed'" in tbl_sql:
        print(f"  [skip] {table}.review_status CHECK 已含 'auto_confirmed'")
        return False

    pattern = re.compile(r"(review_status IN \([^)]*\))")

    def _append(m: re.Match[str]) -> str:
        inner = m.group(1)[len("review_status IN ("):-1]
        return f"review_status IN ({inner},'auto_confirmed')"

    new_sql, n = pattern.subn(_append, tbl_sql, count=1)
    if n != 1:
        raise RuntimeError(f"{table} 的 DDL 中找不到 review_status CHECK，拒绝继续")

    idx_sqls = [
        r[0]
        for r in con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (table,),
        ).fetchall()
    ]

    new_table = f"{table}_new"
    con.execute(f'DROP TABLE IF EXISTS "{new_table}"')
    m = re.match(r'CREATE TABLE\s+"?' + re.escape(table) + r'"?', new_sql)
    if not m:
        raise RuntimeError(f"{table} 的 DDL 开头无法识别，拒绝继续")
    ddl = f"CREATE TABLE {new_table}" + new_sql[m.end():]
    con.execute(ddl)
    con.execute(f'INSERT INTO "{new_table}" SELECT * FROM "{table}"')
    con.execute(f'DROP TABLE "{table}"')
    con.execute(f'ALTER TABLE "{new_table}" RENAME TO "{table}"')
    for stmt in idx_sqls:
        con.execute(stmt)  # 表级 UNIQUE 随 DDL 重建；显式 CREATE INDEX 需重放
    print(f"  [ok] {table} 表已重建（review_status CHECK 扩充 'auto_confirmed'，索引已重建）")
    return True


def main() -> int:
    db_path = _db_path()
    if not db_path.exists():
        print(f"数据库不存在：{db_path}，无需迁移（新建库由 init_db 直接建出目标 schema）。")
        return 0

    con = sqlite3.connect(str(db_path))
    try:
        sql = _table_sql(con, "score_records")
        if sql is None:
            print("score_records 表不存在（空库），无需迁移。")
            return 0
        if "'auto_confirmed'" in sql:
            print("  [skip] score_records.review_status CHECK 已含 'auto_confirmed'，无需迁移。")
            return 0

        # WAL 模式下最近写入在 -wal 文件里，先 checkpoint 折入主库再备份
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            print("[checkpoint] WAL 已并入主库")
        except sqlite3.OperationalError:
            print("[checkpoint] 跳过（数据库被占用，按现状备份）", file=sys.stderr)
        con.commit()

        backup = db_path.with_name(f"{db_path.name}.bak-auto-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(db_path, backup)
        print(f"[backup] 已备份到 {backup}")

        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("BEGIN IMMEDIATE")
        _rebuild_review_status_check(con)
        con.commit()
        print("迁移完成。全自动模式（BID_EVAL_AUTO_MODE，默认开）现已可用。")
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
