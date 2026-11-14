"""Verify explicit SQL mapping casts survive dlt's SQLAlchemy column hints in local DuckDB."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.tests._dlt_isolated_runtime import isolated_environment, prepare_runtime, run_script


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "migration"))
try:
    from migration import run_migration as migration
    import duckdb
    from sqlalchemy import MetaData, Table, create_engine
except ModuleNotFoundError as error:
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name not in {"dlt", "sqlalchemy", "duckdb"}:
        raise
    migration = None


@unittest.skipIf(migration is None, "migration runtime dependencies are not installed")
class SQLMappingCastDestinationTests(unittest.TestCase):
    def test_explicit_date_timestamp_and_integer_casts_survive_to_duckdb(self) -> None:
        with tempfile.TemporaryDirectory(prefix="migration-sql-casts-") as temporary:
            root = Path(temporary)
            source_path = root / "source.sqlite"
            with sqlite3.connect(source_path) as source:
                source.executescript("""
                    CREATE TABLE orders (
                        order_id VARCHAR, customer_id VARCHAR, order_status VARCHAR,
                        "Order Date" VARCHAR, "Updated At" VARCHAR
                    );
                    INSERT INTO orders VALUES
                        ('o1', 'c1', 'complete', '2025-03-04', '2025-03-04T10:30:00Z');
                    CREATE TABLE order_items (
                        order_item_id VARCHAR, order_id VARCHAR, product_id VARCHAR, "Qty" VARCHAR
                    );
                    INSERT INTO order_items VALUES ('i1', 'o1', 'p1', '7');
                """)

            source_url = f"sqlite:///{source_path}"
            source_engine = migration.create_read_only_sql_engine(source_url)
            try:
                with source_engine.connect() as connection:
                    orders = Table("orders", MetaData(), schema="main", autoload_with=connection)
                    items = Table("order_items", MetaData(), schema="main", autoload_with=connection)
                self.assertEqual(str(orders.c["Order Date"].type).upper(), "VARCHAR")
                self.assertEqual(str(orders.c["Updated At"].type).upper(), "VARCHAR")
                self.assertEqual(str(items.c["Qty"].type).upper(), "VARCHAR")
            finally:
                source_engine.dispose()

            mapping_path = root / "mapping.json"
            mapping_path.write_text(json.dumps({
                "version": 1,
                "tables": {
                    "orders": [
                        {"source": "Order Date", "target": "order_date", "cast": "date"},
                        {"source": "Updated At", "target": "updated_at", "cast": "timestamp"},
                    ],
                    "order_items": [
                        {"source": "Qty", "target": "quantity", "cast": "integer"},
                    ],
                },
            }), encoding="utf-8")

            dataset = "starterkit_migration_sql_casts"
            pipeline_prefix = "cast_fixture"
            base_args = [
                "run_migration.py", "--source", "sql", "--destination", "duckdb",
                "--dataset", dataset, "--pipeline-name", pipeline_prefix,
                "--source-schema", "main", "--tables", "orders,order_items",
                "--mapping-file", str(mapping_path),
            ]
            previous_umask = os.umask(0o077)
            runtime = prepare_runtime(root)
            env = isolated_environment(runtime, root)
            env["MIGRATION_SOURCE_URL"] = source_url
            try:
                dry_result = run_script(runtime, "run_migration.py", [*base_args, "--dry-run"], env)
                dry_output = dry_result.stdout + dry_result.stderr
                self.assertEqual(dry_result.returncode, 0, dry_output)
                dry_report = json.loads(dry_output)

                load_result = run_script(runtime, "run_migration.py", [
                    *base_args,
                    "--confirm-target", dry_report["target_identity"],
                    "--confirm-tables", dry_report["table_confirmation"],
                    "--confirm-schema-fingerprint", dry_report["schema_confirmation_fingerprint"],
                ], env)
                load_output = load_result.stdout + load_result.stderr
                self.assertEqual(load_result.returncode, 0, load_output)

                manifests = list((runtime / "run-manifests").glob("*.json"))
                self.assertEqual(len(manifests), 1)
                manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
                self.assertEqual(manifest["status"], "completed")
                target_tail = manifest["target_identity"].removeprefix("duckdb/")
                database, separator, namespace = target_tail.rpartition("/")
                self.assertTrue(separator)
                with duckdb.connect(database, read_only=True) as destination:
                    order_date, order_date_type, updated_at, updated_at_type = destination.execute(
                        f'SELECT order_date, typeof(order_date), updated_at, typeof(updated_at) '
                        f'FROM "{namespace}"."orders"'
                    ).fetchone()
                    quantity, quantity_type = destination.execute(
                        f'SELECT quantity, typeof(quantity) FROM "{namespace}"."order_items"'
                    ).fetchone()

                self.assertEqual(order_date_type, "DATE")
                self.assertEqual(order_date.isoformat(), "2025-03-04")
                self.assertIn(updated_at_type, {"TIMESTAMP WITH TIME ZONE", "TIMESTAMP"})
                expected_utc = datetime(2025, 3, 4, 10, 30, tzinfo=timezone.utc)
                if updated_at.tzinfo is None:
                    self.assertEqual(updated_at, expected_utc.replace(tzinfo=None))
                else:
                    self.assertEqual(updated_at.astimezone(timezone.utc), expected_utc)
                self.assertEqual(quantity_type, "BIGINT")
                self.assertEqual(quantity, 7)
            finally:
                os.umask(previous_umask)


if __name__ == "__main__":
    unittest.main()
