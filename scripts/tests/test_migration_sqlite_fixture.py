"""Keep the bundled SQLite demo's date and timestamp values within the Silver contract."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SQLITE_FIXTURE = REPOSITORY_ROOT / "migration" / "examples" / "sqlite_source.sql"
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "migration"))
try:
    from migration import run_migration
except ModuleNotFoundError as error:
    if error.name not in {"dlt", "sqlalchemy"} or __import__("os").environ.get("CI", "").casefold() in {"1", "true", "yes"}:
        raise
    run_migration = None


class BundledSQLiteFixtureTests(unittest.TestCase):
    def test_order_dates_are_iso_dates_and_updated_at_values_are_timestamps(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript(SQLITE_FIXTURE.read_text(encoding="utf-8"))

        rows = connection.execute("SELECT order_date, updated_at FROM orders ORDER BY order_id").fetchall()

        self.assertTrue(rows)
        for order_date, updated_at in rows:
            with self.subTest(order_date=order_date, updated_at=updated_at):
                self.assertRegex(order_date, r"^\d{4}-\d{2}-\d{2}$")
                self.assertEqual(date.fromisoformat(order_date).isoformat(), order_date)
                self.assertRegex(updated_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
                self.assertEqual(datetime.fromisoformat(updated_at.replace("Z", "+00:00")).utcoffset().total_seconds(), 0)

    @unittest.skipIf(run_migration is None, "migration runtime dependencies are not installed")
    def test_reflected_sqlite_fixture_matches_retail_contract_types(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "retail.sqlite"
            with sqlite3.connect(database) as connection:
                connection.executescript(SQLITE_FIXTURE.read_text(encoding="utf-8"))

            engine = run_migration.create_read_only_sql_engine(f"sqlite:///{database}")
            self.addCleanup(engine.dispose)
            tables = ["customers", "products", "orders", "order_items"]
            inventory = run_migration.inventory_sql_inputs(engine, "main", tables)
            client = SimpleNamespace(
                schema=SimpleNamespace(naming=run_migration.SnakeCaseNamingConvention()),
                capabilities=SimpleNamespace(casefold_identifier=str.casefold),
            )
            report = run_migration.retail_contract_report(inventory, "sql", client)

            self.assertEqual(report["incompatible_columns"], [])
            self.assertEqual(report["status"], "schema_compatible_data_tests_still_required")
            with engine.connect() as connection:
                metadata = run_migration.MetaData()
                customers = run_migration.Table("customers", metadata, schema="main", autoload_with=connection)
                orders = run_migration.Table("orders", metadata, schema="main", autoload_with=connection)
                created_at = connection.execute(run_migration.select(customers.c.created_at).limit(1)).scalar_one()
                order_date, updated_at = connection.execute(
                    run_migration.select(orders.c.order_date, orders.c.updated_at).limit(1)
                ).one()
            self.assertIsInstance(created_at, datetime)
            self.assertIsInstance(order_date, date)
            self.assertNotIsInstance(order_date, datetime)
            self.assertIsInstance(updated_at, datetime)


if __name__ == "__main__":
    unittest.main()
