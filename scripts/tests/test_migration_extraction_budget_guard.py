"""Prove extraction-budget rejection leaves an existing local DuckDB table unchanged."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.tests._dlt_isolated_runtime import isolated_environment, prepare_runtime, run_script


try:
    import dlt  # noqa: F401
    import duckdb
except ModuleNotFoundError as error:
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name not in {"dlt", "sqlalchemy", "duckdb"}:
        raise
    dlt = None
    duckdb = None


@unittest.skipIf(dlt is None or duckdb is None, "migration runtime dependencies are not installed")
class MigrationExtractionBudgetGuardTests(unittest.TestCase):
    def _invoke(self, runtime: Path, env: dict[str, str], *args: str, expected: int = 0) -> str:
        result = run_script(runtime, "run_migration.py", list(args), env)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, expected, output)
        return output

    def _rows(self, target_database: str, namespace: str) -> list[tuple]:
        with duckdb.connect(target_database, read_only=True) as destination:
            return destination.execute(
                f'SELECT customer_id, created_at, country FROM "{namespace}"."customers" ORDER BY customer_id'
            ).fetchall()

    def test_budget_rejection_before_load_preserves_successful_target(self) -> None:
        previous_umask = os.umask(0o077)
        try:
            with tempfile.TemporaryDirectory(prefix="migration-extraction-budget-") as temporary:
                root = Path(temporary)
                runtime = prepare_runtime(root)
                env = isolated_environment(runtime, root)
                csv_dir = root / "csv"
                csv_dir.mkdir()
                source = csv_dir / "customers.csv"
                source.write_text(
                    "customer_id,created_at,country\nc1,2025-01-01T00:00:00Z,AU\n",
                    encoding="utf-8",
                )
                base_args = [
                    "--source", "csv", "--destination", "duckdb",
                    "--dataset", "starterkit_migration_budget_guard",
                    "--pipeline-name", "budget_guard",
                    "--csv-dir", str(csv_dir),
                    "--tables", "customers",
                ]

                dry = json.loads(self._invoke(runtime, env, *base_args, "--dry-run"))
                self._invoke(
                    runtime,
                    env,
                    *base_args,
                    "--confirm-target", dry["target_identity"],
                    "--confirm-tables", dry["table_confirmation"],
                    "--confirm-schema-fingerprint", dry["schema_confirmation_fingerprint"],
                )
                manifest_dir = runtime / "run-manifests"
                baseline_manifests = list(manifest_dir.glob("*.json"))
                self.assertEqual(len(baseline_manifests), 1)
                baseline = json.loads(baseline_manifests[0].read_text(encoding="utf-8"))
                target_tail = baseline["target_identity"].removeprefix("duckdb/")
                database, separator, namespace = target_tail.rpartition("/")
                self.assertTrue(separator)
                original_rows = self._rows(database, namespace)
                self.assertEqual(original_rows, [("c1", original_rows[0][1], "AU")])

                source.write_text(
                    "customer_id,created_at,country\n"
                    "c1,2025-01-01T00:00:00Z,AU\n"
                    "c2,2025-01-02T00:00:00Z,NZ\n",
                    encoding="utf-8",
                )
                limited_args = [*base_args, "--max-rows-per-table", "1"]
                limited_dry = json.loads(self._invoke(runtime, env, *limited_args, "--dry-run"))
                self.assertEqual(limited_dry["schema_confirmation_fingerprint"], dry["schema_confirmation_fingerprint"])
                stale_before = {path.name for path in manifest_dir.glob("*.json")}
                output = self._invoke(
                    runtime,
                    env,
                    *limited_args,
                    "--confirm-target", limited_dry["target_identity"],
                    "--confirm-tables", limited_dry["table_confirmation"],
                    "--confirm-schema-fingerprint", limited_dry["schema_confirmation_fingerprint"],
                    expected=1,
                )
                self.assertIn("Migration stopped (PipelineStepFailed)", output)
                new_manifests = [
                    path for path in manifest_dir.glob("*.json")
                    if path.name not in stale_before and not path.name.startswith("evidence-index-")
                ]
                self.assertEqual(len(new_manifests), 1)
                failed = json.loads(new_manifests[0].read_text(encoding="utf-8"))
                self.assertEqual(failed["status"], "failed")
                self.assertFalse(failed["destination_write_may_be_partial"])
                self.assertEqual(failed["extraction_reject_summary"]["reason_code"], "extraction_budget_exceeded")
                self.assertEqual(failed["extraction_reject_summary"]["accepted_rows_total"], 1)
                self.assertEqual(failed["source_row_counts"]["customers"]["extraction_status"], "incomplete")
                self.assertIsNone(failed["source_row_counts"]["customers"]["rows"])
                self.assertEqual(self._rows(database, namespace), original_rows)
        finally:
            os.umask(previous_umask)


if __name__ == "__main__":
    unittest.main()
