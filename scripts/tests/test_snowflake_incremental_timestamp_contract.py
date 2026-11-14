"""Offline regression: Snowflake ``fct_orders_incremental`` timestamp edge contract.

Renders the real dbt model (and ``stg_orders``) with a minimal subset of dbt
Jinja, transpiles it from Snowflake SQL to DuckDB with ``sqlglot``, and runs it
against synthetic rows in an in-memory DuckDB database. Incremental runs apply
a local model of dbt's ``merge`` strategy on the declared ``unique_key``; full
refreshes rebuild the target from the same final source.

Nothing here connects to Snowflake or any other service. The harness checks
model *logic* (filters, watermark, keys) only. It does not prove Snowflake
adapter behavior, casts, session time zones, merge semantics, or cost.
"""

from __future__ import annotations

import ast
import csv
import os
import re
import unittest
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import duckdb
    import sqlglot
    from sqlglot import exp
except ModuleNotFoundError as error:
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name not in {"duckdb", "sqlglot"}:
        raise
    duckdb = None
    sqlglot = None


ROOT = Path(__file__).resolve().parents[2]
SF_KIT = ROOT / "editions" / "Snowflake" / "kit"
SF_MODELS = SF_KIT / "models"
SF_INCREMENTAL = SF_MODELS / "marts" / "fct_orders_incremental.sql"
SF_STAGING = SF_MODELS / "staging" / "stg_orders.sql"
SF_CONTRACT_TEST = SF_KIT / "tests" / "assert_fct_orders_incremental_timestamp_contract.sql"
SF_COLUMNS = ["order_id", "customer_id", "order_date", "status", "amount", "source_updated_at"]

# Pinned build date. The seed watermark (max valid source_updated_at) is 2026-01-12 10:00:00.
AS_OF = date(2026, 1, 20)

CONFIG_RE = re.compile(r"\{\{\s*config\((?P<body>.*?)\)\s*\}\}", re.S)
IF_INCREMENTAL_RE = re.compile(
    r"\{%-?\s*if\s+is_incremental\(\)\s*-?%\}(?P<body>.*?)\{%-?\s*endif\s*-?%\}", re.S
)
REF_RE = re.compile(r"\{\{\s*ref\(\s*'(?P<name>[^']+)'\s*\)\s*\}\}")
VAR_RE = re.compile(r"\{\{\s*var\(\s*'(?P<name>[^']+)'\s*(?:,\s*(?P<default>[^)]*?))?\s*\)\s*\}\}")
THIS_RE = re.compile(r"\{\{\s*this\s*\}\}")


class RenderError(AssertionError):
    pass


class MergeError(AssertionError):
    """A target row matched more than one source row (Snowflake's default nondeterministic-merge error)."""


def parse_config(body: str) -> dict[str, Any]:
    call = ast.parse(f"config({body})", mode="eval").body
    assert isinstance(call, ast.Call)
    config: dict[str, Any] = {}
    for keyword in call.keywords:
        try:
            config[keyword.arg] = ast.literal_eval(keyword.value)
        except ValueError:
            config[keyword.arg] = ast.unparse(keyword.value)
    return config


def render(
    path: Path,
    *,
    incremental: bool,
    refs: dict[str, str],
    variables: dict[str, Any] | None = None,
    this: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Render the dbt Jinja subset these models use; refuse anything else."""
    text = path.read_text(encoding="utf-8")
    config: dict[str, Any] = {}
    for match in CONFIG_RE.finditer(text):
        config.update(parse_config(match.group("body")))
    text = CONFIG_RE.sub("", text)
    text = IF_INCREMENTAL_RE.sub(lambda m: m.group("body") if incremental else "", text)
    variables = variables or {}

    def ref(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in refs:
            raise RenderError(f"{path.name}: unmapped ref({name!r})")
        return refs[name]

    def var(match: re.Match[str]) -> str:
        name = match.group("name")
        if name in variables:
            return str(variables[name])
        if match.group("default") is None:
            raise RenderError(f"{path.name}: var({name!r}) has no default")
        return str(ast.literal_eval(match.group("default")))

    def this_relation(_: re.Match[str]) -> str:
        if this is None:
            raise RenderError(f"{path.name}: {{{{ this }}}} used without an existing target")
        return this

    text = REF_RE.sub(ref, text)
    text = VAR_RE.sub(var, text)
    text = THIS_RE.sub(this_relation, text)
    leftover = re.search(r"\{\{|\{%|\{#", text)
    if leftover:
        raise RenderError(f"{path.name}: unsupported Jinja near {text[leftover.start():leftover.start() + 60]!r}")
    return text, config


def to_duckdb(sql: str, as_of: date | None) -> str:
    """Transpile Snowflake SQL to DuckDB, pinning current_date() to ``as_of``."""
    tree = sqlglot.parse_one(sql, read="snowflake")

    def pin_clock(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.CurrentDate):
            if as_of is None:
                raise RenderError("model reads current_date() but no as_of date was supplied")
            return exp.cast(exp.Literal.string(as_of.isoformat()), exp.DataType.build("date"))
        if isinstance(node, (exp.CurrentTimestamp, exp.CurrentTime)):
            raise RenderError("unpinned wall-clock function in model SQL")
        return node

    return tree.transform(pin_clock).sql(dialect="duckdb")


class OrdersHarness:
    """Run fct_orders_incremental and stg_orders in local DuckDB."""

    def __init__(self, variables: dict[str, Any] | None = None) -> None:
        self.variables = variables or {}
        self.refs = {"raw_orders": "raw_orders", "stg_orders": "stg_orders"}
        self.connection = duckdb.connect(":memory:")
        self.connection.execute("SET TimeZone = 'UTC'")
        # dbt seed type inference is not modeled; staging casts every column explicitly.
        columns = ", ".join(f"{column} VARCHAR" for column in SF_COLUMNS)
        self.connection.execute(f"CREATE TABLE raw_orders ({columns})")
        staging_sql, _ = render(SF_STAGING, incremental=False, refs=self.refs, variables=self.variables)
        self.connection.execute(f"CREATE VIEW stg_orders AS {to_duckdb(staging_sql, None)}")
        _, self.config = render(SF_INCREMENTAL, incremental=False, refs=self.refs, variables=self.variables)
        if self.config.get("incremental_strategy") != "merge" or self.config.get("unique_key") != "order_id":
            raise AssertionError(f"harness models a merge on order_id, got {self.config!r}")

    def close(self) -> None:
        self.connection.close()

    def set_source(self, rows: Iterable[Sequence[Any]]) -> None:
        self.connection.execute("DELETE FROM raw_orders")
        rows = list(rows)
        if rows:
            self.connection.executemany(f"INSERT INTO raw_orders VALUES ({', '.join('?' for _ in SF_COLUMNS)})", rows)

    def _sql(self, *, incremental: bool, target: str, as_of: date) -> str:
        sql, _ = render(
            SF_INCREMENTAL,
            incremental=incremental,
            refs=self.refs,
            variables=self.variables,
            this=target if incremental else None,
        )
        return to_duckdb(sql, as_of)

    def full_refresh(self, target: str, *, as_of: date = AS_OF) -> None:
        self.connection.execute(f"CREATE OR REPLACE TABLE {target} AS {self._sql(incremental=False, target=target, as_of=as_of)}")

    def incremental_run(self, target: str, *, as_of: date = AS_OF) -> set[int]:
        """Apply dbt merge semantics (update matched keys, insert the rest); return selected order_ids."""
        connection = self.connection
        connection.execute(f"CREATE OR REPLACE TEMP TABLE __batch AS {self._sql(incremental=True, target=target, as_of=as_of)}")
        match = f"{target}.order_id = __batch.order_id"
        joined = connection.execute(f"SELECT count(*) FROM {target} JOIN __batch ON {match}").fetchone()[0]
        matched = connection.execute(f"SELECT count(*) FROM {target} WHERE EXISTS (SELECT 1 FROM __batch WHERE {match})").fetchone()[0]
        if joined != matched:
            raise MergeError(f"{joined - matched} extra source match(es) for existing {target} keys")
        connection.execute(f"DELETE FROM {target} WHERE EXISTS (SELECT 1 FROM __batch WHERE {match})")
        connection.execute(f"INSERT INTO {target} BY NAME SELECT * FROM __batch")
        return {row[0] for row in connection.execute("SELECT order_id FROM __batch").fetchall()}

    def contract_violations(self, target: str, *, as_of: date = AS_OF) -> set[tuple[int, str]]:
        """Run the real singular dbt test against ``target``; return its violation rows."""
        refs = {**self.refs, "fct_orders_incremental": target}
        sql, _ = render(SF_CONTRACT_TEST, incremental=False, refs=refs, variables=self.variables)
        return {tuple(row) for row in self.connection.execute(to_duckdb(sql, as_of)).fetchall()}

    def rows(self, target: str) -> list[tuple[Any, ...]]:
        return self.connection.execute(f"SELECT * FROM {target} ORDER BY ALL").fetchall()

    def divergent_keys(self, left: str, right: str) -> set[int]:
        query = f"""
            SELECT DISTINCT order_id FROM (
                (SELECT * FROM {left} EXCEPT ALL SELECT * FROM {right})
                UNION ALL
                (SELECT * FROM {right} EXCEPT ALL SELECT * FROM {left})
            )
        """
        return {row[0] for row in self.connection.execute(query).fetchall()}

    def row_for(self, target: str, order_id: int) -> dict[str, Any]:
        cursor = self.connection.execute(f"SELECT * FROM {target} WHERE order_id = ?", [order_id])
        columns = [column[0] for column in cursor.description]
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise AssertionError(f"expected one {target} row for order {order_id}, found {len(rows)}")
        return dict(zip(columns, rows[0]))


def seed_rows() -> list[tuple[Any, ...]]:
    with (SF_KIT / "seeds" / "raw_orders.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == SF_COLUMNS, reader.fieldnames
        return [tuple(row[column] or None for column in SF_COLUMNS) for row in reader]


def replace_order(rows: list[tuple[Any, ...]], order_id: str, **changes: Any) -> list[tuple[Any, ...]]:
    updated = []
    for row in rows:
        if row[0] == order_id:
            record = dict(zip(SF_COLUMNS, row))
            record.update(changes)
            row = tuple(record[column] for column in SF_COLUMNS)
        updated.append(row)
    return updated


def order(order_id: str, updated_at: str | None, amount: str = "20.00") -> tuple[Any, ...]:
    return (order_id, "1001", "2026-01-13", "completed", amount, updated_at)


@unittest.skipIf(duckdb is None, "duckdb/sqlglot test dependencies are not installed")
class SnowflakeIncrementalTimestampContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.seed = seed_rows()
        # Fixture assumptions used by the window arithmetic below.
        by_id = {row[0]: row[5] for row in self.seed}
        self.assertEqual(by_id["5004"], "2026-01-12 10:00:00")  # valid watermark
        self.assertEqual(by_id["5003"], "2026-01-09 10:00:00")  # exactly on the default window start
        self.assertIsNone(by_id["5005"])  # missing
        self.assertEqual(by_id["5006"], "2099-01-01 00:00:00")  # future
        self.harness = self.make_harness()

    def make_harness(self, **variables: Any) -> OrdersHarness:
        harness = OrdersHarness(variables)
        self.addCleanup(harness.close)
        return harness

    def build_both(self, harness: OrdersHarness, changed: list[tuple[Any, ...]]) -> set[int]:
        harness.set_source(self.seed)
        harness.full_refresh("incremental_target")
        harness.set_source(changed)
        selected = harness.incremental_run("incremental_target")
        harness.full_refresh("full_refresh_target")
        return selected

    # -- null source_updated_at ------------------------------------------- #

    def test_null_rows_are_reselected_on_consecutive_runs_and_match_full_refresh(self) -> None:
        harness = self.harness
        harness.set_source(self.seed)
        harness.full_refresh("incremental_target")

        # Run 1: an existing order loses its timestamp and changes amount; a new null-timestamp order arrives.
        run1 = replace_order(self.seed, "5002", amount="99.00", source_updated_at=None)
        run1.append(order("5007", None, amount="11.00"))
        harness.set_source(run1)
        selected1 = harness.incremental_run("incremental_target")
        self.assertTrue({5002, 5005, 5007} <= selected1, selected1)
        harness.full_refresh("full_refresh_target")
        self.assertEqual(harness.rows("incremental_target"), harness.rows("full_refresh_target"))

        # Run 2: only null-timestamp rows change; they are still selected and stay in step.
        run2 = replace_order(run1, "5005", status="cancelled", amount="61.00")
        run2 = replace_order(run2, "5007", amount="12.00")
        harness.set_source(run2)
        selected2 = harness.incremental_run("incremental_target")
        self.assertTrue({5002, 5005, 5007} <= selected2, selected2)
        harness.full_refresh("full_refresh_target")
        self.assertEqual(harness.rows("incremental_target"), harness.rows("full_refresh_target"))

        row = harness.row_for("incremental_target", 5005)
        self.assertEqual((row["status"], str(row["amount"]), row["watermark_status"]), ("cancelled", "61.00", "missing"))
        self.assertIsNone(row["source_updated_at"])

    def test_null_rows_never_set_the_watermark(self) -> None:
        # With every valid timestamp removed, the window falls back to selecting everything.
        only_nulls = [replace_order([row], row[0], source_updated_at=None)[0] for row in self.seed]
        harness = self.harness
        harness.set_source(only_nulls)
        harness.full_refresh("incremental_target")
        selected = harness.incremental_run("incremental_target")
        self.assertEqual(selected, {int(row[0]) for row in self.seed})

    def test_rerun_without_changes_reselects_null_future_and_window_rows_only(self) -> None:
        selected = self.build_both(self.harness, self.seed)
        # 5003 (inclusive window start), 5004 (watermark), 5005 (null), 5006 (future); not 5001/5002.
        self.assertEqual(selected, {5003, 5004, 5005, 5006})
        self.assertEqual(self.harness.incremental_run("incremental_target"), {5003, 5004, 5005, 5006})
        self.assertEqual(self.harness.rows("incremental_target"), self.harness.rows("full_refresh_target"))

    # -- future-dated source_updated_at ----------------------------------- #

    def test_future_dated_row_does_not_freeze_the_watermark(self) -> None:
        harness = self.harness
        harness.set_source(self.seed)
        harness.full_refresh("incremental_target")
        typo = replace_order(self.seed, "5004", source_updated_at="2027-01-12 10:00:00")
        harness.set_source(typo)
        harness.incremental_run("incremental_target")
        self.assertEqual(harness.row_for("incremental_target", 5004)["watermark_status"], "future")

        later = replace_order(typo, "5001", amount="140.00", source_updated_at="2026-01-14 10:00:00")
        harness.set_source(later)
        selected = harness.incremental_run("incremental_target")
        harness.full_refresh("full_refresh_target")

        self.assertIn(5001, selected)
        self.assertEqual(harness.divergent_keys("incremental_target", "full_refresh_target"), set())

    def test_skew_var_bounds_future_cutoff_inclusively(self) -> None:
        # Default skew 1 day: cutoff is 2026-01-21 00:00:00 for an AS_OF of 2026-01-20.
        changed = self.seed + [
            order("5007", "2026-01-20 23:59:59"),  # valid: just before the cutoff
            order("5008", "2026-01-21 00:00:00"),  # future: exactly on the cutoff
        ]
        self.build_both(self.harness, changed)
        self.assertEqual(self.harness.row_for("incremental_target", 5007)["watermark_status"], "valid")
        self.assertEqual(self.harness.row_for("incremental_target", 5008)["watermark_status"], "future")

        # A wider skew of 3 days accepts 2026-01-22 as valid, so it now sets the watermark.
        wide = self.make_harness(incremental_future_skew_days=3)
        changed = self.seed + [order("5008", "2026-01-22 12:00:00")]
        self.build_both(wide, changed)
        self.assertEqual(wide.row_for("incremental_target", 5008)["watermark_status"], "valid")
        self.assertEqual(wide.incremental_run("incremental_target"), {5005, 5006, 5008})

    def test_future_row_becomes_valid_once_build_date_passes_it(self) -> None:
        harness = self.harness
        changed = self.seed + [order("5007", "2026-01-25 10:00:00")]
        harness.set_source(changed)
        harness.full_refresh("incremental_target")
        self.assertEqual(harness.row_for("incremental_target", 5007)["watermark_status"], "future")
        harness.incremental_run("incremental_target", as_of=date(2026, 1, 26))
        harness.full_refresh("full_refresh_target", as_of=date(2026, 1, 26))
        self.assertEqual(harness.row_for("incremental_target", 5007)["watermark_status"], "valid")
        self.assertEqual(harness.rows("incremental_target"), harness.rows("full_refresh_target"))

    def test_stored_future_row_is_rewritten_valid_after_later_rows_pass_the_cutoff(self) -> None:
        # Two future rows; by the later build date both are valid. The watermark comes only from
        # rows stored as valid, so the older one is not stranded below the window as "future".
        harness = self.harness
        changed = self.seed + [order("5007", "2026-01-25 10:00:00"), order("5008", "2026-01-30 10:00:00")]
        harness.set_source(changed)
        harness.full_refresh("incremental_target")
        later = date(2026, 2, 10)
        selected = harness.incremental_run("incremental_target", as_of=later)
        harness.full_refresh("full_refresh_target", as_of=later)
        self.assertTrue({5007, 5008} <= selected, selected)
        self.assertEqual(harness.row_for("incremental_target", 5007)["watermark_status"], "valid")
        self.assertEqual(harness.rows("incremental_target"), harness.rows("full_refresh_target"))
        self.assertEqual(harness.contract_violations("incremental_target", as_of=later), set())

    # -- singular dbt test ------------------------------------------------- #

    def test_singular_contract_test_passes_after_full_refresh_and_incremental_run(self) -> None:
        harness = self.harness
        changed = replace_order(self.seed, "5002", amount="81.00", source_updated_at="2026-01-10 10:00:00")
        self.build_both(harness, changed)
        self.assertEqual(harness.contract_violations("incremental_target"), set())
        self.assertEqual(harness.contract_violations("full_refresh_target"), set())

    def test_singular_contract_test_flags_stale_watermark_status(self) -> None:
        harness = self.harness
        harness.set_source(self.seed)
        harness.full_refresh("incremental_target")
        harness.connection.execute(
            "UPDATE incremental_target SET watermark_status = 'future' WHERE order_id = 5004"
        )
        self.assertIn((5004, "valid_row_not_current"), harness.contract_violations("incremental_target"))

    # -- late arrivals ----------------------------------------------------- #

    def test_late_arrival_inside_lookback_is_recovered(self) -> None:
        # Window start is 2026-01-12 10:00:00 - 3 days = 2026-01-09 10:00:00 (inclusive).
        changed = replace_order(self.seed, "5002", amount="81.00", source_updated_at="2026-01-09 10:00:00")
        changed.append(order("5007", "2026-01-10 08:00:00"))
        selected = self.build_both(self.harness, changed)
        self.assertTrue({5002, 5007} <= selected, selected)
        self.assertEqual(self.harness.divergent_keys("incremental_target", "full_refresh_target"), set())

    def test_late_arrival_before_window_start_is_missed_until_full_refresh(self) -> None:
        changed = replace_order(self.seed, "5001", amount="126.00", source_updated_at="2026-01-09 09:59:59")
        self.build_both(self.harness, changed)
        self.assertEqual(self.harness.divergent_keys("incremental_target", "full_refresh_target"), {5001})
        self.harness.full_refresh("incremental_target")
        self.assertEqual(self.harness.rows("incremental_target"), self.harness.rows("full_refresh_target"))

    def test_wider_lookback_var_recovers_older_late_arrival(self) -> None:
        wide = self.make_harness(incremental_lookback_days=7)
        changed = replace_order(self.seed, "5001", amount="126.00", source_updated_at="2026-01-06 00:00:00")
        selected = self.build_both(wide, changed)
        self.assertIn(5001, selected)
        self.assertEqual(wide.divergent_keys("incremental_target", "full_refresh_target"), set())

    def test_duplicate_rows_for_existing_order_fail_merge(self) -> None:
        harness = self.harness
        harness.set_source(self.seed)
        harness.full_refresh("incremental_target")
        harness.set_source(self.seed + [("5004", "1003", "2026-01-12", "returned", "215.25", "2026-01-13 10:00:00")])
        with self.assertRaises(MergeError):
            harness.incremental_run("incremental_target")


if __name__ == "__main__":
    unittest.main()
