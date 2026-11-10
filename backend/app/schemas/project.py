"""项目契约。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


class ProjectPatch(BaseModel):
    name: str | None = None
    project_code: str | None = None
    purchaser: str | None = None
    max_price: Decimal | None = None
    status: ProjectStatus | None = None


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
