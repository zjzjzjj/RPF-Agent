"""SQLAlchemy 声明基类与跨方言类型别名。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, MetaData
from sqlalchemy.orm import DeclarativeBase

# 命名约定：让约束名可预测（迁移脚本与错误信息都依赖它）。
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# 跨方言 JSON：SQLite 落 TEXT，Postgres 落 JSONB。字典/列表直接存取。
JSONDict = dict[str, Any]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
