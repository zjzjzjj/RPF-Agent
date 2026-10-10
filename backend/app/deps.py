"""通用依赖与工具：操作者标识、ID 生成。"""
from __future__ import annotations

import random
import string
from datetime import datetime

from fastapi import Header


def get_actor(x_actor: str | None = Header(default=None, alias="X-Actor")) -> str:
    """操作者标识。MVP 无登录，但所有写操作与留痕都真实记录这个值。

    将来接入账号体系（kuncore accounts / JWT 中间件）时，只替换本函数。
    """
    actor = (x_actor or "").strip()
    return actor or "anonymous"


def new_project_id() -> str:
    """PRJ-YYYYMMDD-XXXX：可读、可引用、便于审计。"""
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"PRJ-{datetime.now().strftime('%Y%m%d')}-{suffix}"
