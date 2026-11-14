"""DuckDB-rendered SQL logic checks, not warehouse adapter behavior, for migration Gold."""

from __future__ import annotations

import importlib
import os
import re
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path


def _load_duckdb():
    try:
        return importlib.import_module("duckdb")
    except ModuleNotFoundError as error:
        if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name != "duckdb":
            raise
        return None


duckdb = _load_duckdb()
ROOT = Path(__file__).resolve().parents[2]
EDITIONS = (
    ROOT / "editions" / "Bigquery",
    ROOT / "editions" / "Snowflake" / "kit",
)


def _render(sql: str) -> str:
    sql = re.sub(r"\{\{\s*dbt\.type_string\(\)\s*\}\}", "varchar", sql)
    sql = re.sub(r"\{\{\s*dbt\.type_timestamp\(\)\s*\}\}", "timestamp", sql)
    sql = re.sub(r"\{\{\s*dbt\.type_numeric\(\)\s*\}\}", "decimal(38, 9)", sql)
    sql = re.sub(r"\{\{\s*source\('migration_bronze',\s*'[^']+'\)\s*\}\}", lambda m: m.group(0).split("'")[3], sql)
    sql = re.sub(r"\{\{\s*ref\('([^']+)'\)\s*\}\}", r"\1", sql)
    if "{{" in sql or "}}" in sql:
        raise AssertionError("Unrendered dbt expression in migration model")
    return sql


@unittest.skipIf(duckdb is None, "duckdb is required for offline migration Gold acceptance")
class MigrationGoldAcceptanceTests(unittest.TestCase):
    def _build_silver_models(self, connection, edition: Path) -> None:
        models = edition / "models" / "migration"
        for name in ("customers", "orders", "order_items"):
            model_path = models / "silver" / f"silver_{name}.sql"
            connection.execute(
                f"create table silver_{name} as "
                + _render(model_path.read_text(encoding="utf-8"))
            )

    def test_daily_sales_gold_metrics_and_grain_match_in_both_editions(self):
        for edition in EDITIONS:
            with self.subTest(edition=str(edition)):
                connection = duckdb.connect(database=":memory:")
                self.addCleanup(connection.close)
                connection.execute("""
                    create table orders(order_id varchar, customer_id varchar, order_status varchar,
                        order_date date, updated_at timestamp, _ingested_at_utc timestamp);
                    insert into orders values
                      ('o100', 'c1', 'complete', date '2025-01-01', timestamp '2025-01-01 12:00:00', timestamp '2025-01-02'),
                      ('o101', 'c1', 'returned', date '2025-01-01', timestamp '2025-01-01 13:00:00', timestamp '2025-01-02'),
                      ('o102', 'c2', 'completed', date '2025-01-01', timestamp '2025-01-01 14:00:00', timestamp '2025-01-02'),
                      ('o103', 'c2', 'cancelled', date '2025-01-01', timestamp '2025-01-01 15:00:00', timestamp '2025-01-02'),
                      ('o104', 'c1', 'complete', date '2025-01-02', timestamp '2025-01-02 12:00:00', timestamp '2025-01-02');
                    create table products(product_id varchar, product_name varchar, category varchar, _ingested_at_utc timestamp);
                    insert into products values
                      ('p1', 'Widget', 'Accessories', timestamp '2025-01-02'),
                      ('p2', 'Gadget', 'Electronics', timestamp '2025-01-02');
                    create table order_items(order_item_id varchar, order_id varchar, product_id varchar,
                        quantity integer, unit_price decimal(10,2), _ingested_at_utc timestamp);
                    insert into order_items values
                      ('i1', 'o100', 'p1', 2, 10.00, timestamp '2025-01-02'),
                      ('i2', 'o100', 'p1', 1, 5.00, timestamp '2025-01-02'),
                      ('i3', 'o101', 'p1', 1, 7.00, timestamp '2025-01-02'),
                      ('i4', 'o102', 'p2', 1, 12.00, timestamp '2025-01-02'),
                      ('i5', 'o103', 'p1', 1, 50.00, timestamp '2025-01-02'),
                      ('i6', 'o104', 'p1', 1, 9.00, timestamp '2025-01-02');
                """)

                models = edition / "models" / "migration"
                for name in ("orders", "order_items", "products"):
                    model_path = models / "silver" / f"silver_{name}.sql"
                    connection.execute(
                        f"create table silver_{name} as "
                        + _render(model_path.read_text(encoding="utf-8"))
                    )
                gold = _render((models / "gold" / "mart_migration_daily_product_sales.sql").read_text(encoding="utf-8"))
                result = connection.execute(gold).fetchall()
                output_keys = [(row[0].isoformat(), row[1]) for row in result]
                self.assertEqual(len(output_keys), len(set(output_keys)), "daily sales Gold keys must be unique")
                actual = {
                    (row[0].isoformat(), row[1]): (row[4], row[5], float(row[6]), float(row[7]))
                    for row in result
                }
                expected = {
                    ("2025-01-01", "p1"): (1, 3, 25.0, 7.0),
                    ("2025-01-01", "p2"): (1, 1, 12.0, 0.0),
                    ("2025-01-02", "p1"): (1, 1, 9.0, 0.0),
                }
                self.assertEqual(actual, expected)

    def test_late_order_updates_blank_keys_and_customer_value_gold_in_both_editions(self):
        for edition in EDITIONS:
            with self.subTest(edition=str(edition)):
                connection = duckdb.connect(database=":memory:")
                self.addCleanup(connection.close)
                connection.execute("""
                    create table customers(
                        customer_id varchar, created_at timestamp, country varchar, _ingested_at_utc timestamp
                    );
                    insert into customers values
                      ('c1', timestamp '2025-01-01', 'Old Country', timestamp '2025-01-01'),
                      ('c1', timestamp '2025-01-01', 'AU', timestamp '2025-01-02'),
                      ('c2', timestamp '2025-01-02', 'NZ', timestamp '2025-01-02'),
                      ('c3', timestamp '2025-01-03', 'CA', timestamp '2025-01-03'),
                      ('', timestamp '2025-01-01', 'Invalid', timestamp '2025-01-01'),
                      ('   ', timestamp '2025-01-01', 'Invalid', timestamp '2025-01-01'),
                      (null, timestamp '2025-01-01', 'Invalid', timestamp '2025-01-01');

                    create table orders(
                        order_id varchar, customer_id varchar, order_status varchar,
                        order_date date, updated_at timestamp, _ingested_at_utc timestamp
                    );
                    insert into orders values
                      ('o1', 'c1', 'complete', date '2025-01-01', timestamp '2025-01-01', timestamp '2025-01-05'),
                      ('o1', 'c1', 'returned', date '2025-01-01', timestamp '2025-01-03', timestamp '2025-01-04'),
                      ('o2', 'c1', 'pending', date '2025-01-02', timestamp '2025-01-02', timestamp '2025-01-05'),
                      ('o2', 'c1', 'complete', date '2025-01-04', timestamp '2025-01-04', timestamp '2025-01-04'),
                      ('o3', 'c1', 'completed', date '2025-01-02', timestamp '2025-01-02', timestamp '2025-01-02'),
                      ('o4', 'c2', 'cancelled', date '2025-01-03', timestamp '2025-01-03', timestamp '2025-01-03'),
                      ('', 'c1', 'complete', date '2025-01-05', timestamp '2025-01-05', timestamp '2025-01-05'),
                      ('   ', 'c1', 'complete', date '2025-01-05', timestamp '2025-01-05', timestamp '2025-01-05'),
                      (null, 'c1', 'complete', date '2025-01-05', timestamp '2025-01-05', timestamp '2025-01-05');

                    create table order_items(
                        order_item_id varchar, order_id varchar, product_id varchar,
                        quantity integer, unit_price decimal(10,2), _ingested_at_utc timestamp
                    );
                    insert into order_items values
                      ('i1', 'o1', 'p1', 1, 999.00, timestamp '2025-01-02'),
                      ('i2', 'o2', 'p1', 2, 5.00, timestamp '2025-01-02'),
                      ('i2', 'o2', 'p1', 2, 6.00, timestamp '2025-01-04'),
                      ('i3', 'o3', 'p1', 1, 3.00, timestamp '2025-01-02'),
                      ('i4', 'o4', 'p1', 1, 50.00, timestamp '2025-01-03'),
                      ('', 'o2', 'p1', 100, 100.00, timestamp '2025-01-05'),
                      ('   ', 'o2', 'p1', 100, 100.00, timestamp '2025-01-05'),
                      (null, 'o2', 'p1', 100, 100.00, timestamp '2025-01-05'),
                      ('i5', '   ', 'p1', 100, 100.00, timestamp '2025-01-05');
                """)

                self._build_silver_models(connection, edition)
                latest_orders = connection.execute(
                    "select order_id, order_status, order_date from silver_orders order by order_id"
                ).fetchall()
                self.assertEqual(latest_orders, [
                    ("o1", "returned", date(2025, 1, 1)),
                    ("o2", "complete", date(2025, 1, 4)),
                    ("o3", "completed", date(2025, 1, 2)),
                    ("o4", "cancelled", date(2025, 1, 3)),
                ])
                self.assertEqual(connection.execute(
                    "select customer_id from silver_customers order by customer_id"
                ).fetchall(), [("c1",), ("c2",), ("c3",)])
                self.assertEqual(connection.execute(
                    "select order_item_id, order_id, unit_price from silver_order_items order by order_item_id"
                ).fetchall(), [("i1", "o1", 999), ("i2", "o2", 6), ("i3", "o3", 3), ("i4", "o4", 50), ("i5", None, 100)])

                gold_path = edition / "models" / "migration" / "gold" / "mart_migration_customer_value.sql"
                gold = _render(gold_path.read_text(encoding="utf-8"))
                actual = connection.execute(gold).fetchall()
                normalized = sorted(
                    (
                        row[0], row[1], row[2], Decimal(str(row[3])), row[4]
                    )
                    for row in actual
                )
                self.assertEqual(normalized, [
                    ("c1", "AU", 2, Decimal("15"), date(2025, 1, 4)),
                    ("c2", "NZ", 0, Decimal("0"), None),
                    ("c3", "CA", 0, Decimal("0"), None),
                ])
