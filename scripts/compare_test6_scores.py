from __future__ import annotations

import argparse
import json
import sqlite3
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLDEN = ROOT / "backend" / "tests" / "fixtures" / "test6_manual_scores.json"


def _decimal(value) -> Decimal:
    return Decimal(str(value))


def load_actual(database: Path, run_id: int) -> dict:
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        names = {row["id"]: row["name"] for row in db.execute("SELECT id, name FROM bidders")}
        rows = db.execute(
            """
            SELECT c.code, s.bidder_id, s.final_score
            FROM score_records s
            JOIN criteria c ON c.id = s.criterion_id
            WHERE s.run_id = ?
            """,
            (run_id,),
        )
        scores = {}
        totals = {}
        for row in rows:
            name = names[row["bidder_id"]]
            if row["final_score"] is not None:
                scores.setdefault(row["code"], {})[name] = str(row["final_score"])
                totals[name] = totals.get(name, Decimal(0)) + _decimal(row["final_score"])
    return {
        "scores": scores,
        "totals": {name: str(value) for name, value in totals.items()},
    }


def compare(golden: dict, actual: dict) -> dict:
    names = [item["name"] for item in golden["bidder_order"]]
    failures = []
    objective = {}
    for code, expected_values in golden["expected_objective"].items():
        actual_values = [actual["scores"].get(code, {}).get(name) for name in names]
        expected = [str(Decimal(value)) for value in expected_values]
        normalized = [None if value is None else str(Decimal(value)) for value in actual_values]
        objective[code] = {"expected": expected, "actual": normalized, "exact": normalized == expected}
        if normalized != expected:
            failures.append(f"{code} objective mismatch")

    subjective = {}
    errors = []
    for code, expected_values in golden["manual_subjective"].items():
        rows = []
        for name, expected_value in zip(names, expected_values):
            actual_value = actual["scores"].get(code, {}).get(name)
            error = None if actual_value is None else abs(_decimal(actual_value) - _decimal(expected_value))
            if error is not None:
                errors.append(error)
            rows.append({"name": name, "expected": expected_value, "actual": actual_value,
                         "abs_error": None if error is None else str(error)})
        subjective[code] = rows
    max_error = max(errors, default=Decimal("Infinity"))
    mae = (sum(errors, Decimal(0)) / Decimal(len(errors))) if errors else Decimal("Infinity")
    if max_error > Decimal(golden["acceptance"]["subjective_max_abs_error"]):
        failures.append("subjective max abs error exceeded")
    if mae > Decimal(golden["acceptance"]["subjective_mae"]):
        failures.append("subjective MAE exceeded")

    expected_totals = golden["independent_rule_adjustment"]["adjusted_total"]
    total_rows = []
    for name, expected_value in zip(names, expected_totals):
        actual_value = actual["totals"].get(name)
        error = None if actual_value is None else abs(_decimal(actual_value) - _decimal(expected_value))
        total_rows.append({"name": name, "expected": expected_value, "actual": actual_value,
                           "abs_error": None if error is None else str(error)})
        if error is None or error > Decimal(golden["acceptance"]["adjusted_total_max_abs_error"]):
            failures.append(f"{name} adjusted total mismatch")

    expected_rank = golden["independent_rule_adjustment"]["rank"]
    actual_order = sorted(names, key=lambda name: _decimal(actual["totals"].get(name, "-Infinity")), reverse=True)
    actual_rank = [actual_order.index(name) + 1 for name in names]
    if actual_rank != expected_rank:
        failures.append("ranking mismatch")

    return {
        "passed": not failures,
        "failures": failures,
        "objective": objective,
        "subjective": {"rows": subjective, "max_abs_error": str(max_error), "mae": str(mae)},
        "totals": total_rows,
        "ranking": {"expected": expected_rank, "actual": actual_rank},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    args = parser.parse_args()
    golden = json.loads(args.golden.read_text(encoding="utf-8"))
    actual = load_actual(args.db, args.run_id)
    result = compare(golden, actual)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
