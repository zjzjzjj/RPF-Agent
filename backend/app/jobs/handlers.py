"""任务处理器注册表。

handler 签名：`handler(ctx: JobContext, payload: dict) -> dict | None`
- 必须周期性调用 `ctx.advance(...)`（内含取消检查），让长任务可取消、可观测。
- 返回值写入 jobs.result_json。

具体处理器在各自里程碑接入：
- parse_document  → M1（解析层）
- compile_rules   → M2（规则编译）
- evaluate        → M3/M4（评分运行）
- export_report   → M6
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.errors import AppError
from app.jobs.queue import JobContext

Handler = Callable[[JobContext, dict[str, Any]], dict[str, Any] | None]

_HANDLERS: dict[str, Handler] = {}


def register(kind: str) -> Callable[[Handler], Handler]:
    def _decorator(fn: Handler) -> Handler:
        _HANDLERS[kind] = fn
        return fn

    return _decorator


def get_handler(kind: str) -> Handler:
    if kind not in _HANDLERS:
        raise AppError(f"未知任务类型：{kind}", detail={"kind": kind})
    return _HANDLERS[kind]


def registered_kinds() -> list[str]:
    return sorted(_HANDLERS)


# 导入各处理器模块以触发注册（新增处理器时在此追加）
def _load_all() -> None:
    from app.rules import compiler  # noqa: F401  (compile_rules)
    from app.services import (  # noqa: F401
        bidder_service,
        ingest_service,  # (parse_document)
        rule_service,
        scoring_service,  # (evaluate)
    )


_load_all()
