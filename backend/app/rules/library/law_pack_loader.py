"""法规通用规则包加载器：law_packs/<pack>/ → 强校验 → 组装导入文档。

借鉴 coll_det 规则包框架：pack.json（元信息）+ policies.json（法条登记）
+ rules/（一条一文件）。坏包 fail-fast，绝不让坏包流入导入管线。

对外只暴露三个函数：
- load_law_pack() -> {"package": {...}, "criteria": [...]}   原始 dict（非 model_dump），
  与法规库制品逐字段一致，可直接喂给 law_import_service / validate_only；
- law_pack_content_hash() -> canonical JSON SHA-256 前 16 位；
- reset_law_pack_cache()。

加载期校验链（任一失败抛 LawPackError，消息含文件名）：
JSON 可解析 → LawPackRule 契约 → code==文件名 → 全包 code 唯一
→ `法条://` 引用必须在 policies.json 命中（text 非空）
→ 其它前缀引用必须在 pack.json.external_refs 声明
→ rule_count 与实际文件数一致。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.rules.freeze import canonical_json
from app.rules.library.law_pack_schema import LawPackMeta, LawPackRule

LAW_PACKS_DIR = Path(__file__).resolve().parent / "law_packs"
DEFAULT_PACK_ID = "general-law"

# 组序即 sort_order 序：与法规库制品的规则排列一致
_GROUP_ORDER = ("FORM", "QUAL", "RES", "REJ")
_CODE_RE = re.compile(r"^(?P<prefix>[A-Z]+)-(?P<seq>\d+)$")

_cache: dict[str, dict[str, Any]] = {}


class LawPackError(ValueError):
    """法规规则包加载/校验失败（fail-fast）。"""


def default_pack_dir(pack_id: str = DEFAULT_PACK_ID) -> Path:
    return LAW_PACKS_DIR / pack_id


def _sort_key(code: str) -> tuple[int, str, int]:
    m = _CODE_RE.match(code)
    if not m:
        return (len(_GROUP_ORDER), code, 0)
    prefix = m.group("prefix")
    try:
        seq = int(m.group("seq"))
    except ValueError:
        seq = 0
    group = _GROUP_ORDER.index(prefix) if prefix in _GROUP_ORDER else len(_GROUP_ORDER)
    return (group, prefix, seq)


def _load_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 解析/编码问题统一呈现
        raise LawPackError(f"{path.name}：JSON 解析失败 — {exc}") from exc


def _validate_rule_file(path: Path) -> tuple[LawPackRule, dict[str, Any]]:
    """校验单个规则文件，返回（契约模型, 原始 dict）——原始 dict 原样进 criteria。"""
    raw = _load_json_file(path)
    if not isinstance(raw, dict):
        raise LawPackError(f"{path.name}：规则文件必须是 JSON 对象")
    if str(raw.get("code") or "") != path.stem:
        raise LawPackError(
            f"{path.name}：code（{raw.get('code')!r}）必须等于文件名 {path.stem}"
        )
    try:
        rule = LawPackRule.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        raise LawPackError(f"{path.name}：规则契约校验失败 — {exc}") from exc
    return rule, raw


def load_law_pack(pack_dir: Path | None = None) -> dict[str, Any]:
    """加载并校验法规规则包，组装为导入服务可直接消费的 {package, criteria}。

    返回原始 dict（值来自 JSON 文件本身，非 model_dump）——与法规库制品
    逐字段一致，artifact_sha256 语义不变。
    """
    d = (pack_dir or default_pack_dir()).resolve()
    key = str(d)
    if key in _cache:
        return _cache[key]

    if not d.is_dir():
        raise LawPackError(f"法规规则包目录不存在：{d}")

    meta_raw = _load_json_file(d / "pack.json")
    if not isinstance(meta_raw, dict):
        raise LawPackError("pack.json 必须是 JSON 对象")
    try:
        meta = LawPackMeta.model_validate(meta_raw)
    except Exception as exc:  # noqa: BLE001
        raise LawPackError(f"pack.json：包契约校验失败 — {exc}") from exc

    policies_raw = _load_json_file(d / "policies.json")
    if not isinstance(policies_raw, dict) or not policies_raw:
        raise LawPackError("policies.json 必须是非空 JSON 对象")
    for node_id, entry in policies_raw.items():
        if not (isinstance(entry, dict) and str(entry.get("text") or "").strip()):
            raise LawPackError(f"policies.json：{node_id} 缺少非空 text（法条原文）")

    rule_files = sorted((d / "rules").glob("*.json"))
    if not rule_files:
        raise LawPackError(f"{d / 'rules'}：目录下没有规则文件")

    criteria: list[dict[str, Any]] = []
    seen: set[str] = set()
    external = set(meta.external_refs)
    for path in sorted(rule_files, key=lambda p: _sort_key(p.stem)):
        rule, raw = _validate_rule_file(path)
        code = rule.code
        if code in seen:
            raise LawPackError(f"{path.name}：code 重复（{code}）")
        seen.add(code)
        node = rule.source_node_id
        if node:
            if node.startswith("法条://"):
                if node not in policies_raw:
                    raise LawPackError(
                        f"{path.name}：法条引用 {node} 未在 policies.json 登记"
                        "（手改规则或 policies 漂移）"
                    )
            elif node not in external:
                raise LawPackError(
                    f"{path.name}：非法条引用 {node} 未在 pack.json.external_refs 声明"
                )
        criteria.append(raw)

    if len(criteria) != meta.rule_count:
        raise LawPackError(
            f"pack.json：rule_count（{meta.rule_count}）与实际规则文件数（{len(criteria)}）不一致"
        )

    doc: dict[str, Any] = {
        "package": {
            "id": meta.id,
            "name": meta.name,
            "version": meta.version,
            "description": meta.description,
            "scope": meta.scope,
            "source": meta.source,
            "total_max": meta.total_max,
            "status": meta.status,
        },
        "criteria": criteria,
    }
    _cache[key] = doc
    return doc


def law_pack_content_hash(pack_dir: Path | None = None) -> str:
    """包内容指纹：组装文档 canonical JSON 的 SHA-256 前 16 位。"""
    return hashlib.sha256(
        canonical_json(load_law_pack(pack_dir)).encode("utf-8")
    ).hexdigest()[:16]


def reset_law_pack_cache() -> None:
    """测试用：清空加载缓存。"""
    _cache.clear()
