"""Execution identity is frozen per run; operational limits remain adjustable."""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from app import config
from app.errors import ValidationFailed

SUPPORTED_EXECUTOR_VERSIONS = (1, 2)
V2_PROMPT_VERSION = "2026-11-06.auto-v2.2"
_BOUND_MANIFEST: ContextVar[dict | None] = ContextVar("review_execution_manifest", default=None)
_FAILURE_POLICY: ContextVar[str] = ContextVar("review_failure_policy", default="stop")


def validate_executor_version(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in SUPPORTED_EXECUTOR_VERSIONS:
        raise ValidationFailed("不支持的自动评审执行器版本，允许版本为 1、2")
    return value


def default_executor_version() -> int:
    raw = os.environ.get("BID_EVAL_EXECUTOR_DEFAULT_VERSION", "1")
    try:
        version = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationFailed("BID_EVAL_EXECUTOR_DEFAULT_VERSION 必须为 1 或 2") from exc
    return validate_executor_version(version)


def run_executor_version(run_or_config) -> int:
    cfg = run_or_config if isinstance(run_or_config, dict) else (run_or_config.config_json or {})
    return validate_executor_version(cfg.get("executor_version", 1))


def prompt_for_executor(version: int, *, frozen_prompt: str | None = None) -> str:
    version = validate_executor_version(version)
    if frozen_prompt:
        return frozen_prompt
    return V2_PROMPT_VERSION if version == 2 else config.llm_prompt_version()


@contextmanager
def bind_run_execution(manifest: dict, executor_version: int | None = None, *, failure_policy: str | None = None):
    """Bind explicitly in each worker: ContextVars do not cross thread pools."""
    version = validate_executor_version(manifest.get("executor_version", 1) if executor_version is None else executor_version)
    bound = deepcopy(manifest)
    bound["executor_version"] = version
    token = _BOUND_MANIFEST.set(bound)
    policy_token = _FAILURE_POLICY.set(failure_policy or _FAILURE_POLICY.get())
    try:
        yield bound
    finally:
        _FAILURE_POLICY.reset(policy_token)
        _BOUND_MANIFEST.reset(token)


def continue_after_errors() -> bool:
    """Operational scheduling policy; never changes frozen rules or valid scores."""
    return _FAILURE_POLICY.get() == "continue"


def current_execution_manifest() -> dict | None:
    return _BOUND_MANIFEST.get()


def execution_prompt_version() -> str:
    bound = _BOUND_MANIFEST.get()
    return str(bound["prompt_version"]) if bound else config.llm_prompt_version()


def execution_config_value(name: str):
    """Read frozen semantic settings, with live credentials and limits outside it."""
    bound = _BOUND_MANIFEST.get()
    if bound and name in (bound.get("scoring_config") or {}):
        return deepcopy(bound["scoring_config"][name])
    return getattr(config, name)()
