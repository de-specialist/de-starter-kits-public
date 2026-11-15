#!/usr/bin/env python3
"""Compare a synthetic semantic-model export with the authored expected KPI CSV."""

import argparse
import csv
from decimal import Decimal, InvalidOperation
from datetime import date
from pathlib import Path
import re


ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def normalize_period(period: str) -> str:
    """Return the YYYY-MM month key for a valid YYYY-MM-DD date; else the stripped value."""
    period = period.strip()
    if ISO_DATE.fullmatch(period):
        try:
            date.fromisoformat(period)
        except ValueError:
            return period
        return period[:7]
    return period


def read_rows(path: Path):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    required = {"period", "kpi", "value"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"{path} must contain columns: {', '.join(sorted(required))}")
    result = {}
    for row in rows:
        key = (normalize_period(row["period"]), row["kpi"].strip())
        if key in result:
            raise ValueError(f"{path} contains duplicate period/KPI key: {key}")
        result[key] = row
    return result


def equal_value(left, right, tolerance):
    if left in (None, "") or right in (None, ""):
        return left in (None, "") and right in (None, "")
    try:
        return abs(Decimal(left) - Decimal(right)) <= tolerance
    except InvalidOperation:
        return left == right


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("actual", type=Path, help="CSV exported from the Gold semantic model")
    parser.add_argument(
        "--expected", type=Path,
        default=Path(__file__).parent / "fixtures/expected/monthly_hr_kpis.csv",
    )
    parser.add_argument("--tolerance", default="0.000001")
    args = parser.parse_args()
    expected, actual = read_rows(args.expected), read_rows(args.actual)
    tolerance = Decimal(args.tolerance)
    mismatches = []
    for key in sorted(expected.keys() | actual.keys()):
        want, got = expected.get(key), actual.get(key)
        if want is None:
            mismatches.append((key, "unexpected actual row", got.get("value")))
        elif got is None:
            mismatches.append((key, "missing actual row", want.get("value")))
        elif not equal_value(want.get("value"), got.get("value"), tolerance):
            mismatches.append((key, f"value expected {want.get('value')!r}", got.get("value")))
        elif want.get("status") and got.get("status") and want["status"] != got["status"]:
            mismatches.append((key, f"status expected {want['status']!r}", got["status"]))
    if mismatches:
        for key, expected_text, actual_text in mismatches:
            print(f"{key[0]} {key[1]}: {expected_text}; actual {actual_text!r}")
        return 1
    print(f"Reconciled {len(expected)} synthetic KPI rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
