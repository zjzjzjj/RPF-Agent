"""规则包快照与冻结：canonical JSON + 内容哈希。

冻结 = 把整个规则包序列化为 canonical JSON，存 snapshot_json 与 content_hash。
此后评分只认快照——即使业务表被误改，已发生的评分仍可按哈希复现。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any


def _default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"不可序列化类型：{type(obj)}")


def canonical_json(snapshot: dict) -> str:
    """排序键 + 紧凑分隔符 + 保留非 ASCII：同一内容永远得到同一字符串。"""
    return json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=_default)


def content_hash(snapshot: dict) -> str:
    """SHA-256 前 16 位。规则包内容的最小指纹（审计与报告引用）。"""
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()[:16]


def build_snapshot(
    package: dict,
    criteria: list[dict],
) -> dict:
    """从业务行组装快照。字段只挑与评分相关的（不含时间戳/操作人）。"""
    return {
        "schema": "rule_package/v1",
        "package": {
            "project_id": package.get("project_id"),
            "version": package.get("version"),
            "status": package.get("status"),
            "total_max": package.get("total_max", 100),
        },
        "criteria": [
            {
                "code": c["code"],
                "category": c["category"],
                "name": c["name"],
                "description": c.get("description"),
                "max_score": _num(c.get("max_score")),
                "weight": _num(c.get("weight")),
                "score_type": c["score_type"],
                "is_veto": bool(c.get("is_veto")),
                "veto_consequence": c.get("veto_consequence"),
                "required_evidence": c.get("required_evidence"),
                "source_node_id": c.get("source_node_id"),
                "source_page": c.get("source_page"),
                "source_quote": c.get("source_quote"),
                "status": c.get("status"),
                "sort_order": c.get("sort_order", 0),
                "levels": [
                    {
                        "label": lv.get("label"),
                        "min": _num(lv.get("min")),
                        "max": _num(lv.get("max")),
                        "criteria": lv.get("criteria"),
                    }
                    for lv in c.get("levels", [])
                ],
                "check_type": c.get("check_type"),
                "params": _jsonable(c.get("params") or {}),
                "formula": _jsonable(c.get("formula")),
            }
            for c in criteria
        ],
    }


def _num(value: Any) -> str | None:
    if value is None:
        return None
    return str(Decimal(str(value)))


def _jsonable(value: Any) -> Any:
    """递归转成 JSON 可序列化形态（Decimal→str，datetime→iso，其余原样）。

    快照必须能原样存进 JSON 列并逐字节复现，任何 Decimal 泄漏都会让冻结失败。
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value
