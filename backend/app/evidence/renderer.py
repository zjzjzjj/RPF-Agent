"""页面渲染：pypdfium2 渲染 PNG 并磁盘缓存。

为什么服务端渲染而不是前端 PDF.js：
- 中文 PDF 在浏览器端有 CMap/字体坑；扫描件更是无文字层；
- 高亮**不烧录**进 PNG——前端在页图上按归一化 bbox 百分比叠加高亮层，
  切换证据零延迟（同一页只渲染一次）。
"""
from __future__ import annotations

from pathlib import Path

from app import config
from app.errors import AppError


def render_page_png(sha256: str, stored_path: str, page_no: int, scale: float | None = None) -> Path:
    """返回页图 PNG 的磁盘路径（带缓存）。"""
    scale = scale or config.render_scale()
    cache_dir = config.render_dir() / sha256[:16]
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"p{page_no}_{scale:g}.png"
    if cache.exists():
        return cache

    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(stored_path)
        if page_no < 1 or page_no > len(pdf):
            raise AppError(f"页码越界：{page_no}（共 {len(pdf)} 页）")
        page = pdf[page_no - 1]
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        image.save(cache, format="PNG")
        return cache
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AppError(f"页面渲染失败：{exc}") from exc
