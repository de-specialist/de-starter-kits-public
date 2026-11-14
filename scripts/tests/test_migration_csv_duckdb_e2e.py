"""End-to-end local CSV-to-DuckDB migration and reconciliation regression."""

from __future__ import annotations

import json
import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.tests._dlt_isolated_runtime import isolated_environment, prepare_runtime, run_script


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_ROOT = ROOT / "migration"
try:
    if importlib.util.find_spec("dlt") is None:
        raise ModuleNotFoundError("migration runtime package dlt is unavailable", name="dlt")
    import duckdb
except ModuleNotFoundError as error:
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name not in {"dlt", "sqlalchemy", "duckdb"}:
        raise
    duckdb = None


@unittest.skipIf(duckdb is None, "migration runtime dependencies are not installed")
class CsvDuckDBMigrationE2ETests(unittest.TestCase):
    def _invoke(self, runtime: Path, script: str, argv: list[str], env: dict[str, str]) -> tuple[int, str]:
        result = run_script(runtime, script, argv, env)
        return result.returncode, result.stdout + result.stderr

    def test_default_prefix_load_reconciliation_and_stale_header_guard(self) -> None:
        expected_counts = {"customers": 3, "order_items": 4, "orders": 3, "products": 3}
        previous_umask = os.umask(0o077)
        try:
            with tempfile.TemporaryDirectory(prefix="migration-csv-duckdb-e2e-") as temporary:
                root = Path(temporary)
                runtime = prepare_runtime(root)
                # Deliberately provide hostile ambient dlt settings. The test helper
                # must replace project/home roots and strip destination overrides.
                hostile_project = root / "hostile-project"
                hostile_home = root / "hostile-home"
                (hostile_project / ".dlt").mkdir(parents=True)
                (hostile_home / ".dlt").mkdir(parents=True)
                (hostile_project / ".dlt" / "config.toml").write_text(
                    '[destination.duckdb.credentials]\ndatabase = "redirected-from-project.duckdb"\n', encoding="utf-8",
                )
                (hostile_home / ".dlt" / "config.toml").write_text(
                    '[destination.duckdb.credentials]\ndatabase = "redirected-from-home.duckdb"\n', encoding="utf-8",
                )
                hostile_env = dict(os.environ)
                hostile_env["DLT_PROJECT_DIR"] = str(hostile_project)
                hostile_env["HOME"] = str(hostile_home)
                hostile_env["DESTINATION__DUCKDB__CREDENTIALS__DATABASE"] = str(root / "redirected-by-env.duckdb")
                hostile_env["BQ_PROJECT"] = "ambient-google-project"
                hostile_env["SNOWFLAKE_ACCOUNT"] = "ambient-snowflake-account"
                hostile_env["GOOGLE_APPLICATION_CREDENTIALS"] = str(root / "ambient-google-credentials.json")
                hostile_env["MIGRATION_SOURCE_URL"] = "postgresql+psycopg://ambient.invalid/secret"
                env = isolated_environment(runtime, root, base=hostile_env)
                self.assertEqual(Path(env["DLT_PROJECT_DIR"]), runtime)
                self.assertEqual(Path(env["HOME"]), root / "isolated-home")
                self.assertNotIn("DESTINATION__DUCKDB__CREDENTIALS__DATABASE", env)
                for variable in (
                    "BQ_PROJECT", "SNOWFLAKE_ACCOUNT", "GOOGLE_APPLICATION_CREDENTIALS", "MIGRATION_SOURCE_URL",
                ):
                    self.assertNotIn(variable, env)
                csv_dir = root / "csv"
                csv_dir.mkdir()
                for source_file in (MIGRATION_ROOT / "examples" / "csv").glob("*.csv"):
                    shutil.copyfile(source_file, csv_dir / source_file.name)

                base_args = [
                    "run_migration.py", "--source", "csv", "--destination", "duckdb",
                    "--dataset", "starterkit_migration_bronze", "--csv-dir", str(csv_dir),
                ]
                dry_status, dry_output = self._invoke(runtime, "run_migration.py", [*base_args, "--dry-run"], env)
                self.assertEqual(dry_status, 0, dry_output)
                dry = json.loads(dry_output)
                self.assertEqual(dry["destination_tables"], list(expected_counts))
                self.assertEqual(dry["table_confirmation"], ",".join(expected_counts))
                self.assertEqual(len(dry["schema_confirmation_fingerprint"]), 64)
                target_tail = dry["target_identity"].removeprefix("duckdb/")
                database, separator, _namespace = target_tail.rpartition("/")
                self.assertTrue(separator)
                self.assertTrue(Path(database).resolve().is_relative_to((runtime / ".dlt" / "pipelines").resolve()))

                load_status, load_output = self._invoke(runtime, "run_migration.py", [
                    *base_args,
                    "--confirm-target", dry["target_identity"],
                    "--confirm-tables", dry["table_confirmation"],
                    "--confirm-schema-fingerprint", dry["schema_confirmation_fingerprint"],
                ], env)
                self.assertEqual(load_status, 0, load_output)
                manifest_files = list((runtime / "run-manifests").glob("*.json"))
                self.assertEqual(len(manifest_files), 1)
                manifest_path = manifest_files[0]
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(manifest["status"], "completed")
                self.assertEqual(manifest["pipeline_name"].split("_duckdb_")[0], "starterkit_migration")
                for table, expected in expected_counts.items():
                    self.assertEqual(manifest["source_row_counts"][table]["rows"], expected)
                    self.assertEqual(manifest["dlt_prepared_row_counts"][table]["rows"], expected)

                run_id = manifest["run_id"]
                offline_status, offline_output = self._invoke(
                    runtime, "reconcile_bronze.py", ["--run-id", run_id], env,
                )
                self.assertEqual(offline_status, 0, offline_output)
                offline_path = runtime / "run-manifests" / f"{run_id}.bronze-reconciliation.offline.json"
                offline_report = json.loads(offline_path.read_text(encoding="utf-8"))
                self.assertFalse(offline_report["destination_count_query_performed"])

                queried_status, queried_output = self._invoke(runtime, "reconcile_bronze.py", [
                    "--run-id", run_id, "--query-destination",
                    "--confirm-target", manifest["target_identity"],
                    "--confirm-tables", ",".join(manifest["destination_tables"]),
                ], env)
                self.assertEqual(queried_status, 0, queried_output)
                queried_path = runtime / "run-manifests" / f"{run_id}.bronze-reconciliation.queried.json"
                self.assertNotEqual(offline_path, queried_path)
                queried_report = json.loads(queried_path.read_text(encoding="utf-8"))
                self.assertTrue(queried_report["destination_count_query_performed"])
                self.assertEqual(queried_report["reconciliation_status"], "count_match")
                queried_counts = {
                    item["bronze_table"]: item["current_destination_rows"]
                    for item in queried_report["counts"]
                }
                self.assertEqual(queried_counts, expected_counts)

                target_tail = manifest["target_identity"].removeprefix("duckdb/")
                database, separator, namespace = target_tail.rpartition("/")
                self.assertTrue(separator)
                orders_path = csv_dir / "orders.csv"
                original_orders = orders_path.read_text(encoding="utf-8")
                orders_path.write_text(original_orders.replace("order_date", "order_date_changed", 1), encoding="utf-8")
                changed_status, changed_output = self._invoke(runtime, "run_migration.py", [*base_args, "--dry-run"], env)
                self.assertEqual(changed_status, 0, changed_output)
                changed_fingerprint = json.loads(changed_output)["schema_confirmation_fingerprint"]
                self.assertNotEqual(changed_fingerprint, dry["schema_confirmation_fingerprint"])

                before_stale = {path.name for path in (runtime / "run-manifests").iterdir()}
                stale_status, stale_output = self._invoke(runtime, "run_migration.py", [
                    *base_args,
                    "--confirm-target", manifest["target_identity"],
                    "--confirm-tables", ",".join(manifest["destination_tables"]),
                    "--confirm-schema-fingerprint", dry["schema_confirmation_fingerprint"],
                ], env)
                self.assertEqual(stale_status, 1)
                self.assertIn("differs from the reviewed fingerprint", stale_output)
                self.assertEqual(
                    {path.name for path in (runtime / "run-manifests").iterdir()},
                    before_stale,
                    "stale schema confirmation must stop before creating a manifest",
                )

                with duckdb.connect(database, read_only=True) as destination:
                    current_counts = {
                        table: destination.execute(
                            f'SELECT count(*) FROM "{namespace}"."{table}"'
                        ).fetchone()[0]
                        for table in expected_counts
                    }
                self.assertEqual(current_counts, expected_counts)
        finally:
            os.umask(previous_umask)


if __name__ == "__main__":
    unittest.main()
