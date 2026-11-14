"""Offline smoke of the documented local migration dry-run, confirmed load, and reconciliation commands."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.tests._dlt_isolated_runtime import isolated_environment, prepare_runtime, run_script


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_ROOT = ROOT / "migration"
try:
    import dlt  # noqa: F401
    import duckdb  # noqa: F401
except ModuleNotFoundError as error:
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name not in {"dlt", "sqlalchemy", "duckdb"}:
        raise
    dlt = None


@unittest.skipIf(dlt is None, "migration runtime dependencies are not installed")
class CleanCheckoutMigrationSmokeTests(unittest.TestCase):
    def test_documented_csv_and_sqlite_dry_runs_use_only_isolated_local_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="migration-clean-checkout-smoke-") as temporary:
            root = Path(temporary)
            runtime = prepare_runtime(root)
            env = isolated_environment(runtime, root)

            examples = runtime / "examples"
            shutil.copytree(MIGRATION_ROOT / "examples" / "csv", examples / "csv")
            csv_result = run_script(runtime, "run_migration.py", [
                "--source", "csv", "--destination", "duckdb", "--dataset", "starterkit_migration_bronze",
                "--csv-dir", "examples/csv", "--dry-run",
            ], env)
            self.assertEqual(csv_result.returncode, 0, csv_result.stdout + csv_result.stderr)
            csv_report = json.loads(csv_result.stdout)
            self.assertEqual(csv_report["source"], "csv")
            self.assertEqual(csv_report["destination"], "duckdb")
            self.assertFalse(csv_report["writes_performed"])

            source_path = root / "starterkit-retail.sqlite"
            demo = subprocess.run(
                [sys.executable, str(ROOT / "migration" / "create_sqlite_demo.py"), str(source_path)],
                cwd=runtime,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(demo.returncode, 0, demo.stdout + demo.stderr)
            self.assertTrue(source_path.is_file())
            env["MIGRATION_SOURCE_URL"] = f"sqlite:///{source_path}"
            sqlite_result = run_script(runtime, "run_migration.py", [
                "--source", "sql", "--destination", "duckdb", "--dataset", "starterkit_migration_bronze",
                "--source-schema", "main", "--tables", "customers,products,orders,order_items", "--dry-run",
            ], env)
            self.assertEqual(sqlite_result.returncode, 0, sqlite_result.stdout + sqlite_result.stderr)
            sqlite_report = json.loads(sqlite_result.stdout)
            self.assertEqual(sqlite_report["source"], "sql")
            self.assertEqual(sqlite_report["destination"], "duckdb")
            self.assertFalse(sqlite_report["writes_performed"])
            self.assertNotIn(str(source_path), sqlite_result.stdout + sqlite_result.stderr)
            self.assertFalse((runtime / "run-manifests").exists())
            self.assertFalse(list((runtime / ".dlt" / "pipelines").rglob("*.duckdb")))

            load_result = run_script(runtime, "run_migration.py", [
                "--source", "sql", "--destination", "duckdb", "--dataset", "starterkit_migration_bronze",
                "--source-schema", "main", "--tables", "customers,products,orders,order_items",
                "--confirm-target", sqlite_report["target_identity"],
                "--confirm-tables", sqlite_report["table_confirmation"],
                "--confirm-schema-fingerprint", sqlite_report["schema_confirmation_fingerprint"],
            ], env)
            self.assertEqual(load_result.returncode, 0, load_result.stdout + load_result.stderr)
            manifest_paths = list((runtime / "run-manifests").glob("*.json"))
            self.assertEqual(len(manifest_paths), 1)
            manifest = json.loads(manifest_paths[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["destination_tables"], ["customers", "order_items", "orders", "products"])

            reconcile_result = run_script(runtime, "reconcile_bronze.py", ["--run-id", manifest["run_id"]], env)
            self.assertEqual(reconcile_result.returncode, 0, reconcile_result.stdout + reconcile_result.stderr)
            report_path = runtime / "run-manifests" / f"{manifest['run_id']}.bronze-reconciliation.offline.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["report_type"], "count_only_diagnostic")
            self.assertEqual(report["reconciliation_status"], "count_unavailable")
            self.assertFalse(report["destination_count_query_performed"])

            target_tail = sqlite_report["target_identity"].removeprefix("duckdb/")
            database_path, separator, _namespace = target_tail.rpartition("/")
            self.assertTrue(separator)
            self.assertTrue(Path(database_path).is_file())


if __name__ == "__main__":
    unittest.main()
