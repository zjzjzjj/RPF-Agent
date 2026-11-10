#!/usr/bin/env python
"""Explicit additive executor-V2 migration; safe to rerun and retain on rollback."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))


def migrate(database: Path, *, check_only: bool = False) -> Path | None:
    from sqlalchemy import create_engine
    from app.db.execution_schema import upgrade_execution_schema, validate_execution_schema

    database = database.resolve()
    if not database.is_file():
        raise RuntimeError("目标数据库不存在；请先显式初始化基础数据库")
    engine = create_engine("sqlite:///" + database.as_posix())
    try:
        if check_only:
            validate_execution_schema(engine)
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = database.with_name(database.name + ".before-executor-v2-" + stamp)
        # SQLite online backup includes committed WAL pages; never copy a live .db alone.
        with sqlite3.connect("file:" + database.as_posix() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(str(backup)) as target:
                source.backup(target)
        upgrade_execution_schema(engine)
        validate_execution_schema(engine)
        return backup
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(os.environ.get("BID_EVAL_DB_PATH", REPO_ROOT / "data" / "bid_eval.db")))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        backup = migrate(args.database, check_only=args.check)
        print("执行器 V2 数据结构校验通过" + (f"；备份：{backup}" if backup else ""))
        return 0
    except Exception as exc:
        print("执行器 V2 数据结构处理失败：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
