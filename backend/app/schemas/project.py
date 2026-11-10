"""项目契约。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ProjectStatus = Literal[
    "draft",
    "tender_parsed",
    "rules_draft",
    "rules_frozen",
    "bids_parsed",
    "evaluating",
    "reviewing",
    "completed",
    "archived",
]


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    project_code: str | None = None
    purchaser: str | None = None
    max_price: Decimal | None = None
    method: str = "comprehensive"

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        # min_length 按字符数挡不住纯空白（"  " 长度 2 会过），这里补上并归一去首尾空白
        v = v.strip()
        if not v:
            raise ValueError("项目名称不能为空白")
        return v


class ProjectPatch(BaseModel):
    # 与 ProjectCreate 同口径：None=不改，给了就必须非空白（重命名等入口防清空名称）
    name: str | None = Field(default=None, min_length=1, max_length=255)
    project_code: str | None = None
    purchaser: str | None = None
    max_price: Decimal | None = None
    status: ProjectStatus | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("项目名称不能为空白")
        return v


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    project_code: str | None
    purchaser: str | None
    method: str
    status: str
    max_price: Decimal | None
    currency: str
    created_at: datetime
    updated_at: datetime


class StageState(BaseModel):
    """流水线一个阶段的聚合状态（工作台首屏用）。"""

    key: str
    label: str
    state: Literal["done", "active", "blocked", "pending"]
    detail: str = ""
    todo_count: int = 0
    # 可选阶段（如已有模板规则时的招标文件）：pending 不阻塞"下一步"引导
    optional: bool = False


class WorkbenchOut(BaseModel):
    project: ProjectOut
    stages: list[StageState]
    blockers: list[str] = []
