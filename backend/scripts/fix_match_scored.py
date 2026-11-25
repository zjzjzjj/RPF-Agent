# -*- coding: utf-8 -*-
"""一次性修正：冻结规则包里"得N分"型 match 误编译（2026-11-25 c45 实证）。

normalize_package 有 match_scored_fixed 规则（rule_service.py，"判断项不得带
分值，已按细则形态归正为计分项"），但**冻结包不再重跑 normalize**——c45
"具有有效期内CMMI认证二级或CSMM认证二级及以上得2分"被编译成 match（判断项），
2 分永远兑现不了（run159/160/161 三家各 −2）。本脚本把同一确定性规则应用到
含 match+分值 条目的包（按细则形态 classify_scoring_form 判定），幂等可重跑。

用法：python scripts/fix_match_scored.py [--package-id N]（缺省=全部包）
"""
from __future__ import annotations

import argparse
import sys


def fix_package(package_id: int) -> int:
    from sqlalchemy import select

    from app.db.models import Criterion, CriterionCheck, RulePackage
    from app.db.session import session_scope
    from app.rules.check_infer import classify_scoring_form

    fixed = 0
    with session_scope() as s:
        pkg = s.get(RulePackage, package_id)
        if pkg is None:
            print(f"包 {package_id} 不存在")
            return 0
        crits = s.execute(
            select(Criterion).where(Criterion.package_id == package_id)
        ).scalars().all()
        for c in crits:
            if (c.status == "retired" or c.score_type != "match" or c.is_veto
                    or c.max_score is None or c.max_score <= 0):
                continue
            form = classify_scoring_form(
                f"{c.name}\n{c.description or ''}\n{c.source_quote or ''}")
            if form is None:
                continue
            kind, params = form
            if kind == "all_or_nothing":
                new_type, params = "subjective", {
                    "_subjectivity_hint": "objective",
                    "_objective_form": "all_or_nothing",
                }
            else:
                new_type, params = "count", {**params, "_objective_form": kind}
            c.score_type = new_type
            s.add(c)
            check = s.execute(
                select(CriterionCheck).where(CriterionCheck.criterion_id == c.id)
            ).scalar_one_or_none()
            merged = dict(check.params_json or {}) if check is not None else {}
            for k, v in params.items():
                merged.setdefault(k, v)  # 既有显式参数优先
            if check is None:
                check = CriterionCheck(
                    criterion_id=c.id,
                    check_type="human_only" if new_type == "subjective" else "count_at_least",
                    field=merged.get("field") if isinstance(merged.get("field"), str) else None,
                    params_json=merged,
                    on_fail="need_human",
                    source_quote=c.source_quote,
                )
            else:
                check.params_json = merged
                if check.field is None and isinstance(merged.get("field"), str):
                    check.field = merged["field"]
            s.add(check)
            fixed += 1
            print(f"包{package_id} {c.code} {c.name[:24]}：match → {new_type}"
                  f"（form={kind}，依据细则形态，幂等修正）")
    return fixed


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--package-id", type=int, default=None)
    args = ap.parse_args()
    from sqlalchemy import select

    from app.db.models import RulePackage
    from app.db.session import session_scope

    if args.package_id is not None:
        ids = [args.package_id]
    else:
        with session_scope() as s:
            ids = [p.id for p in s.execute(select(RulePackage)).scalars()]
    total = sum(fix_package(pid) for pid in ids)
    print(f"done: fixed {total} criteria")


if __name__ == "__main__":
    main()
