# -*- coding: utf-8 -*-
"""Public review/report output sanitization.

Audit JSON keeps the full failure trail. Public output must expose only
user-facing review state and must never leak internal evidence aliases,
exception classes, hashes, or legacy operational wording.
"""
from __future__ import annotations

import re
from typing import Any

_INTERNAL_KEYS = {
    "source_id", "evidence_id", "validation_history",
    "technical_detail", "rejected_evidence", "error_code", "sha256",
    "image_sha256", "response_sha256", "input_signature",
}
_INTERNAL_ID_PATTERNS = (
    re.compile(r"\bb\d+e\d+\b"),
    re.compile(r"\bd\d+p\d+_[0-9a-f]{8,}\b", re.I),
    re.compile(r"\bR(?=[0-9A-Z]*\d)[0-9A-Z]+\b"),
    re.compile(r"\b[0-9a-f]{32,}\b", re.I),
)
_PUBLIC_REPLACEMENTS = (
    ("兜底分（非评审结论）", "暂定分（待人工确认）"),
    ("兜底暂定分", "暂定分"),
    ("兜底分", "暂定分"),
    ("非评审结论", "待人工确认"),
    ("未深评", "待确认"),
    ("技术故障", "系统复核异常"),
    ("本批原页证据定向补抽", "材料复核"),
    ("本批原页证据定向补评", "材料复核"),
    ("记录后跳过", "转人工复核"),
    ("review_attempts_exhausted", "评审待复核"),
    ("EvidenceBindingError", "证据绑定异常"),
    ("ValidationError", "输出格式复核"),
    ("LLMError", "系统复核异常"),
)


def sanitize_public_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    result = value
    for pattern in _INTERNAL_ID_PATTERNS:
        result = pattern.sub("[内部编号已省略]", result)
    for old, new in _PUBLIC_REPLACEMENTS:
        result = result.replace(old, new)
    return result


def sanitize_public_object(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): sanitize_public_object(item)
            for key, item in value.items()
            if str(key) not in _INTERNAL_KEYS
        }
    if isinstance(value, list):
        return [sanitize_public_object(item) for item in value]
    return sanitize_public_text(value)
