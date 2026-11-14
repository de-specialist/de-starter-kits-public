"""Offline regression: BigQuery incremental mart drops stale groups inside its lookback.

``mart_daily_product_sales_incremental`` rebuilds every daily partition in its
lookback window with dbt-bigquery's *static* ``insert_overwrite`` strategy. This
test renders the real model and staging SQL, transpiles it from BigQuery to
DuckDB with ``sqlglot``, and emulates the adapter's static insert_overwrite
(``MERGE ... ON FALSE WHEN NOT MATCHED BY SOURCE AND <partition> IN (...)
THEN DELETE WHEN NOT MATCHED THEN INSERT``) in an in-memory DuckDB database.

It checks that, when an order item is deleted or moves to a different
(date, product) group inside the window, an incremental run equals a full
refresh. It also pins the documented limit: groups dated before the window
are not rebuilt and still need ``--full-refresh``.

Nothing here connects to BigQuery. Rendering supports only the Jinja shapes
used by these models, dialect translation goes through sqlglot, and the
adapter merge is modeled, not executed. It checks model logic, not warehouse
SQL, types, permissions, or cost.
"""

from __future__ import annotations

import ast
import os
import re
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

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
BQ_MODELS = ROOT / "editions" / "Bigquery" / "models"
MODEL = BQ_MODELS / "marts" / "mart_daily_product_sales_incremental.sql"
CORE_MART = BQ_MODELS / "marts" / "mart_daily_product_sales.sql"
STAGING = {
    "stg_order_items": BQ_MODELS / "staging" / "stg_order_items.sql",
    "stg_products": BQ_MODELS / "staging" / "stg_products.sql",
}
SOURCES = {("thelook", "order_items"): "thelook_order_items", ("thelook", "products"): "thelook_products"}
RAW_TABLES = {
    "thelook_order_items": [
        ("id", "BIGINT"), ("order_id", "BIGINT"), ("user_id", "BIGINT"), ("product_id", "BIGINT"),
        ("status", "VARCHAR"), ("created_at", "TIMESTAMP"), ("sale_price", "DOUBLE"),
    ],
    "thelook_products": [
        ("id", "BIGINT"), ("name", "VARCHAR"), ("category", "VARCHAR"), ("department", "VARCHAR"),
        ("brand", "VARCHAR"), ("cost", "DOUBLE"), ("retail_price", "DOUBLE"),
    ],
}


# --------------------------------------------------------------------------- #
# Minimal rendering of the model's Jinja                                       #
# --------------------------------------------------------------------------- #

COMMENT_RE = re.compile(r"\{#.*?#\}", re.S)
CONFIG_RE = re.compile(r"\{\{\s*config\((?P<body>.*?)\)\s*\}\}", re.S)
IF_INCREMENTAL_RE = re.compile(
    r"\{%-?\s*if\s+is_incremental\(\)\s*-?%\}(?P<body>.*?)\{%-?\s*endif\s*-?%\}", re.S
)
LOOKBACK_SET_RE = re.compile(
    r"\{%-?\s*set\s+lookback_days\s*=\s*var\(\s*'incremental_lookback_days'\s*,\s*(?P<default>\d+)\s*\)\s*-?%\}"
)
# The preamble that validates the lookback and builds the static partition list.
PREAMBLE_RE = re.compile(r"\A\s*\{%-?\s*set\s+lookback_days.*?\{%-?\s*endfor\s*-?%\}", re.S)
PARTITION_LOOP = (
    "{%- for days_ago in range(lookback_days + 1) -%}\n"
    "  {%- do partitions_to_replace.append('date_sub(current_date(), interval ' ~ days_ago ~ ' day)') -%}\n"
    "{%- endfor -%}"
)
REF_RE = re.compile(r"\{\{\s*ref\(\s*'(?P<name>[^']+)'\s*\)\s*\}\}")
SOURCE_RE = re.compile(r"\{\{\s*source\(\s*'(?P<source>[^']+)'\s*,\s*'(?P<table>[^']+)'\s*\)\s*\}\}")


class RenderError(AssertionError):
    pass


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


def default_lookback_days(text: str) -> int:
    match = LOOKBACK_SET_RE.search(text)
    if match is None:
        raise RenderError("model must set lookback_days from var('incremental_lookback_days', <int>)")
    return int(match.group("default"))


def render(path: Path, *, incremental: bool, variables: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """Render the Jinja subset these models use; refuse anything else."""
    variables = variables or {}
    text = COMMENT_RE.sub("", path.read_text(encoding="utf-8"))
    preamble = PREAMBLE_RE.search(text)
    lookback_days: int | None = None
    if preamble is not None:
        if PARTITION_LOOP not in preamble.group(0):
            raise RenderError("static partition loop changed; update this emulation to match")
        lookback_days = int(variables.get("incremental_lookback_days", default_lookback_days(text)))
        text = text[: preamble.start()] + text[preamble.end():]

    config: dict[str, Any] = {}
    for match in CONFIG_RE.finditer(text):
        config.update(parse_config(match.group("body")))
    text = CONFIG_RE.sub("", text)
    text = IF_INCREMENTAL_RE.sub(lambda m: m.group("body") if incremental else "", text)
    if lookback_days is not None:
        text = re.sub(r"\{\{\s*lookback_days\s*\}\}", str(lookback_days), text)
        # Mirrors the preamble loop: today and each of the previous lookback_days days.
        config["partitions"] = [
            f"date_sub(current_date(), interval {days_ago} day)" for days_ago in range(lookback_days + 1)
        ]
    text = REF_RE.sub(lambda m: m.group("name"), text)

    def source(match: re.Match[str]) -> str:
        key = (match.group("source"), match.group("table"))
        if key not in SOURCES:
            raise RenderError(f"{path.name}: unmapped source{key!r}")
        return SOURCES[key]

    text = SOURCE_RE.sub(source, text)
    leftover = re.search(r"\{[{%#]", text)
    if leftover:
        raise RenderError(f"{path.name}: unsupported Jinja near {text[leftover.start():leftover.start() + 60]!r}")
    return text, config


def to_duckdb(sql: str, as_of: date | None) -> str:
    """Transpile BigQuery SQL to DuckDB, pinning current_date() to ``as_of``."""

    def pin_clock(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.CurrentDate):
            if as_of is None:
                raise RenderError("SQL reads current_date() but no as_of date was supplied")
            return exp.cast(exp.Literal.string(as_of.isoformat()), exp.DataType.build("date"))
        if isinstance(node, (exp.CurrentTimestamp, exp.CurrentTime)):
            raise RenderError("unpinned wall-clock function in model SQL")
        return node

    return sqlglot.parse_one(sql, read="bigquery").transform(pin_clock).sql(dialect="duckdb")


# --------------------------------------------------------------------------- #
# Harness                                                                      #
# --------------------------------------------------------------------------- #

class BigQueryInsertOverwriteHarness:
    """Run the incremental mart against synthetic TheLook-shaped rows in DuckDB."""

    def __init__(self, variables: dict[str, Any] | None = None) -> None:
        self.variables = variables or {}
        self.connection = duckdb.connect(":memory:")
        self.connection.execute("SET TimeZone = 'UTC'")
        for table, columns in RAW_TABLES.items():
            self.connection.execute(f"CREATE TABLE {table} ({', '.join(f'{c} {t}' for c, t in columns)})")
        for name, path in STAGING.items():
            sql, _ = render(path, incremental=False)
            self.connection.execute(f"CREATE VIEW {name} AS {to_duckdb(sql, None)}")
        _, self.config = render(MODEL, incremental=True, variables=self.variables)
        self.partition_field = self.config["partition_by"]["field"]

    def close(self) -> None:
        self.connection.close()

    def set_source(self, table: str, rows: Sequence[Sequence[Any]]) -> None:
        self.connection.execute(f"DELETE FROM {table}")
        if rows:
            placeholders = ", ".join("?" for _ in RAW_TABLES[table])
            self.connection.executemany(f"INSERT INTO {table} VALUES ({placeholders})", list(rows))

    def full_refresh(self, target: str, *, as_of: date) -> None:
        sql, _ = render(MODEL, incremental=False, variables=self.variables)
        self.connection.execute(f"CREATE OR REPLACE TABLE {target} AS {to_duckdb(sql, as_of)}")

    def replaced_partitions(self, as_of: date) -> list[date]:
        expressions = ", ".join(self.config["partitions"])
        query = to_duckdb(f"select cast(d as date) from unnest([{expressions}]) as d", as_of)
        return sorted(row[0] for row in self.connection.execute(query).fetchall())

    def incremental_run(self, target: str, *, as_of: date, static: bool = True) -> None:
        """Emulate dbt-bigquery insert_overwrite.

        static=True is the model's configured mode: every listed partition is
        deleted, then the batch is inserted. static=False models the dynamic
        mode (only partitions present in the batch are replaced) as a control.
        """
        if self.config.get("incremental_strategy") != "insert_overwrite":
            raise AssertionError("harness only models the insert_overwrite strategy")
        sql, _ = render(MODEL, incremental=True, variables=self.variables)
        connection = self.connection
        field = self.partition_field
        connection.execute(f"CREATE OR REPLACE TEMP TABLE __batch AS {to_duckdb(sql, as_of)}")
        if static:
            partitions = self.replaced_partitions(as_of)
            outside = connection.execute(
                f"SELECT count(*) FROM __batch WHERE {field} NOT IN (SELECT unnest(?::DATE[]))", [partitions]
            ).fetchone()[0]
            if outside:
                # The adapter would append these rows without deleting their partition.
                raise AssertionError(f"{outside} batch row(s) fall outside the static partitions list")
            connection.execute(f"DELETE FROM {target} WHERE {field} IN (SELECT unnest(?::DATE[]))", [partitions])
        else:
            connection.execute(f"DELETE FROM {target} WHERE {field} IN (SELECT DISTINCT {field} FROM __batch)")
        connection.execute(f"INSERT INTO {target} BY NAME SELECT * FROM __batch")

    def rows(self, target: str) -> list[tuple[Any, ...]]:
        return self.connection.execute(f"SELECT * FROM {target} ORDER BY ALL").fetchall()

    def divergent_keys(self, left: str, right: str) -> set[tuple[Any, ...]]:
        query = f"""
            SELECT DISTINCT order_item_date, product_id FROM (
                (SELECT * FROM {left} EXCEPT ALL SELECT * FROM {right})
                UNION ALL
                (SELECT * FROM {right} EXCEPT ALL SELECT * FROM {left})
            )
        """
        return set(self.connection.execute(query).fetchall())

    def has_group(self, target: str, key: tuple[date, int]) -> bool:
        return bool(self.connection.execute(
            f"SELECT count(*) FROM {target} WHERE order_item_date = ? AND product_id = ?", list(key)
        ).fetchone()[0])


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

AS_OF = date(2026, 6, 30)
YESTERDAY = AS_OF - timedelta(days=1)
OLD = AS_OF - timedelta(days=120)  # outside the default 90-day window
MID = AS_OF - timedelta(days=10)
RECENT = AS_OF - timedelta(days=1)
SOLO = AS_OF - timedelta(days=5)  # a date with a single item, so its whole partition can empty out

PRODUCTS = [
    (1, "Widget", "Accessories", "Women", "Acme", 5.0, 25.0),
    (2, "Gadget", "Electronics", "Men", "Acme", 6.0, 30.0),
    (3, "Gizmo", "Electronics", "Women", "Bolt", 2.0, 10.0),
]


def item(item_id: int, order_id: int, product_id: int, status: str, day: date, price: float) -> tuple[Any, ...]:
    created_at = datetime.combine(day, datetime.min.time()) + timedelta(hours=12)
    return (item_id, order_id, 900 + order_id, product_id, status, created_at, price)


ITEMS = [
    item(1, 101, 1, "complete", MID, 20.0),
    item(2, 101, 1, "processing", MID, 5.0),
    item(3, 102, 1, "returned", MID, 7.0),
    item(4, 103, 2, "complete", OLD, 12.0),
    item(5, 104, 2, "shipped", OLD, 30.0),
    item(6, 105, 3, "complete", RECENT, 9.0),  # only item in (RECENT, 3)
    item(7, 106, 2, "complete", RECENT, 15.0),
    item(8, 107, 3, "complete", SOLO, 11.0),  # only item dated SOLO
]


def without(items: list[tuple[Any, ...]], item_id: int) -> list[tuple[Any, ...]]:
    return [row for row in items if row[0] != item_id]


def moved(items: list[tuple[Any, ...]], item_id: int, *, day: date | None = None, product_id: int | None = None):
    updated = []
    for row in items:
        if row[0] == item_id:
            row = list(row)
            if day is not None:
                row[5] = datetime.combine(day, datetime.min.time()) + timedelta(hours=12)
            if product_id is not None:
                row[3] = product_id
            row = tuple(row)
        updated.append(row)
    return updated


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

@unittest.skipIf(duckdb is None, "duckdb/sqlglot test dependencies are not installed")
class IncrementalModelContractTests(unittest.TestCase):
    def test_config_uses_static_insert_overwrite_on_date_partitions(self) -> None:
        text = MODEL.read_text(encoding="utf-8")
        raw_config = parse_config(CONFIG_RE.search(text).group("body"))
        self.assertEqual(raw_config["materialized"], "incremental")
        self.assertEqual(raw_config["enabled"], "var('enable_incremental_example', false)")
        self.assertEqual(raw_config["incremental_strategy"], "insert_overwrite")
        self.assertEqual(raw_config["partitions"], "partitions_to_replace")
        self.assertEqual(raw_config["partition_by"], {"field": "order_item_date", "data_type": "date"})
        self.assertEqual(raw_config["cluster_by"], ["product_id"])
        self.assertNotIn("unique_key", raw_config)
        self.assertEqual(default_lookback_days(text), 90)

    def test_lookback_is_bounded_with_a_compile_error(self) -> None:
        text = MODEL.read_text(encoding="utf-8")
        self.assertIn("lookback_days > 3650", text)
        self.assertIn("lookback_days < 0", text)
        self.assertIn("exceptions.raise_compiler_error", text)

    def test_source_filter_selects_exactly_the_replaced_partitions(self) -> None:
        sql, config = render(MODEL, incremental=True)
        where = sqlglot.parse_one(sql, read="bigquery").find(exp.Where)
        self.assertEqual({column.name for column in where.find_all(exp.Column)}, {"order_item_date"})
        self.assertEqual(len(config["partitions"]), 91)
        harness = BigQueryInsertOverwriteHarness()
        self.addCleanup(harness.close)
        partitions = harness.replaced_partitions(AS_OF)
        self.assertEqual(partitions[0], AS_OF - timedelta(days=90))
        self.assertEqual(partitions[-1], AS_OF)
        self.assertEqual(len(set(partitions)), 91)

    def test_full_refresh_sql_matches_core_daily_mart(self) -> None:
        incremental_sql, _ = render(MODEL, incremental=False)
        core_sql, _ = render(CORE_MART, incremental=False)
        normalize = lambda sql: sqlglot.parse_one(sql, read="bigquery").sql(dialect="bigquery")
        self.assertEqual(normalize(incremental_sql), normalize(core_sql))


@unittest.skipIf(duckdb is None, "duckdb/sqlglot test dependencies are not installed")
class StaleGroupRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = BigQueryInsertOverwriteHarness()
        self.addCleanup(self.harness.close)

    def scenario(self, changed_items, *, static: bool = True) -> BigQueryInsertOverwriteHarness:
        """Build yesterday on the base data, then run incrementally and fully refresh today."""
        harness = self.harness
        harness.set_source("thelook_products", PRODUCTS)
        harness.set_source("thelook_order_items", ITEMS)
        harness.full_refresh("incremental_target", as_of=YESTERDAY)
        harness.set_source("thelook_order_items", changed_items)
        harness.incremental_run("incremental_target", as_of=AS_OF, static=static)
        harness.full_refresh("full_refresh_target", as_of=AS_OF)
        return harness

    def assert_matches_full_refresh(self, harness: BigQueryInsertOverwriteHarness) -> None:
        self.assertEqual(harness.divergent_keys("incremental_target", "full_refresh_target"), set())
        self.assertEqual(harness.rows("incremental_target"), harness.rows("full_refresh_target"))

    def test_deleted_last_item_drops_its_group(self) -> None:
        harness = self.scenario(without(ITEMS, 6))
        self.assertFalse(harness.has_group("incremental_target", (RECENT, 3)))
        self.assert_matches_full_refresh(harness)

    def test_deleted_item_that_empties_a_whole_date_drops_its_group(self) -> None:
        harness = self.scenario(without(ITEMS, 8))
        self.assertFalse(harness.has_group("incremental_target", (SOLO, 3)))
        self.assert_matches_full_refresh(harness)

    def test_item_moved_to_another_date_moves_its_aggregate(self) -> None:
        harness = self.scenario(moved(ITEMS, 6, day=MID))
        self.assertFalse(harness.has_group("incremental_target", (RECENT, 3)))
        self.assertTrue(harness.has_group("incremental_target", (MID, 3)))
        self.assert_matches_full_refresh(harness)

    def test_item_moved_to_another_product_moves_its_aggregate(self) -> None:
        harness = self.scenario(moved(ITEMS, 6, product_id=1))
        self.assertFalse(harness.has_group("incremental_target", (RECENT, 3)))
        self.assertTrue(harness.has_group("incremental_target", (RECENT, 1)))
        self.assert_matches_full_refresh(harness)

    def test_item_moved_off_a_single_item_date_matches_full_refresh(self) -> None:
        harness = self.scenario(moved(ITEMS, 8, day=RECENT, product_id=1))
        self.assertFalse(harness.has_group("incremental_target", (SOLO, 3)))
        self.assert_matches_full_refresh(harness)

    def test_dynamic_partition_replacement_would_leave_a_vacated_date_stale(self) -> None:
        """Control: only static partitions remove a date whose every item left."""
        harness = self.scenario(without(ITEMS, 8), static=False)
        self.assertEqual(harness.divergent_keys("incremental_target", "full_refresh_target"), {(SOLO, 3)})

    def test_rerun_without_source_changes_is_idempotent(self) -> None:
        harness = self.scenario(ITEMS)
        harness.incremental_run("incremental_target", as_of=AS_OF)
        self.assert_matches_full_refresh(harness)

    def test_changes_before_the_window_still_need_full_refresh(self) -> None:
        changed = moved(without(ITEMS, 5), 4, product_id=3)  # both OLD, outside the window
        changed = without(changed, 6)  # inside the window
        harness = self.scenario(changed)
        self.assertEqual(
            harness.divergent_keys("incremental_target", "full_refresh_target"), {(OLD, 2), (OLD, 3)}
        )
        self.assertFalse(harness.has_group("incremental_target", (RECENT, 3)))
        harness.full_refresh("incremental_target", as_of=AS_OF)  # documented remedy
        self.assert_matches_full_refresh(harness)

    def test_lookback_override_bounds_the_rebuilt_window(self) -> None:
        harness = BigQueryInsertOverwriteHarness({"incremental_lookback_days": 7})
        self.addCleanup(harness.close)
        self.harness = harness
        # SOLO (5 days ago) is inside a 7-day window; MID (10 days ago) is not.
        changed = without(without(ITEMS, 8), 3)
        harness = self.scenario(changed)
        self.assertEqual(harness.replaced_partitions(AS_OF)[0], AS_OF - timedelta(days=7))
        self.assertFalse(harness.has_group("incremental_target", (SOLO, 3)))
        self.assertEqual(harness.divergent_keys("incremental_target", "full_refresh_target"), {(MID, 1)})

    def test_window_start_is_inclusive_and_the_day_before_is_untouched(self) -> None:
        edge = AS_OF - timedelta(days=90)
        beyond = edge - timedelta(days=1)
        base = ITEMS + [item(20, 120, 1, "complete", edge, 10.0), item(21, 121, 1, "complete", beyond, 10.0)]
        harness = self.harness
        harness.set_source("thelook_products", PRODUCTS)
        harness.set_source("thelook_order_items", base)
        harness.full_refresh("incremental_target", as_of=AS_OF)
        harness.set_source("thelook_order_items", without(without(base, 20), 21))
        harness.incremental_run("incremental_target", as_of=AS_OF)
        harness.full_refresh("full_refresh_target", as_of=AS_OF)
        self.assertFalse(harness.has_group("incremental_target", (edge, 1)))
        self.assertEqual(harness.divergent_keys("incremental_target", "full_refresh_target"), {(beyond, 1)})

    def test_future_dated_items_wait_until_their_date_enters_the_window(self) -> None:
        tomorrow = AS_OF + timedelta(days=1)
        changed = ITEMS + [item(30, 130, 2, "complete", tomorrow, 8.0)]
        harness = self.scenario(changed)
        self.assertEqual(harness.divergent_keys("incremental_target", "full_refresh_target"), {(tomorrow, 2)})
        harness.incremental_run("incremental_target", as_of=tomorrow)
        harness.full_refresh("full_refresh_target", as_of=tomorrow)
        self.assert_matches_full_refresh(harness)


if __name__ == "__main__":
    unittest.main()
