"""LLM 抽象层：provider 协议与错误类型。

provider 实现者只负责"一次调用"；缓存、限速、预算、数据分级都在 gateway 里，
这样将来把 provider 换成 kuncore 的 model_router 时，上层零改动。
"""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class LLMError(Exception):
    """模型调用错误。transient=True 表示可重试（超时/限流/5xx）。"""

    def __init__(self, message: str, transient: bool = False):
        self.transient = transient
        super().__init__(message)


class LLMResult(BaseModel):
    text: str
    provider: str
    model: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    cached: bool = False
    latency_ms: int | None = None


class LLMProvider(Protocol):
    """一次模型调用的最小接口。"""

    name: str
    model: str
    host: str  # 端点主机（数据分级策略据此判断是否本地）

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> LLMResult: ...
