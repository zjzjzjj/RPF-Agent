#!/usr/bin/env python
"""一次性幂等迁移：criteria 增"主/客观分"列值两列（显示口径二轴制，2026-11-23）。

变更内容：
- criteria.score_column        'subjective'|'objective'|NULL（NULL=评标文件无该列，显示"—"）
- criteria.score_column_source 'file'|'llm_copy'|'inferred'|NULL（inferred 不作显示来源）

纯增量 ALTER TABLE ADD COLUMN（不动任何已有数据/约束；SQLite 对新增列不附加
CHECK，由应用层与新建库 DDL 双保险）。执行前自动备份原库到 <db>.bak-scorecolumn-<ts>。
幂等：列已存在则 [skip]。

用法：
    py -3 scripts/migrate_score_column.py
    BID_EVAL_DB_PATH=/path/to.db py -3 scripts/migrate_score_column.py
"""
from __future__ import annotations

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


def main() -> int:
    db = _db_path()
    if not db.exists():
        print(f"[error] 数据库不存在：{db}")
        return 1
    con = sqlite3.connect(db)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(criteria)")}
        todo = [c for c in ("score_column", "score_column_source") if c not in cols]
        if not todo:
            print("[skip] criteria.score_column/score_column_source 已存在")
            return 0
        backup = db.with_name(f"{db.name}.bak-scorecolumn-{datetime.now():%Y%m%d%H%M%S}")
        con.close()
        shutil.copy2(db, backup)
        print(f"[backup] {backup}")
        con = sqlite3.connect(db)
        for col in todo:
            con.execute(f"ALTER TABLE criteria ADD COLUMN {col} TEXT")
        con.commit()
        print(f"[ok] 已添加列：{', '.join(todo)}（存量行保持 NULL=无列，显示—）")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
