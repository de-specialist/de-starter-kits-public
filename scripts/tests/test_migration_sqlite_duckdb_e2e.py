"""Hermetic SQLite-to-DuckDB migration, reconciliation, and schema-drift regression."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.tests._dlt_isolated_runtime import isolated_environment, prepare_runtime, run_script


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_ROOT = ROOT / "migration"
try:
    import dlt  # noqa: F401
    import duckdb
except ModuleNotFoundError as error:
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name not in {"dlt", "sqlalchemy", "duckdb"}:
        raise
    dlt = None
    duckdb = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@unittest.skipIf(duckdb is None or dlt is None, "migration runtime dependencies are not installed")
class SQLiteDuckDBMigrationE2ETests(unittest.TestCase):
    def _invoke(self, runtime: Path, env: dict[str, str], script: str, *args: str):
        result = run_script(runtime, script, list(args), env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def _destination_rows(self, connection, namespace: str, tables: dict[str, int]) -> dict[str, list[tuple]]:
        return {
            table: connection.execute(
                f'SELECT * FROM "{namespace}"."{table}" ORDER BY ALL'
            ).fetchall()
            for table in tables
        }

    def test_confirmed_sqlite_load_reconciliation_and_stale_schema_refusal(self) -> None:
        expected_counts = {"customers": 3, "order_items": 4, "orders": 3, "products": 3}
        previous_umask = os.umask(0o077)
        try:
            with tempfile.TemporaryDirectory(prefix="migration-sqlite-duckdb-e2e-") as temporary:
                root = Path(temporary)
                runtime = prepare_runtime(root)
                env = isolated_environment(runtime, root)
                fixture_path = root / "sqlite_source.sql"
                shutil.copyfile(MIGRATION_ROOT / "examples" / "sqlite_source.sql", fixture_path)
                source_path = root / "source.sqlite"
                source = sqlite3.connect(source_path)
                try:
                    source.executescript(fixture_path.read_text(encoding="utf-8"))
                finally:
                    source.close()
                source_path.chmod(0o400)
                source_url = f"sqlite:///{source_path}"
                env["MIGRATION_SOURCE_URL"] = source_url

                base_args = [
                    "--source", "sql", "--destination", "duckdb",
                    "--dataset", "starterkit_migration_sqlite_e2e",
                    "--pipeline-name", "sqlite_e2e",
                    "--source-schema", "main",
                    "--tables", "customers,products,orders,order_items",
                ]
                source_before = (_sha256(source_path), source_path.stat().st_mtime_ns)
                dry = json.loads(self._invoke(runtime, env, "run_migration.py", *base_args, "--dry-run"))
                self.assertEqual(dry["destination_tables"], list(expected_counts))
                self.assertEqual(dry["table_confirmation"], ",".join(expected_counts))
                self.assertEqual(len(dry["schema_confirmation_fingerprint"]), 64)
                target_tail = dry["target_identity"].removeprefix("duckdb/")
                database, separator, namespace = target_tail.rpartition("/")
                self.assertTrue(separator)
                self.assertTrue(Path(database).resolve().is_relative_to((runtime / ".dlt" / "pipelines").resolve()))

                self._invoke(
                    runtime,
                    env,
                    "run_migration.py",
                    *base_args,
                    "--confirm-target", dry["target_identity"],
                    "--confirm-tables", dry["table_confirmation"],
                    "--confirm-schema-fingerprint", dry["schema_confirmation_fingerprint"],
                )
                self.assertEqual((_sha256(source_path), source_path.stat().st_mtime_ns), source_before)
                self.assertFalse(Path(f"{source_path}-wal").exists())
                self.assertFalse(Path(f"{source_path}-shm").exists())
                self.assertFalse(Path(f"{source_path}-journal").exists())

                manifests = list((runtime / "run-manifests").glob("*.json"))
                self.assertEqual(len(manifests), 1)
                manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
                self.assertEqual(manifest["status"], "completed")
                self.assertEqual(manifest["destination_tables"], list(expected_counts))
                for table, count in expected_counts.items():
                    self.assertEqual(manifest["source_row_counts"][table]["rows"], count)
                    self.assertEqual(manifest["dlt_prepared_row_counts"][table]["rows"], count)
                with duckdb.connect(database, read_only=True) as destination:
                    actual_counts = {
                        table: destination.execute(f'SELECT count(*) FROM "{namespace}"."{table}"').fetchone()[0]
                        for table in expected_counts
                    }
                self.assertEqual(actual_counts, expected_counts)

                run_id = manifest["run_id"]
                self._invoke(runtime, env, "reconcile_bronze.py", "--run-id", run_id)
                offline_path = runtime / "run-manifests" / f"{run_id}.bronze-reconciliation.offline.json"
                offline = json.loads(offline_path.read_text(encoding="utf-8"))
                self.assertFalse(offline["destination_count_query_performed"])

                self._invoke(
                    runtime,
                    env,
                    "reconcile_bronze.py",
                    "--run-id", run_id,
                    "--query-destination",
                    "--confirm-target", manifest["target_identity"],
                    "--confirm-tables", ",".join(manifest["destination_tables"]),
                )
                queried_path = runtime / "run-manifests" / f"{run_id}.bronze-reconciliation.queried.json"
                queried = json.loads(queried_path.read_text(encoding="utf-8"))
                self.assertTrue(queried["destination_count_query_performed"])
                self.assertEqual(queried["reconciliation_status"], "count_match")
                self.assertEqual(
                    {item["bronze_table"]: item["current_destination_rows"] for item in queried["counts"]},
                    expected_counts,
                )
                self.assertTrue(manifest["load_ids"], "successful run should record at least one dlt load ID")
                with duckdb.connect(database, read_only=True) as destination:
                    rows_before_stale = self._destination_rows(destination, namespace, expected_counts)
                load_ids_before_stale = list(manifest["load_ids"])

                # Simulate schema drift in a second immutable fixture. The original
                # source remains untouched; the loader must reject the stale
                # confirmation without changing the drifted source or target rows.
                drift_path = root / "source_schema_drift.sqlite"
                shutil.copyfile(source_path, drift_path)
                source = sqlite3.connect(drift_path)
                try:
                    source.execute("ALTER TABLE orders ADD COLUMN drift_marker TEXT")
                finally:
                    source.close()
                drift_path.chmod(0o400)
                drifted_source = (_sha256(drift_path), drift_path.stat().st_mtime_ns)
                env["MIGRATION_SOURCE_URL"] = f"sqlite:///{drift_path}"
                manifests_before = {path.name for path in (runtime / "run-manifests").iterdir()}
                stale_result = run_script(runtime, "run_migration.py", [
                    *base_args,
                    "--confirm-target", manifest["target_identity"],
                    "--confirm-tables", ",".join(manifest["destination_tables"]),
                    "--confirm-schema-fingerprint", dry["schema_confirmation_fingerprint"],
                ], env)
                self.assertEqual(stale_result.returncode, 1, stale_result.stdout + stale_result.stderr)
                self.assertIn("differs from the reviewed fingerprint", stale_result.stdout + stale_result.stderr)
                self.assertEqual((_sha256(drift_path), drift_path.stat().st_mtime_ns), drifted_source)
                self.assertEqual((_sha256(source_path), source_path.stat().st_mtime_ns), source_before)
                for path in (source_path, drift_path):
                    self.assertFalse(Path(f"{path}-wal").exists())
                    self.assertFalse(Path(f"{path}-shm").exists())
                    self.assertFalse(Path(f"{path}-journal").exists())
                self.assertEqual(
                    {path.name for path in (runtime / "run-manifests").iterdir()}, manifests_before,
                    "stale schema refusal must not create a run manifest",
                )
                manifest_after_refusal = json.loads(manifests[0].read_text(encoding="utf-8"))
                self.assertEqual(manifest_after_refusal["load_ids"], load_ids_before_stale)
                with duckdb.connect(database, read_only=True) as destination:
                    after_refusal = {
                        table: destination.execute(f'SELECT count(*) FROM "{namespace}"."{table}"').fetchone()[0]
                        for table in expected_counts
                    }
                    rows_after_stale = self._destination_rows(destination, namespace, expected_counts)
                self.assertEqual(after_refusal, expected_counts)
                self.assertEqual(rows_after_stale, rows_before_stale)
        finally:
            os.umask(previous_umask)


if __name__ == "__main__":
    unittest.main()
