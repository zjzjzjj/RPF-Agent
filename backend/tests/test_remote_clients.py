"""远程模型客户端单测：mock HTTP，不依赖真实服务。"""
from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# PaddleOCR-VL 客户端
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


def test_remote_ocr_page_success(monkeypatch):
    from app.parsing import paddle_client

    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(json.dumps(
            {"choices": [{"message": {"content": "第三章 投标保证金汇款凭证"}}]}
        ).encode("utf-8"))

    monkeypatch.setattr(paddle_client.urllib.request, "urlopen", fake_urlopen)
    text = paddle_client.remote_ocr_page(b"\x89PNG-fake")
    assert text == "第三章 投标保证金汇款凭证"
    assert captured["url"].endswith("/v1/chat/completions")
    content = captured["body"]["messages"][0]["content"]
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_remote_ocr_page_failure_raises(monkeypatch):
    from app.parsing import paddle_client

    def fake_urlopen(request, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(paddle_client.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="调用失败"):
        paddle_client.remote_ocr_page(b"png")


# ---------------------------------------------------------------------------
# bge-m3 嵌入客户端
# ---------------------------------------------------------------------------
def test_embed_batch_remote_and_cache(monkeypatch, tmp_path):
    import app.config as config
    from app.evidence import embed_client

    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)
    calls = []

    def fake_urlopen(request, timeout=0):
        calls.append(json.loads(request.data.decode("utf-8")))
        texts = calls[-1]["inputs"]
        return _FakeResponse(json.dumps(
            [[0.1] * 1024 for _ in texts]).encode("utf-8"))

    monkeypatch.setattr(embed_client.urllib.request, "urlopen", fake_urlopen)
    v1 = embed_client.embed_batch(["业绩", "证书"])
    assert len(v1) == 2 and len(v1[0]) == 1024
    v2 = embed_client.embed_batch(["业绩"])  # 第二次命中磁盘缓存
    assert len(calls) == 1  # 只发过一次远程请求
    assert v2[0] == v1[0]


def test_embed_batch_failure_returns_nones(monkeypatch, tmp_path):
    import app.config as config
    from app.evidence import embed_client

    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)

    def fake_urlopen(request, timeout=0):
        raise OSError("down")

    monkeypatch.setattr(embed_client.urllib.request, "urlopen", fake_urlopen)
    out = embed_client.embed_batch(["a", "b"])
    assert out == [None, None]


# ---------------------------------------------------------------------------
# 向量选段 + 统一入口
# ---------------------------------------------------------------------------
def _chunks():
    return [
        (0, "营业执照复印件详见附件"),
        (1, "售后服务方案：7×24 小时响应"),
        (2, "项目团队配置：高级工程师 5 名"),
        (3, "无关填充内容 " * 50),
    ]


def test_select_vector_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("BID_EVAL_SELECTION", "vector")
    from app.engine import selection
    from app.evidence import embed_client

    # 查询与 chunk1 最相似（手造向量：前 1024 维全 0.5）
    def fake_embed_one(text):
        return [0.5] * 1024 if "售后" in text else [0.0] * 1024

    def fake_embed_batch(texts):
        return [fake_embed_one(t) for t in texts]

    monkeypatch.setattr(embed_client, "embed_one", fake_embed_one)
    monkeypatch.setattr(embed_client, "embed_batch", fake_embed_batch)
    picked, meta = selection.select_chunks_for_criterion(
        _chunks(), {"name": "售后服务方案"}, 5000
    )
    assert meta["mode"] == "vector" and meta["tiers"].get("vector", 0) >= 1
    assert any("售后服务" in t for _i, t in picked)


def test_select_vector_downgrades_to_keyword(monkeypatch, tmp_path):
    monkeypatch.setenv("BID_EVAL_SELECTION", "vector")
    from app.engine import selection
    from app.evidence import embed_client

    monkeypatch.setattr(
        embed_client, "embed_one",
        lambda text: (_ for _ in ()).throw(RuntimeError("嵌入服务调用失败")),
    )
    picked, meta = selection.select_chunks_for_criterion(
        _chunks(), {"name": "售后服务方案"}, 5000
    )
    assert meta["mode"] == "keyword"
    assert any("售后服务" in t for _i, t in picked)


def test_select_keyword_mode_default(monkeypatch):
    monkeypatch.setenv("BID_EVAL_SELECTION", "keyword")
    from app.engine import selection

    picked, meta = selection.select_chunks_for_criterion(
        _chunks(), {"name": "售后服务方案"}, 5000
    )
    assert meta["mode"] == "keyword"
    assert any("售后服务" in t for _i, t in picked)
