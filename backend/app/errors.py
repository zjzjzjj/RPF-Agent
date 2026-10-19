"""统一异常与错误信封。

错误信封形状：{"code": "...", "message": "...", "detail": {...}}
`code` 是稳定机器码（前端可分支），`message` 是面向用户的中文说明。
"""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    """业务异常基类。子类只声明 code/http_status 与默认消息。"""

    code: str = "INTERNAL"
    http_status: int = 500
    default_message: str = "服务器内部错误"

    def __init__(self, message: str | None = None, detail: dict[str, Any] | None = None):
        self.message = message or self.default_message
        self.detail = detail or {}
        super().__init__(self.message)


class NotFound(AppError):
    code = "NOT_FOUND"
    http_status = 404
    default_message = "资源不存在"


class Conflict(AppError):
    """状态冲突：如对已冻结规则包执行写入。"""

    code = "CONFLICT"
    http_status = 409
    default_message = "当前状态不允许该操作"


class FrozenPackageError(Conflict):
    code = "PACKAGE_FROZEN"
    default_message = "规则包已冻结，不可修改；请创建新版本"


class ValidationFailed(AppError):
    """前置条件未满足：如规则包未通过校验、报价未确认。"""

    code = "VALIDATION_FAILED"
    http_status = 422
    default_message = "前置条件未满足"

    def __init__(self, message: str | None = None, blockers: list[dict[str, Any]] | None = None):
        super().__init__(message, detail={"blockers": blockers or []})


class DocumentRejected(AppError):
    """文档在进入解析前/解析中被拒绝。code 使用 config 中的 REJECT_* 稳定码。"""

    code = "DOCUMENT_REJECTED"
    http_status = 422
    default_message = "文档无法解析"

    def __init__(self, reject_code: str, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message, detail)
        self.code = reject_code


class LLMBudgetExceeded(AppError):
    code = "LLM_BUDGET_EXCEEDED"
    http_status = 429
    default_message = "已达到模型调用预算上限，本次运行中止"


class LLMNotConfigured(AppError):
    """未配置 LLM provider（BID_EVAL_LLM_PROVIDER）——fail-fast，不静默降级。

    注意：不得继承 llm.base.LLMError——compile 的重试循环只对 LLMError 退避
    重试，配置错误应当立即失败并给出可操作提示，而不是伪装成模型故障。
    """

    code = "LLM_NOT_CONFIGURED"
    http_status = 500
    default_message = "未配置 LLM provider"


class DataClassDenied(AppError):
    """数据分级策略拒绝：非本地端点不允许接收 confidential 数据。"""

    code = "DATA_CLASS_DENIED"
    http_status = 403
    default_message = "数据分级策略拒绝了本次模型调用（涉密数据只能发往本地端点）"
