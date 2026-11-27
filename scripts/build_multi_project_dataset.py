from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(r"E:\ZJUBJ\数据001\评分对比\_对比工作")
OUT = Path(r"E:\codex work\RPF-Agent-12项目统一校准数据集.json")

CATEGORY_MAP = {
    "商务客观": "commercial_objective",
    "商务主观": "commercial_subjective",
    "技术客观": "technical_objective",
    "技术主观": "technical_subjective",
    "资格/符合性": "qualification",
    "价格": "price",
}


def norm(value: str) -> str:
    return "".join(str(value or "").split()).replace("（", "(").replace("）", ")")


def find_manual_item(items, names):
    wanted = {norm(x) for x in names}
    for item in items or []:
        if norm(item.get("item")) in wanted:
            return item
    return None


def build():
    dataset = []
    for index in range(1, 13):
        path = ROOT / f"测试{index}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        manual_items = data.get("manual", {}).get("items") or []
        ai_items = data.get("ai", {}).get("items") or []
        ai_by_code = {str(x.get("code")): x for x in ai_items}
        for align in data.get("alignment") or []:
            code = str(align.get("ai_code") or "")
            ai = ai_by_code.get(code)
            if not ai:
                continue
            manual = find_manual_item(manual_items, align.get("manual_items") or [])
            if not manual:
                continue
            max_score = float(ai.get("max") or 0)
            category = CATEGORY_MAP.get(str(align.get("category") or ""), "other")
            for bidder, expected in (manual.get("scores") or {}).items():
                actual = (ai.get("scores") or {}).get(bidder)
                if actual is None:
                    continue
                dataset.append({
                    "test_id": data.get("test_id"),
                    "project_name": data.get("project_name"),
                    "item_code": code,
                    "item_name": ai.get("name"),
                    "category": category,
                    "max_score": max_score,
                    "bidder": bidder,
                    "ai_score": float(actual),
                    "manual_score": float(expected),
                    "ai_ratio": float(actual) / max_score if max_score else None,
                    "manual_ratio": float(expected) / max_score if max_score else None,
                })
    OUT.write_text(json.dumps({"rows": dataset, "count": len(dataset)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"count": len(dataset), "output": str(OUT)}, ensure_ascii=False))


if __name__ == "__main__":
    build()
