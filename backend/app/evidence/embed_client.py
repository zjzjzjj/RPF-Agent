"""bge-m3 远程嵌入客户端（mwb 自部署服务，TEI 兼容协议）。

服务来源：mwb 的 coll-det-gpu-embed-proxy（GPU 5 号卡，bge-m3 CLS pooling + L2）。
协议：POST {base}/embed  body {"inputs": str | list[str]}
      → [[1024 维 float], ...]（与输入顺序一致）
磁盘缓存 embed_cache/<sha256[:16]>.json——同一评分项/分块重复嵌入直接命中。
"""
from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

from app import config

log = logging.getLogger("bid_eval.evidence.embed")

_TIMEOUT = 120
_DIM = 1024


def _cache_dir() -> Path:
    return config.data_dir() / "embed_cache"


def _cache_get(key: str) -> list[float] | None:
    f = _cache_dir() / f"{key}.json"
    if not f.exists():
        return None
    try:
        vec = json.loads(f.read_text(encoding="utf-8"))
        return vec if isinstance(vec, list) and len(vec) == _DIM else None
    except Exception:  # noqa: BLE001 —— 缓存坏了当没有
        return None


def _cache_put(key: str, vec: list[float]) -> None:
    try:
        f = _cache_dir() / f"{key}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(vec), encoding="utf-8")
    except OSError:
        pass  # 缓存写失败不影响主流程


def _remote(texts: list[str]) -> list[list[float]]:
    url = config.embed_url().rstrip("/") + "/embed"
    body = json.dumps({"inputs": texts}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            vecs = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise RuntimeError(f"嵌入服务调用失败：{exc}") from exc
    if not isinstance(vecs, list) or len(vecs) != len(texts):
        raise RuntimeError("嵌入服务返回数量与输入不一致")
    return vecs


def embed_one(text: str) -> list[float] | None:
    """单文本嵌入；服务不可用返回 None（调用方回退关键词选段）。"""
    return embed_batch([text])[0]


def embed_batch(texts: list[str]) -> list[list[float] | None]:
    """批量嵌入，逐条磁盘缓存；失败返回等长 None 列表。"""
    keys = [hashlib.sha256(t.encode("utf-8")).hexdigest()[:16] for t in texts]
    out: list[list[float] | None] = [_cache_get(k) for k in keys]
    missing = [i for i, v in enumerate(out) if v is None]
    if missing:
        try:
            vecs = _remote([texts[i] for i in missing])
        except RuntimeError as exc:
            log.warning("批量嵌入失败，%d 条返回 None：%s", len(missing), exc)
            return out
        for pos, i in enumerate(missing):
            vec = vecs[pos]
            if isinstance(vec, list) and len(vec) == _DIM:
                out[i] = vec
                _cache_put(keys[i], vec)
    return out


def cosine(a: list[float], b: list[float]) -> float:
    """L2 归一化向量的余弦 = 点积；退化输入返回 0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))
