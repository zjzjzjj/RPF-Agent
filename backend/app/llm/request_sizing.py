"""Check the complete request, never just the material excerpt.

UTF-8 byte count is a conservative text token upper bound. Image reservations
are operational allowances, not a claim about a vendor's tokenizer. A verified
context size must be configured before a V2 performance gate can be accepted.
"""
from __future__ import annotations

import json
import os

from app.llm.base import LLMError
from app.services.execution_version import execution_config_value


class ReviewContextExceeded(LLMError):
    pass


def request_usage(system: str, user: str, images: list[str] | None = None) -> dict:
    configured = int(execution_config_value("llm_context_tokens") or 0)
    output = int(execution_config_value("llm_default_max_tokens") or 8192)
    text = json.dumps([{"role": "system", "content": system},
                       {"role": "user", "content": user}], ensure_ascii=False)
    image_reserve = max(1024, int(os.environ.get("BID_EVAL_VISION_IMAGE_CONTEXT_RESERVE", "16384")))
    return {"text_chars": len(text), "text_token_upper_bound": len(text.encode("utf-8")),
            "image_count": len(images or []), "image_token_reserve": image_reserve * len(images or []),
            "output_tokens": output, "context_tokens": configured,
            "context_verified": configured > 0}


def assert_request_fits(system: str, user: str, images: list[str] | None = None) -> dict:
    usage = request_usage(system, user, images)
    chars = max(8000, int(os.environ.get("BID_EVAL_REVIEW_REQUEST_CHARS", "64000")))
    if usage["text_chars"] > chars or usage["image_count"] > 4:
        raise ReviewContextExceeded("完整评审请求超过安全批次预算，必须继续拆分材料或事实组")
    if usage["context_tokens"] and (
        usage["text_token_upper_bound"] + usage["image_token_reserve"] + usage["output_tokens"] + 1024
        > usage["context_tokens"]
    ):
        raise ReviewContextExceeded("完整评审请求超过已配置模型上下文，禁止删除证据或压低合法输出预算")
    return usage
