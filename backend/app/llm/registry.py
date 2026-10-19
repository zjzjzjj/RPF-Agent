"""provider 注册与选择。"""
from __future__ import annotations

from app import config
from app.errors import LLMNotConfigured
from app.llm.base import LLMProvider

# 这些名字都映射到 OpenAI 兼容客户端（硅基流动 / DeepSeek 官方 / 阿里云百炼等）
_OPENAI_COMPAT_ALIASES = ("openai", "openai_compat", "siliconflow", "deepseek", "qwen", "glm", "kimi")


def get_provider(name: str | None = None, model: str | None = None) -> LLMProvider:
    """按名称/配置构造 provider；model 覆盖默认模型（前端可选模型）。

    未配置 BID_EVAL_LLM_PROVIDER 时抛 LLMNotConfigured（fail-fast，不静默降级）。
    未来迁入 kuncore 时只改这里。
    """
    name = (name or config.llm_provider() or "").strip().lower()
    if not name:
        raise LLMNotConfigured(
            "未配置 LLM provider：请设置环境变量 BID_EVAL_LLM_PROVIDER"
            f"（可选值：{'、'.join(_OPENAI_COMPAT_ALIASES)}），或在仓库根目录 .env 中配置"
        )
    if name in _OPENAI_COMPAT_ALIASES:
        from app.llm.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(model=model)
    raise ValueError(f"未知的 LLM provider：{name}（可选值：{'、'.join(_OPENAI_COMPAT_ALIASES)}）")
