#!/usr/bin/env python3
"""Offline check for the SCD2 history template. Standard library only; no warehouse.

Each edition's ``scd2_history.sql`` is run on in-memory SQLite against the MOCK
fixtures and compared with the expected history. The shared singular tests must
return no rows on good output and must return rows on deliberately broken output.

This checks the template's logic. It does not prove that BigQuery or Snowflake
accept the SQL: ``config()``/``ref()`` are stubbed and a few dialect spellings are
rewritten for SQLite (see ``to_sqlite``). Run ``dbt build`` in a warehouse for that.

Usage: python3 migration/templates/scd2-history/check_template.py
"""

from __future__ import annotations

import csv
import hashlib
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
EDITIONS = ("bigquery", "snowflake")
HISTORY = "scd2_customer_history"
CHANGES = "stg_mock_customer_changes"
SNAPSHOTS = "stg_mock_customer_snapshots"
BOOLEAN_COLUMNS = {"is_deleted", "is_current"}
# Spellings that belong to the other warehouse and must not leak across editions.
FOREIGN_DIALECT = {
    "bigquery": ("::", "iff(", "count_if("),
    "snowflake": ("to_hex(", "safe_cast(", "countif(", "int64"),
}


def to_sqlite(sql: str) -> str:
    """Stub the dbt Jinja and rewrite the few non-SQLite spellings the templates use."""
    sql = re.sub(r"\{\{\s*config\(.*?\)\s*\}\}", "", sql, flags=re.S)
    sql = re.sub(r"\{\{\s*ref\('(\w+)'\)\s*\}\}", r"\1", sql)
    if "{{" in sql or "{%" in sql:
        raise ValueError("unsupported Jinja left after stubbing config() and ref()")
    sql = re.sub(r"::\w+", "", sql)  # Snowflake casts; SQLite text needs none here
    return re.sub(r"\bas string\)", "as text)", sql)  # BigQuery string cast


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.create_function("md5", 1, lambda v: hashlib.md5(str(v).encode()).hexdigest())
    con.create_function("to_hex", 1, lambda v: v)
    return con


def read_csv(name: str) -> tuple[list[str], list[tuple]]:
    with (FIXTURES / name).open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        return header, [tuple(row) for row in reader]


def load(con: sqlite3.Connection, table: str, name: str) -> None:
    header, rows = read_csv(name)
    con.execute(f"create table {table} ({', '.join(header)})")
    booleans = [column in BOOLEAN_COLUMNS for column in header]
    for row in rows:
        values = [
            None if value == "" else (value == "true") if is_bool else value
            for value, is_bool in zip(row, booleans)
        ]
        con.execute(f"insert into {table} values ({', '.join('?' * len(header))})", values)


def history_rows(con: sqlite3.Connection, header: list[str]) -> list[tuple]:
    def text(column: str, value: object) -> str:
        if value is None:
            return ""
        return ("true" if value else "false") if column in BOOLEAN_COLUMNS else str(value)

    cursor = con.execute(f"select {', '.join(header)} from {HISTORY}")
    return sorted(tuple(text(c, v) for c, v in zip(header, row)) for row in cursor)


def failures(con: sqlite3.Connection, test: str) -> list[tuple]:
    return con.execute(to_sqlite((HERE / "shared" / test).read_text(encoding="utf-8"))).fetchall()


def check_edition(edition: str, scenario: str) -> list[str]:
    errors: list[str] = []
    model = (HERE / edition / "scd2_history.sql").read_text(encoding="utf-8")
    con = connect()
    if scenario == "change_log":
        expected_file = "expected_customer_history.csv"
        load(con, "raw_changes", "mock_customer_changes.csv")
        con.execute(
            f"create view {CHANGES} as select *, 'source_effective' as effective_basis, "
            "'event' as observation_grain from raw_changes"
        )
        # The grain test needs a snapshot relation; change-log history must pass it untouched.
        con.execute(f"create table {SNAPSHOTS} (snapshot_date)")
    else:
        expected_file = "expected_snapshot_history.csv"
        load(con, SNAPSHOTS, "mock_customer_monthly_snapshots.csv")
        prep = (HERE / "shared" / "snapshot_to_changes.sql").read_text(encoding="utf-8")
        con.execute(f"create view {CHANGES} as {to_sqlite(prep)}")
    con.execute(f"create table {HISTORY} as {to_sqlite(model)}")

    header, expected = read_csv(expected_file)
    actual = history_rows(con, header)
    if actual != sorted(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        errors.append(f"history differs from {expected_file}: missing={missing} extra={extra}")

    keys = con.execute(f"select count(*), count(distinct scd2_key) from {HISTORY}").fetchone()
    if keys[0] != keys[1]:
        errors.append("scd2_key is not unique")
    for test in ("assert_scd2_intervals.sql", "assert_scd2_snapshot_grain.sql"):
        rows = failures(con, test)
        if rows:
            errors.append(f"{test} failed on template output: {rows}")

    # Negative controls: each singular test must catch the defect it exists for.
    con.execute(
        f"update {HISTORY} set valid_to = null, is_current = 1 "
        f"where rowid = (select min(rowid) from {HISTORY} where valid_to is not null)"
    )
    if not failures(con, "assert_scd2_intervals.sql"):
        errors.append("assert_scd2_intervals.sql missed a second open interval")
    if scenario == "snapshots":
        con.execute(f"update {HISTORY} set valid_from = '2026-03-15' where valid_from = '2026-03-31'")
        if not failures(con, "assert_scd2_snapshot_grain.sql"):
            errors.append("assert_scd2_snapshot_grain.sql missed a mid-month boundary")
    return [f"{edition}/{scenario}: {error}" for error in errors]


def check_tied_corrections(edition: str) -> list[str]:
    """Exercise each fallback term with both arrival orders and duplicate rows."""
    model = to_sqlite((HERE / edition / "scd2_history.sql").read_text(encoding="utf-8"))
    columns = ["customer_key", "effective_at", "recorded_at", "is_deleted",
               "tier", "region", "effective_basis", "observation_grain"]
    base = ["MOCK-TIE", "2026-01-01", "2026-02-01", 0,
            "basic", "east", "source_effective", "event"]
    cases = [("recorded_at", "2026-01-01", "2026-02-01"),
             ("is_deleted", None, 1), ("is_deleted", 0, 1)]
    for column in columns[4:]:
        cases.extend([(column, None, "a"), (column, "a", "z")])
    cases.append(("tier", "basic", "basic"))  # exact duplicates
    errors = []
    for column, lower, higher in cases:
        loser, winner = base.copy(), base.copy()
        loser[columns.index(column)] = lower
        winner[columns.index(column)] = higher
        expected = (winner[4], winner[5], bool(winner[3]), winner[6], winner[7], winner[2])
        for rows in ((loser, winner, winner), (winner, winner, loser)):
            with connect() as con:
                con.execute(f"create table {CHANGES} ({', '.join(columns)})")
                con.executemany(f"insert into {CHANGES} values (?, ?, ?, ?, ?, ?, ?, ?)", rows)
                con.execute(f"create table {HISTORY} as {model}")
                actual = con.execute(
                    f"select tier, region, is_deleted, effective_basis, observation_grain, "
                    f"source_recorded_at from {HISTORY}"
                ).fetchall()
                if actual != [expected]:
                    errors.append(f"{edition}/ties: {column} ({lower!r}, {higher!r}): {actual!r}")
    return errors


def check_files() -> list[str]:
    errors: list[str] = []
    for edition, tokens in FOREIGN_DIALECT.items():
        text = (HERE / edition / "scd2_history.sql").read_text(encoding="utf-8").lower()
        errors += [f"{edition}/scd2_history.sql uses other-edition syntax {t!r}" for t in tokens if t in text]
    for name in ("mock_customer_changes.csv", "mock_customer_monthly_snapshots.csv"):
        header, rows = read_csv(name)
        key = header.index("customer_key")
        if any(not row[key].startswith("MOCK-") for row in rows):
            errors.append(f"fixtures/{name}: every customer_key must start with MOCK-")
    return errors


def main() -> int:
    errors = check_files()
    for edition in EDITIONS:
        errors += check_tied_corrections(edition)
        for scenario in ("change_log", "snapshots"):
            errors += check_edition(edition, scenario)
    for error in errors:
        print(f"FAIL {error}", file=sys.stderr)
    if not errors:
        print("OK scd2-history template: 2 editions x 2 scenarios match expected history; 48 tied-correction checks pass (SQLite, offline)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
