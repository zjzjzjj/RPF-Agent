"""LLM 网关：缓存 → 数据分级 → 限速 → 预算 → 调用 → 记账。

数据分级是合规刚需：confidential（真实投标文件）只允许发往本地端点。
审计只记录元数据（provider/model/prompt_version/tokens/latency/cache_hit），不记全文。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app import config
from app.errors import DataClassDenied, LLMBudgetExceeded
from app.llm.base import LLMError, LLMProvider, LLMResult

log = logging.getLogger("bid_eval.llm")

T = TypeVar("T", bound=BaseModel)

_CACHE_LIMIT = 512

# 缓存命中统计（阶段 1，2026-10-30）：mem=内存命中 / disk=磁盘命中 / miss=真实调用。
# 阶段 5 验收直接读它。+= 非原子，但只是观测计数，不参与判定。
_CACHE_STATS: dict[str, int] = {"mem": 0, "disk": 0, "miss": 0}

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


def _repair_missing_commas(text: str) -> tuple[str, int]:
    """字符串感知地补缺失逗号（`}`/`]`/数字/字面量后直接跟新 token 的形态）。

    实证（2026-10-23 run 44 客观审查 173 项中断）：DeepSeek 输出在数组元素/键值
    对之间漏一个逗号 → `Expecting ',' delimiter` → 整场评分失败。不能安全修复
    字符串内未转义引号，但"值后缺逗号"是确定性结构错误，可机械修复。
    """
    out: list[str] = []
    in_str = False
    escape = False
    fixed = 0
    last_sig = ""  # 字符串外最近一个"值结束"信号：} ] " #(数字) A(字面量)
    i = 0
    n = len(text)
    _DIGITS = "0123456789"
    _NUM_EXTRA = "+-.eE"
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
                last_sig = '"'
            i += 1
            continue
        if ch == '"':
            if last_sig in ('}', ']', '"', '#', 'A'):
                out.append(",")
                fixed += 1
            in_str = True
            out.append(ch)
            last_sig = ""
            i += 1
            continue
        if ch in "{[":
            if last_sig in ('}', ']', '"', '#', 'A'):
                out.append(",")
                fixed += 1
            out.append(ch)
            last_sig = ""
            i += 1
            continue
        if ch in ":,":
            out.append(ch)
            last_sig = ch
            i += 1
            continue
        if ch in "}]":
            out.append(ch)
            last_sig = ch
            i += 1
            continue
        if ch in _DIGITS or (ch == "-" and i + 1 < n and text[i + 1] in _DIGITS):
            # 数字 token：整段消费后再判下一个 token
            if last_sig in ('}', ']', '"', '#', 'A'):
                out.append(",")
                fixed += 1
            j = i
            while j < n and (text[j] in _DIGITS or text[j] in _NUM_EXTRA):
                j += 1
            out.append(text[i:j])
            last_sig = "#"
            i = j
            continue
        if ch.isalpha():
            # true/false/null 字面量（或未知裸词）整词消费
            if last_sig in ('}', ']', '"', '#', 'A'):
                out.append(",")
                fixed += 1
            j = i
            while j < n and text[j].isalnum():
                j += 1
            out.append(text[i:j])
            last_sig = "A"
            i = j
            continue
        # 空白与其它符号原样输出（不改变 last_sig：逗号后的空白后跟值仍能判对）
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

    # 第三级：缺失逗号（值后直接跟新 token）——2026-10-23 run 44 客观审查
    # 173 项因一个漏逗号整场失败；结构错误可机械补。
    nocommas, inserted = _repair_missing_commas(stripped)
    if inserted:
        data = _loads_lenient(nocommas)
        if data is not None:
            log.warning(
                "模型输出 JSON 含 %d 处非法转义、%d 处尾逗号、%d 处缺失逗号，已自动修复",
                escapes, commas, inserted,
            )
            return data

    # 仍失败：用原始 candidate 的报错信息（对用户最直观）
    try:
        json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LLMError(f"模型输出 JSON 无法解析：{exc}") from exc
    raise LLMError("模型输出 JSON 无法解析")


# ---------------------------------------------------------------------------
# LLM 磁盘缓存（阶段 1，2026-10-30）
# ---------------------------------------------------------------------------
def _disk_cache_path(key: str) -> Path:
    """缓存文件路径：data/llm_cache/<key 前 16 位>.json（键是 sha256，16 位前缀已够唯一）。"""
    return config.data_dir() / "llm_cache" / f"{key[:16]}.json"


def _disk_get(key: str) -> dict | None:
    """读磁盘缓存：文件缺失/坏 JSON/缺字段一律 None（静默当 miss，绝不抛）。

    缓存是 best-effort 加速层：任何读失败都退回真实调用，不能让评分链路失败。
    """
    if not config.llm_disk_cache_enabled():
        return None
    try:
        data = json.loads(_disk_cache_path(key).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 文件不存在/坏 JSON/坏编码都当 miss
        return None
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("text"), str)
        or not isinstance(data.get("provider"), str)
        or not isinstance(data.get("model"), str)
    ):
        return None
    return data


def _disk_put(key: str, result: LLMResult) -> None:
    """写磁盘缓存（best-effort）：mkdir + 临时文件 + os.replace 原子替换。

    只在 chat_json 校验通过后调用（2026-10-30 阶段 1）：坏文本绝不上盘，
    避免重启/重试后反复命中坏缓存。任何 IO 失败都静默——缓存写不进
    绝不能让评分失败。
    """
    if not config.llm_disk_cache_enabled():
        return
    payload = {
        "provider": result.provider,
        "model": result.model,
        "text": result.text,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "latency_ms": result.latency_ms,
    }
    try:
        path = _disk_cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件带 pid+线程 id：并发写同一 key 互不踩踏，os.replace 保证原子可见
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 —— 落盘失败静默，绝不影响本次调用结果
        pass


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
        thinking: str = "auto",
        disable_thinking: bool = False,
        seed: int | None = None,
        base_url: str = "",
    ) -> str:
        # 影响输出的所有维度都要进键：temperature/max_tokens/thinking/seed/base_url
        # 任一不同 → 结果不同（2026-10-30 阶段 1：补 thinking/disable_thinking/seed/
        # base_url；同 host 不同路径前缀的端点也可能是不同模型服务）。
        payload = "\x00".join(
            [
                provider,
                model,
                base_url,
                prompt_version,
                system,
                user,
                f"t={temperature}",
                f"m={max_tokens}",
                f"thinking={thinking}",
                f"dt={disable_thinking}",
                f"seed={seed}",
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _key_for(
        self,
        system: str,
        user: str,
        prompt_version: str,
        temperature: float,
        max_tokens: int | None,
    ) -> str:
        """chat / chat_json 共用的缓存键：现取 provider 与全部影响输出的配置维度。

        base_url 用 getattr 容错：FakeProvider 等测试替身没有该属性（2026-10-30）。
        """
        return self._cache_key(
            system,
            user,
            self.provider.name,
            self.provider.model,
            prompt_version,
            temperature,
            max_tokens,
            thinking=config.llm_thinking(),
            disable_thinking=config.llm_disable_thinking(),
            seed=config.llm_seed(),
            base_url=getattr(self.provider, "base_url", ""),
        )

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
        key = self._key_for(system, user, prompt_version, temperature, max_tokens)

        with self._cache_lock:
            hit = self._cache.get(key)
        if hit is not None:
            _CACHE_STATS["mem"] += 1
            return hit.model_copy(update={"cached": True})

        # 磁盘缓存（阶段 1，2026-10-30）：命中即回填内存并返回——不调 provider、
        # 不耗预算（预算只统计真实调用）。
        disk = _disk_get(key)
        if disk is not None:
            result = LLMResult(
                text=disk["text"],
                provider=disk["provider"],
                model=disk["model"],
                tokens_in=disk.get("tokens_in"),
                tokens_out=disk.get("tokens_out"),
                latency_ms=disk.get("latency_ms"),
                cached=True,
            )
            with self._cache_lock:
                if len(self._cache) >= _CACHE_LIMIT:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[key] = result
            _CACHE_STATS["disk"] += 1
            return result

        _CACHE_STATS["miss"] += 1
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
        """调用并解析为 Pydantic 模型。解析失败抛 LLMError（上层降级为待人工，不猜）。

        JSON 解析/契约失败重试 2 次（采样偶发缺逗号/截断，换一次生成大概率就好；
        余额/配额类错误不重试——上层按 fail-fast 处理）。
        model_validate 成功才落磁盘缓存（阶段 1，2026-10-30）：坏文本绝不上盘；
        失败重试前驱逐该 key 的内存/磁盘缓存——chat() 刚把这份坏文本缓存了进去，
        不驱逐的话下一轮重试会直接命中它、永远不再调 provider（自缓存毒化）。
        """
        prompt_version = prompt_version or config.llm_prompt_version()
        key = self._key_for(system, user, prompt_version, temperature, max_tokens)
        last_exc: LLMError | None = None
        result = None
        for attempt in range(3):
            result = self.chat(
                system,
                user,
                data_class=data_class,
                prompt_version=prompt_version,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            try:
                payload = extract_json(result.text)
                validated = out_model.model_validate(payload)
            except ValidationError as exc:
                # 只保留 type/loc/msg：pydantic v2 的 errors() 默认带 input（字段原值），
                # 而这段文本会被拼进 AppError → job.error → 前端/日志——招标原文
                # 属 confidential，不得外泄（2026-10-21 合规复核）。
                brief = [
                    {k: e.get(k) for k in ("type", "loc", "msg")} for e in exc.errors()[:3]
                ]
                last_exc = LLMError(f"模型输出不符合契约：{brief}")
                last_exc.__cause__ = exc
                self._evict_failed(key)
            except LLMError as exc:
                msg = str(exc)
                if any(k in msg for k in ("402", "insufficient", "quota", "30001", "arrears")):
                    raise  # 余额/配额：fail-fast，不重试
                last_exc = exc
                self._evict_failed(key)
            else:
                _disk_put(key, result)  # 校验通过才上盘（阶段 1，2026-10-30）
                return validated, result
            if attempt < 2:
                log.warning(
                    "chat_json 第 %d 次输出解析失败，重试：%s", attempt + 1, last_exc
                )
        assert last_exc is not None and result is not None
        raise last_exc

    def _evict_failed(self, key: str) -> None:
        """驱逐坏文本缓存：弹内存 + 删盘上文件（best-effort）。

        2026-10-30 阶段 1：解析/契约失败的文本无论来自 provider 还是缓存，都
        不允许留在缓存里——否则重试（含新 Gateway 实例）会反复命中同一份坏
        文本、永远不调 provider（自缓存毒化）。删不存在的文件是 no-op。
        """
        with self._cache_lock:
            self._cache.pop(key, None)
        try:
            _disk_cache_path(key).unlink(missing_ok=True)
        except OSError:
            pass


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
    _CACHE_STATS.update({"mem": 0, "disk": 0, "miss": 0})
