"""LLM 网关：缓存 → 数据分级 → 限速 → 预算 → 调用 → 记账。

数据分级是合规刚需：confidential（真实投标文件）只允许发往本地端点。
审计只记录元数据（provider/model/prompt_version/tokens/latency/cache_hit），不记全文。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app import config
from app.errors import DataClassDenied, LLMBudgetExceeded
from app.llm.base import LLMError, LLMProvider, LLMResult

log = logging.getLogger("bid_eval.llm")

T = TypeVar("T", bound=BaseModel)

_CACHE_LIMIT = 512

# 进程级预算计数（跨 Gateway 实例共享、按天滚动）
_BUDGET_LOCK = threading.Lock()
_BUDGET_STATE: dict[str, object] = {"day": "", "calls": 0}

# 涉密数据发往外部端点的一次性告警去重（provider/host）；跨实例共享
_REMOTE_CONFIDENTIAL_WARNED_LOCK = threading.Lock()
_REMOTE_CONFIDENTIAL_WARNED: set[str] = set()


_VALID_ESCAPES = frozenset('"\\/bfnrt')
_HEX = frozenset("0123456789abcdefABCDEF")


def _repair_invalid_escapes(text: str) -> tuple[str, int]:
    """把字符串内的**非法反斜杠转义**改成字面反斜杠（`\\X`）。返回 (新文本, 修复数)。

    8B 模型输出的 JSON 字符串里常出现 `"C:\\Users"`、`"\\（中文"`、`"\\u-2026"`
    这类非法转义，`json.loads` 会整段拒绝（Invalid \\escape），导致整块规则抽取
    失败（2026-10-21 线上实证）。合法转义（`\\n` `\\\\` `\\"` `\\u4e2d`）原样保留，
    引号外的字符一律不动——只把"本来想表达字面反斜杠"的地方补齐，不删任何字符。
    """
    out: list[str] = []
    in_str = False
    fixed = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if not in_str:
            if ch == '"':
                in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_str = False
            out.append(ch)
            i += 1
            continue
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1] if i + 1 < n else ""
        if nxt and nxt in _VALID_ESCAPES:
            out.append(ch)
            out.append(nxt)
            i += 2
            continue
        if nxt == "u" and i + 5 < n and all(c in _HEX for c in text[i + 2 : i + 6]):
            out.append(text[i : i + 6])
            i += 6
            continue
        # 非法转义：保留反斜杠字面量（"\\" + 该字符），继续处理该字符本身
        out.append("\\\\")
        fixed += 1
        i += 1
    return "".join(out), fixed


def _strip_trailing_commas(text: str) -> tuple[str, int]:
    """去掉字符串外、紧跟 `}` / `]` 的尾逗号（`{"a":[1,2,],}` 这类）。"""
    out: list[str] = []
    in_str = False
    fixed = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                fixed += 1
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out), fixed


def _loads_lenient(candidate: str) -> dict | None:
    """strict=True / strict=False 各试一次（后者放行字符串内的裸换行等控制字符）。"""
    for strict in (True, False):
        try:
            return json.loads(candidate, strict=strict)
        except json.JSONDecodeError:
            continue
    return None


def extract_json(text: str) -> dict:
    """从模型输出中提取 JSON 对象：剥围栏 + 配平花括号扫描 + 分级容错解析。

    模型输出常见坏形态：```json 围栏、前后缀说明文字、截断、**字符串内非法转义**
    （`\\（`、Windows 路径、`\\u` 后非 4 位十六进制）、字符串内裸换行、尾逗号。
    前三种可容错修复；截断与"字符串内未转义的双引号"无法安全修复，仍抛 LLMError
    （由上层降级，绝不猜）。
    """
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    start = s.find("{")
    if start < 0:
        # 不回显模型原文：招标/投标内容属 confidential，异常会进日志、job.error
        # 并经 API 返回前端（2026-10-21 合规复核）
        raise LLMError(f"模型输出中找不到 JSON（共 {len(text)} 字符）")
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = s[start : i + 1]
                return _parse_candidate(candidate)
    raise LLMError("模型输出 JSON 不完整（可能被截断）")


def _parse_candidate(candidate: str) -> dict:
    """分级解析单个 JSON 对象文本；全部失败才抛 LLMError。"""
    data = _loads_lenient(candidate)
    if data is not None:
        return data

    repaired, escapes = _repair_invalid_escapes(candidate)
    if escapes:
        data = _loads_lenient(repaired)
        if data is not None:
            # 只记数量，不落原文（证据/招标原文属 confidential，日志不得出现）
            log.warning("模型输出 JSON 含 %d 处非法转义，已按字面量自动修复", escapes)
            return data

    stripped, commas = _strip_trailing_commas(repaired)
    if commas:
        data = _loads_lenient(stripped)
        if data is not None:
            log.warning(
                "模型输出 JSON 含 %d 处非法转义、%d 处尾逗号，已自动修复", escapes, commas
            )
            return data

    # 仍失败：用原始 candidate 的报错信息（对用户最直观）
    try:
        json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LLMError(f"模型输出 JSON 无法解析：{exc}") from exc
    raise LLMError("模型输出 JSON 无法解析")


class Gateway:
    """对上层只暴露 chat_json；provider 可替换（将来换 kuncore model_router）。"""

    def __init__(self, provider: LLMProvider | None = None):
        self._provider = provider
        self._cache: dict[str, LLMResult] = {}
        self._cache_lock = threading.Lock()
        self._sem = threading.Semaphore(config.llm_max_concurrency())

    @property
    def provider(self) -> LLMProvider:
        """惰性解析 provider：构造网关不触发配置校验，首次调用时才解析。

        客观-only 评分链路不产生任何 LLM 调用，未配置 provider 也能全程离线；
        解析错误（未配置/密钥缺失）在真正调用时抛出，报错信息可操作。
        """
        if self._provider is None:
            from app.llm.registry import get_provider

            self._provider = get_provider()
        return self._provider

    # ------------------------------------------------------------------
    def _check_data_class(self, data_class: str) -> None:
        if data_class == "confidential":
            host = (self.provider.host or "").lower()
            allowed = [h.lower() for h in config.local_endpoint_hosts()]
            if host not in allowed and not _is_private_host(host):
                if config.allow_remote_confidential():
                    # 部署方显式接受"招标/投标原文会离开本机"（文件只有本地端点
                    # 可读时唯一的云端方案）。每个端点只告警一次，避免刷屏；
                    # 日志不含任何文件内容。
                    key = f"{self.provider.name}/{host}"
                    with _REMOTE_CONFIDENTIAL_WARNED_LOCK:
                        first = key not in _REMOTE_CONFIDENTIAL_WARNED
                        _REMOTE_CONFIDENTIAL_WARNED.add(key)
                    if first:
                        log.warning(
                            "涉密数据将发往非本地端点 %s（provider=%s）："
                            "已通过 BID_EVAL_ALLOW_REMOTE_CONFIDENTIAL 显式放开",
                            host, self.provider.name,
                        )
                    return
                raise DataClassDenied(
                    f"涉密数据（confidential）不允许发往 {host or '未知端点'}；"
                    f"仅允许本地端点：{allowed}",
                    detail={"host": host, "provider": self.provider.name},
                )

    def _check_budget(self) -> None:
        budget = config.llm_daily_call_budget()
        if budget <= 0:
            return
        # 预算计数必须**跨实例**共享（请求带 provider 覆盖会新建 Gateway）
        # 且按天滚动，否则换 provider 名或过午夜就绕过上限。
        today = time.strftime("%Y-%m-%d")
        with _BUDGET_LOCK:
            if _BUDGET_STATE["day"] != today:
                _BUDGET_STATE["day"] = today
                _BUDGET_STATE["calls"] = 0
            if _BUDGET_STATE["calls"] >= budget:
                raise LLMBudgetExceeded(f"今日模型调用已达上限 {budget} 次")
            _BUDGET_STATE["calls"] += 1

    @staticmethod
    def _cache_key(
        system: str,
        user: str,
        provider: str,
        model: str,
        prompt_version: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        # 影响输出的所有维度都要进键：temperature/max_tokens 不同 → 结果不同
        payload = "\x00".join(
            [provider, model, prompt_version, system, user, f"t={temperature}", f"m={max_tokens}"]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def chat(
        self,
        system: str,
        user: str,
        *,
        data_class: str = "synthetic",
        prompt_version: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> LLMResult:
        prompt_version = prompt_version or config.llm_prompt_version()
        self._check_data_class(data_class)
        key = self._cache_key(
            system, user, self.provider.name, self.provider.model, prompt_version,
            temperature, max_tokens,
        )

        with self._cache_lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit.model_copy(update={"cached": True})

        self._check_budget()
        with self._sem:
            result = self.provider.complete(
                system, user, temperature=temperature, max_tokens=max_tokens
            )
        log.info(
            "llm call provider=%s model=%s tokens_in=%s tokens_out=%s latency_ms=%s",
            result.provider, result.model, result.tokens_in, result.tokens_out, result.latency_ms,
        )
        with self._cache_lock:
            if len(self._cache) >= _CACHE_LIMIT:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = result
        return result

    def chat_json(
        self,
        system: str,
        user: str,
        out_model: type[T],
        *,
        data_class: str = "synthetic",
        prompt_version: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> tuple[T, LLMResult]:
        """调用并解析为 Pydantic 模型。解析失败抛 LLMError（上层降级为待人工，不猜）。"""
        result = self.chat(
            system,
            user,
            data_class=data_class,
            prompt_version=prompt_version,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        payload = extract_json(result.text)
        try:
            return out_model.model_validate(payload), result
        except ValidationError as exc:
            # 只保留 type/loc/msg：pydantic v2 的 errors() 默认带 input（字段原值），
            # 而这段文本会被拼进 AppError → job.error → 前端/日志——招标原文
            # 属 confidential，不得外泄（2026-10-21 合规复核）。
            brief = [
                {k: e.get(k) for k in ("type", "loc", "msg")} for e in exc.errors()[:3]
            ]
            raise LLMError(f"模型输出不符合契约：{brief}") from exc


def _is_private_host(host: str) -> bool:
    if not host:
        return False
    if host in ("localhost", "::1"):
        return True
    import ipaddress

    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return host.endswith(".local") or host.endswith(".internal")


def get_gateway() -> Gateway:
    """进程级单例（含缓存与预算计数）。"""
    global _GATEWAY
    if _GATEWAY is None:
        _GATEWAY = Gateway()
    return _GATEWAY


_GATEWAY: Gateway | None = None


def reset_gateway() -> None:
    """测试用：重建网关（重读环境变量、清缓存与预算）。"""
    global _GATEWAY
    _GATEWAY = None
