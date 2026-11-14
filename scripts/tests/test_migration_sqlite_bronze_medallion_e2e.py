"""Feed loader-produced DuckDB Bronze rows through both editions' Silver/Gold SQL."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from scripts.tests._dlt_isolated_runtime import isolated_environment, prepare_runtime, run_script


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_ROOT = ROOT / "migration"
EDITIONS = (ROOT / "editions" / "Bigquery", ROOT / "editions" / "Snowflake" / "kit")
try:
    import dlt  # noqa: F401
    import duckdb
except ModuleNotFoundError as error:
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name not in {"dlt", "sqlalchemy", "duckdb"}:
        raise
    duckdb = None


def _render(sql: str, namespace: str) -> str:
    sql = re.sub(r"\{\{\s*dbt\.type_string\(\)\s*\}\}", "varchar", sql)
    sql = re.sub(r"\{\{\s*dbt\.type_timestamp\(\)\s*\}\}", "timestamp", sql)
    sql = re.sub(r"\{\{\s*dbt\.type_numeric\(\)\s*\}\}", "decimal(38, 9)", sql)
    sql = re.sub(
        r"\{\{\s*source\('migration_bronze',\s*'([^']+)'\)\s*\}\}",
        lambda match: f'"{namespace}"."{match.group(1)}"',
        sql,
    )
    sql = re.sub(r"\{\{\s*ref\('([^']+)'\)\s*\}\}", r'"\1"', sql)
    if "{{" in sql or "}}" in sql:
        raise AssertionError("Unrendered dbt expression in migration model")
    return sql


@unittest.skipIf(duckdb is None, "dlt, SQLAlchemy, and DuckDB are required for offline migration E2E")
class SQLiteBronzeMedallionE2ETests(unittest.TestCase):
    def test_loader_output_feeds_both_editions_silver_and_gold(self) -> None:
        previous_umask = os.umask(0o077)
        try:
            with tempfile.TemporaryDirectory(prefix="migration-bronze-medallion-") as temporary:
                root = Path(temporary)
                runtime = prepare_runtime(root)
                env = isolated_environment(runtime, root)
                source_path = root / "source.sqlite"
                with sqlite3.connect(source_path) as source:
                    fixture = MIGRATION_ROOT / "examples" / "sqlite_source.sql"
                    source.executescript(fixture.read_text(encoding="utf-8"))
                source_path.chmod(0o400)
                env["MIGRATION_SOURCE_URL"] = f"sqlite:///{source_path}"

                base_args = [
                    "--source", "sql", "--destination", "duckdb",
                    "--dataset", "starterkit_migration_medallion_e2e",
                    "--pipeline-name", "medallion_e2e", "--source-schema", "main",
                    "--tables", "customers,products,orders,order_items",
                ]
                dry_result = run_script(runtime, "run_migration.py", [*base_args, "--dry-run"], env)
                self.assertEqual(dry_result.returncode, 0, dry_result.stdout + dry_result.stderr)
                dry = json.loads(dry_result.stdout)
                load_result = run_script(runtime, "run_migration.py", [
                    *base_args,
                    "--confirm-target", dry["target_identity"],
                    "--confirm-tables", dry["table_confirmation"],
                    "--confirm-schema-fingerprint", dry["schema_confirmation_fingerprint"],
                ], env)
                self.assertEqual(load_result.returncode, 0, load_result.stdout + load_result.stderr)

                manifests = list((runtime / "run-manifests").glob("*.json"))
                self.assertEqual(len(manifests), 1)
                manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
                self.assertEqual(manifest["status"], "completed")
                self.assertEqual(manifest["destination_write_may_be_partial"], False)
                expected_bronze_counts = {"customers": 3, "products": 3, "orders": 3, "order_items": 4}
                self.assertEqual(
                    {name: manifest["source_row_counts"][name]["rows"] for name in expected_bronze_counts},
                    expected_bronze_counts,
                )
                target_tail = manifest["target_identity"].removeprefix("duckdb/")
                database, separator, namespace = target_tail.rpartition("/")
                self.assertTrue(separator)
                self.assertTrue(Path(database).resolve().is_relative_to((runtime / ".dlt" / "pipelines").resolve()))

                with duckdb.connect(database) as connection:
                    self.assertEqual(
                        {name: connection.execute(f'SELECT count(*) FROM "{namespace}"."{name}"').fetchone()[0]
                         for name in expected_bronze_counts},
                        expected_bronze_counts,
                    )
                    for edition in EDITIONS:
                        models = edition / "models" / "migration"
                        for table in ("customers", "products", "orders", "order_items"):
                            sql = _render(
                                (models / "silver" / f"silver_{table}.sql").read_text(encoding="utf-8"),
                                namespace,
                            )
                            connection.execute(f'CREATE TEMP TABLE "silver_{table}" AS {sql}')

                        daily_sql = _render(
                            (models / "gold" / "mart_migration_daily_product_sales.sql").read_text(encoding="utf-8"),
                            namespace,
                        )
                        daily = connection.execute(daily_sql).fetchall()
                        daily_actual = {
                            (row[0], row[1]): (row[4], row[5], Decimal(str(row[6])), Decimal(str(row[7])))
                            for row in daily
                        }
                        self.assertEqual(daily_actual, {
                            (date(2025, 2, 1), "P001"): (1, 2, Decimal("37.000000000"), Decimal("0E-9")),
                            (date(2025, 2, 1), "P003"): (1, 1, Decimal("6.000000000"), Decimal("0E-9")),
                            (date(2025, 2, 2), "P002"): (0, 0, Decimal("0E-9"), Decimal("24.000000000")),
                            (date(2025, 2, 3), "P003"): (1, 3, Decimal("18.000000000"), Decimal("0E-9")),
                        })
                        self.assertEqual(len(daily), len(daily_actual), "daily product sales grain must be unique")

                        customer_sql = _render(
                            (models / "gold" / "mart_migration_customer_value.sql").read_text(encoding="utf-8"),
                            namespace,
                        )
                        customers = connection.execute(customer_sql).fetchall()
                        normalized = sorted(
                            (row[0], row[1], row[2], Decimal(str(row[3])), row[4]) for row in customers
                        )
                        self.assertEqual(normalized, [
                            ("C001", "AU", 2, Decimal("61.000000000"), date(2025, 2, 3)),
                            ("C002", "NZ", 0, Decimal("0E-9"), None),
                            ("C003", "AU", 0, Decimal("0E-9"), None),
                        ])
                        connection.execute("DROP TABLE silver_customers")
                        connection.execute("DROP TABLE silver_products")
                        connection.execute("DROP TABLE silver_orders")
                        connection.execute("DROP TABLE silver_order_items")
                self.assertEqual(source_path.stat().st_mode & 0o777, 0o400)
        finally:
            os.umask(previous_umask)


if __name__ == "__main__":
    unittest.main()
