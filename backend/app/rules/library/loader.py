"""通用评分规则库加载器：模板 JSON → 强校验 → 只读目录。

设计稿 §3.1：模板随代码评审、随 git 溯源、随镜像发布；坏文件 fail-fast，
绝不让坏模板流入运行时。加载结果按目录缓存（测试可注入临时目录）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.rules.library.schema import RuleTemplateFile

LIBRARY_DIR = Path(__file__).resolve().parent / "templates"

_cache: dict[str, dict[str, RuleTemplateFile]] = {}


class LibraryError(ValueError):
    """模板库加载/校验失败（fail-fast）。"""


def load_templates(library_dir: Path | None = None) -> dict[str, RuleTemplateFile]:
    """加载目录下全部模板。文件名必须等于 template_id；任何坏文件整体失败。

    返回 {template_id: RuleTemplateFile}，进程内按目录缓存。
    """
    d = (library_dir or LIBRARY_DIR).resolve()
    if d in _cache:
        return _cache[d]

    if not d.is_dir():
        raise LibraryError(f"模板库目录不存在：{d}")

    templates: dict[str, RuleTemplateFile] = {}
    for path in sorted(d.glob("*.json")):
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise LibraryError(f"{path.name}：模板文件必须是 JSON 对象")
            if raw.get("template_id") != path.stem:
                raise LibraryError(
                    f"{path.name}：template_id（{raw.get('template_id')!r}）必须等于文件名 {path.stem}"
                )
            tpl = RuleTemplateFile.model_validate(raw)
        except LibraryError:
            raise
        except Exception as exc:  # JSON 解析失败 / Pydantic 校验失败
            raise LibraryError(f"{path.name}：模板校验失败 — {exc}") from exc
        templates[tpl.template_id] = tpl
    if not templates:
        raise LibraryError(f"模板库目录 {d} 中没有模板文件")

    _cache[str(d)] = templates
    return templates


def get_template(template_id: str, library_dir: Path | None = None) -> RuleTemplateFile:
    """按 id 取模板；不存在抛 LibraryError。"""
    tpl = load_templates(library_dir).get(template_id)
    if tpl is None:
        raise LibraryError(f"模板不存在：{template_id}")
    return tpl


def template_content_hash(tpl: RuleTemplateFile) -> str:
    """模板级内容哈希：canonical JSON 的 SHA-256 前 12 位（设计稿 §3.4）。"""
    import hashlib

    from app.rules.freeze import canonical_json

    return hashlib.sha256(canonical_json(tpl.model_dump(mode="json")).encode("utf-8")).hexdigest()[:12]


def library_status(library_dir: Path | None = None) -> dict:
    """模板库状态（供 /health 等报告加载数量）。"""
    templates = load_templates(library_dir)
    return {
        "loaded": len(templates),
        "templates": [
            {
                "template_id": t.template_id,
                "name": t.name,
                "method": t.method,
                "library_version": t.library_version,
                "criteria_count": len(t.criteria),
            }
            for t in templates.values()
        ],
    }


def reset_cache() -> None:
    """测试用：清空加载缓存。"""
    _cache.clear()