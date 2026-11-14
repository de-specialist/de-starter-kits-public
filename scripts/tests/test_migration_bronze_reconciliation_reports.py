"""Offline CLI regression for distinct, exclusive Bronze reconciliation reports."""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "migration"))

try:
    reconcile_bronze = importlib.import_module("migration.reconcile_bronze")
except ModuleNotFoundError as error:
    if error.name not in {"dlt", "sqlalchemy"}:
        raise
    reconcile_bronze = None


@unittest.skipIf(reconcile_bronze is None, "migration runtime dependencies are not installed")
class BronzeReconciliationReportTests(unittest.TestCase):
    run_id = "11111111-1111-4111-8111-111111111111"
    target_identity = "duckdb/test-target/starterkit_migration_test"
    table_confirmation = "customers,order_items"

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.manifest_directory = self.root / "run-manifests"
        reconcile_bronze.ensure_private_directory(self.manifest_directory)
        pipeline_name = "fixture_duckdb_starterkit_migration_test_0123456789abcdef01234567"
        record = {
            "run_id": self.run_id,
            "status": "completed",
            "write_disposition": "replace",
            "destination": "duckdb",
            "target_identity": self.target_identity,
            "bronze_namespace": "starterkit_migration_test",
            "pipeline_name": pipeline_name,
            "entities": ["customers", "order_items"],
            "destination_tables": ["customers", "order_items"],
            "source_row_counts": {
                "customers": {"rows": 2, "provenance": "synthetic"},
                "order_items": {"rows": 3, "provenance": "synthetic"},
            },
            "dlt_prepared_row_counts": {
                "customers": {"rows": 2, "provenance": "synthetic"},
                "order_items": {"rows": 3, "provenance": "synthetic"},
            },
        }
        reconcile_bronze.create_private_json(self.manifest_directory, f"{self.run_id}.json", record)
        self.record = record

    def _invoke(self, *arguments):
        previous_root = reconcile_bronze.MIGRATION_ROOT
        previous_argv = sys.argv
        stdout = io.StringIO()
        stderr = io.StringIO()
        reconcile_bronze.MIGRATION_ROOT = self.root
        sys.argv = ["reconcile_bronze.py", "--run-id", self.run_id, *arguments]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = reconcile_bronze.main()
        finally:
            reconcile_bronze.MIGRATION_ROOT = previous_root
            sys.argv = previous_argv
        return result, stdout.getvalue(), stderr.getvalue()

    def test_offline_then_queried_reports_use_distinct_private_paths(self):
        offline_result, offline_stdout, offline_stderr = self._invoke()

        self.assertEqual(offline_result, 0, offline_stderr)
        self.assertIn(f"Normalized table confirmation (--confirm-tables): {self.table_confirmation}", offline_stdout)
        offline_path = self.manifest_directory / f"{self.run_id}.bronze-reconciliation.offline.json"
        self.assertTrue(offline_path.is_file())
        offline_bytes = offline_path.read_bytes()
        offline_report = json.loads(offline_bytes)
        self.assertFalse(offline_report["destination_count_query_performed"])
        self.assertEqual(offline_report["normalized_destination_tables"], ["customers", "order_items"])

        pipeline = SimpleNamespace(has_pending_data=False)
        queried_result, queried_stdout, queried_stderr = None, "", ""
        with mock.patch.object(reconcile_bronze, "_attach_pipeline", return_value=pipeline), \
             mock.patch.object(
                 reconcile_bronze,
                 "_resolve_dlt_destination",
                 return_value=(None, None, self.target_identity, self.record["bronze_namespace"]),
             ), \
             mock.patch.object(
                 reconcile_bronze,
                 "_destination_counts",
                 return_value={"customers": 2, "order_items": 3},
             ) as counts:
            queried_result, queried_stdout, queried_stderr = self._invoke(
                "--query-destination",
                "--confirm-target", self.target_identity,
                "--confirm-tables", self.table_confirmation,
            )

        self.assertEqual(queried_result, 0, queried_stderr)
        self.assertIn(f"Normalized table confirmation (--confirm-tables): {self.table_confirmation}", queried_stdout)
        counts.assert_called_once_with(pipeline, ["customers", "order_items"])
        queried_path = self.manifest_directory / f"{self.run_id}.bronze-reconciliation.queried.json"
        self.assertTrue(queried_path.is_file())
        queried_report = json.loads(queried_path.read_text(encoding="utf-8"))
        self.assertTrue(queried_report["destination_count_query_performed"])
        self.assertEqual(queried_report["counts"][0]["current_destination_rows"], 2)
        self.assertEqual(offline_path.read_bytes(), offline_bytes)

    def test_existing_report_mode_is_never_overwritten(self):
        result, _, stderr = self._invoke()
        self.assertEqual(result, 0, stderr)
        report_path = self.manifest_directory / f"{self.run_id}.bronze-reconciliation.offline.json"
        original = report_path.read_bytes()

        repeated_result, _, repeated_stderr = self._invoke()

        self.assertEqual(repeated_result, 1)
        self.assertIn("FileExistsError", repeated_stderr)
        self.assertEqual(report_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
