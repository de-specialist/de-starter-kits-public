"""Privacy and schema checks for the machine-readable migration acceptance report."""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "migration"))
try:
    report = importlib.import_module("acceptance_report")
    from _private_json import create_private_json, read_private_json
except ModuleNotFoundError as error:
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name not in {"dlt", "sqlalchemy"}:
        raise
    report = None


@unittest.skipIf(report is None, "migration runtime dependencies are not installed")
class MigrationAcceptanceReportTests(unittest.TestCase):
    run_id = "123e4567-e89b-42d3-a456-426614174000"
    fingerprint = "a" * 64

    def _manifest(self, migration_root: Path, *, status: str = "completed") -> dict[str, object]:
        pipeline_name = "pipe"
        namespace = "starterkit_migration_report"
        database = migration_root / ".dlt" / "pipelines" / pipeline_name / f"{pipeline_name}.duckdb"
        complete = status == "completed"
        entities = ["Orders", "orderItems"]
        tables = ["orders", "order_items"]
        source_counts = {
            entity: {
                "rows": 2 if complete else None,
                "payload_bytes": 64 if complete else None,
                "extraction_status": "complete" if complete else "incomplete",
                "provenance": "application_counted_canonical_json_payload",
            }
            for entity in entities
        }

        return {
            "run_id": self.run_id,
            "pipeline_name": pipeline_name,
            "source_kind": "csv",
            "entities": entities,
            "destination_tables": tables,
            "source_identity": {
                "kind": "csv",
                "files": [
                    {"entity": "Orders", "file_name": "Orders.csv", "size_bytes": 128},
                    {"entity": "orderItems", "file_name": "orderItems.csv", "size_bytes": 128},
                ],
                "identity_scope": "pre_extraction_file_metadata",
            },
            "source_row_counts": source_counts,
            "accepted_extraction_progress": {
                entity: {"rows": 2, "payload_bytes": 64} for entity in entities
            },
            "destination": "duckdb",
            "target_identity": f"duckdb/{database}/{namespace}",
            "bronze_namespace": namespace,
            "write_disposition": "replace",
            "source_schema_fingerprint": self.fingerprint,
            "typed_schema_fingerprint": "b" * 64,
            "source_mapping_fingerprint": "c" * 64,
            "source_mapping_rule_count": 1,
            "source_schema_fingerprint_status": "matched_at_extraction" if complete else "preflight_only",
            "typed_schema_fingerprint_status": "matched_at_extraction" if complete else "preflight_only",
            "status": status,
            "destination_write_may_be_partial": not complete,
            "load_ids": ["load.123"],
            "load_package_count": 1,
            "load_job_counts": {"completed_jobs": 1},
            "load_evidence_incomplete": False,
            "extraction_reject_summary": None if complete else {
                "table": "Orders", "row_ordinal": 3, "reason_code": "mapping_cast_failed",
                "accepted_table_rows": 2, "accepted_table_bytes": 64,
                "accepted_rows_total": 2, "accepted_bytes_total": 64,
                "unsafe_row_value": "SENSITIVE_ROW_VALUE",
            },
            "credential_payload": "SENSITIVE_CREDENTIAL_VALUE",
            "extra_row_value": "SENSITIVE_ROW_VALUE",
        }

    def _legacy_manifest(self, migration_root: Path) -> dict[str, object]:
        manifest = self._manifest(migration_root)
        manifest.pop("accepted_extraction_progress")
        manifest.pop("source_mapping_rule_count")
        manifest["source_row_counts"] = {
            "orderItems": {
                "rows": 2,
                "payload_bytes": 64,
                "provenance": "application_counted_canonical_json_payload",
            },
        }
        return manifest

    def test_completed_report_summarizes_private_reconciliation_without_sensitive_fields(self) -> None:
        with tempfile.TemporaryDirectory(prefix="acceptance-report-") as temporary:
            migration_root = Path(temporary) / "migration"
            manifest_dir = migration_root / "run-manifests"
            manifest_dir.mkdir(parents=True)
            migration_root.chmod(0o700)
            manifest_dir.chmod(0o700)
            previous_dir, previous_root = report.MANIFEST_DIR, report.evidence_index.ROOT
            try:
                report.MANIFEST_DIR = manifest_dir
                report.evidence_index.ROOT = migration_root
                create_private_json(manifest_dir, f"{self.run_id}.json", self._manifest(migration_root))
                create_private_json(manifest_dir, f"{self.run_id}.bronze-reconciliation.queried.json", {
                    "report_version": 2,
                    "run_id": self.run_id,
                    "report_type": "count_only_diagnostic",
                    "reconciliation_status": "count_match",
                    "cutover_ready": False,
                    "destination_count_query_performed": True,
                    "normalized_destination_tables": ["orders", "order_items"],
                    "counts": [
                        {
                            "source_entity": "Orders", "bronze_table": "orders", "status": "count_match",
                            "source_rows": 2, "dlt_prepared_rows": 2, "current_destination_rows": 2,
                            "source_count_provenance": "application_counted_canonical_json_payload",
                            "dlt_prepared_count_provenance": "dlt_1_32_0_last_normalize_info_row_counts",
                            "private_extra": "NOT_ALLOWED",
                        },
                        {
                            "source_entity": "orderItems", "bronze_table": "order_items", "status": "count_match",
                            "source_rows": 2, "dlt_prepared_rows": 2, "current_destination_rows": 2,
                            "source_count_provenance": "application_counted_canonical_json_payload",
                            "dlt_prepared_count_provenance": "dlt_1_32_0_last_normalize_info_row_counts",
                        },
                    ],
                    "credentials": "NOT_ALLOWED",
                })
                output = io.StringIO()
                with mock.patch.object(sys, "argv", ["acceptance_report.py", self.run_id]), \
                     contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    self.assertEqual(report.main(), 0, output.getvalue())
                artifact = read_private_json(manifest_dir, f"acceptance-report-{self.run_id}.json")
                self.assertEqual(artifact["run_status"], "completed")
                self.assertEqual(artifact["source_inventory"]["entity_counts"][0]["rows"], 2)
                self.assertEqual(artifact["source_inventory"]["entity_destination_tables"], [
                    {"source_entity": "Orders", "bronze_table": "orders"},
                    {"source_entity": "orderItems", "bronze_table": "order_items"},
                ])
                self.assertEqual(artifact["mapping_review"]["rule_count"], 1)
                self.assertEqual(artifact["reconciliation"]["status"], "count_match")
                self.assertTrue(artifact["destination_verification"]["load_ids_recorded"])
                self.assertFalse(artifact["cutover_ready"])
                serialized = json.dumps(artifact)
                for secret in ("SENSITIVE_CREDENTIAL_VALUE", "SENSITIVE_ROW_VALUE", "NOT_ALLOWED", str(migration_root)):
                    self.assertNotIn(secret, serialized)
                duplicate_output = io.StringIO()
                with mock.patch.object(sys, "argv", ["acceptance_report.py", self.run_id]), \
                     contextlib.redirect_stdout(duplicate_output), contextlib.redirect_stderr(duplicate_output):
                    self.assertEqual(report.main(), 1)
                self.assertIn("no existing file was overwritten", duplicate_output.getvalue())
            finally:
                report.MANIFEST_DIR, report.evidence_index.ROOT = previous_dir, previous_root

    def test_failed_manifest_reports_only_allowlisted_reject_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="acceptance-report-failed-") as temporary:
            migration_root = Path(temporary) / "migration"
            manifest_dir = migration_root / "run-manifests"
            manifest_dir.mkdir(parents=True)
            migration_root.chmod(0o700)
            manifest_dir.chmod(0o700)
            previous_dir, previous_root = report.MANIFEST_DIR, report.evidence_index.ROOT
            try:
                report.MANIFEST_DIR = manifest_dir
                report.evidence_index.ROOT = migration_root
                create_private_json(manifest_dir, f"{self.run_id}.json", self._manifest(migration_root, status="failed"))
                output = io.StringIO()
                with mock.patch.object(sys, "argv", ["acceptance_report.py", self.run_id, "--reconciliation", "none"]), \
                     contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    self.assertEqual(report.main(), 0, output.getvalue())
                artifact = read_private_json(manifest_dir, f"acceptance-report-{self.run_id}.json")
                self.assertEqual(artifact["run_status"], "failed")
                self.assertEqual(artifact["rejects"]["entries"][0]["reason_code"], "mapping_cast_failed")
                self.assertEqual(artifact["source_inventory"]["entity_counts"][0]["rows"], None)
                self.assertNotIn("row_ordinal", json.dumps(artifact))
                self.assertNotIn("SENSITIVE_ROW_VALUE", json.dumps(artifact))
            finally:
                report.MANIFEST_DIR, report.evidence_index.ROOT = previous_dir, previous_root

    def test_reconciliation_source_counts_and_provenance_must_match_manifest(self) -> None:
        for contradiction in ("source_count", "source_provenance"):
            with self.subTest(contradiction=contradiction), tempfile.TemporaryDirectory(
                prefix="acceptance-report-mismatch-"
            ) as temporary:
                migration_root = Path(temporary) / "migration"
                manifest_dir = migration_root / "run-manifests"
                manifest_dir.mkdir(parents=True)
                migration_root.chmod(0o700)
                manifest_dir.chmod(0o700)
                previous_dir, previous_root = report.MANIFEST_DIR, report.evidence_index.ROOT
                try:
                    report.MANIFEST_DIR = manifest_dir
                    report.evidence_index.ROOT = migration_root
                    create_private_json(manifest_dir, f"{self.run_id}.json", self._manifest(migration_root))
                    first_row = {
                        "source_entity": "Orders", "bronze_table": "orders", "status": "count_match",
                        "source_rows": 2, "dlt_prepared_rows": 2, "current_destination_rows": 2,
                        "source_count_provenance": "application_counted_canonical_json_payload",
                        "dlt_prepared_count_provenance": "dlt_1_32_0_last_normalize_info_row_counts",
                    }
                    if contradiction == "source_count":
                        first_row.update({"source_rows": 7, "status": "count_mismatch"})
                    else:
                        first_row["source_count_provenance"] = "rows_yielded_from_csv_resource"
                    create_private_json(manifest_dir, f"{self.run_id}.bronze-reconciliation.queried.json", {
                        "report_version": 2,
                        "run_id": self.run_id,
                        "report_type": "count_only_diagnostic",
                        "reconciliation_status": "count_mismatch" if contradiction == "source_count" else "count_match",
                        "cutover_ready": False,
                        "destination_count_query_performed": True,
                        "normalized_destination_tables": ["orders", "order_items"],
                        "counts": [first_row, {
                            "source_entity": "orderItems", "bronze_table": "order_items", "status": "count_match",
                            "source_rows": 2, "dlt_prepared_rows": 2, "current_destination_rows": 2,
                            "source_count_provenance": "application_counted_canonical_json_payload",
                            "dlt_prepared_count_provenance": "dlt_1_32_0_last_normalize_info_row_counts",
                        }],
                    })
                    output = io.StringIO()
                    with mock.patch.object(sys, "argv", ["acceptance_report.py", self.run_id]), \
                         contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                        self.assertEqual(report.main(), 1)
                    self.assertIn("ValueError", output.getvalue())
                    self.assertFalse((manifest_dir / f"acceptance-report-{self.run_id}.json").exists())
                finally:
                    report.MANIFEST_DIR, report.evidence_index.ROOT = previous_dir, previous_root

    def test_legacy_manifest_missing_counts_and_mapping_rule_count_are_not_invented(self) -> None:
        with tempfile.TemporaryDirectory(prefix="acceptance-report-legacy-") as temporary:
            migration_root = Path(temporary) / "migration"
            manifest_dir = migration_root / "run-manifests"
            manifest_dir.mkdir(parents=True)
            migration_root.chmod(0o700)
            manifest_dir.chmod(0o700)
            previous_dir, previous_root = report.MANIFEST_DIR, report.evidence_index.ROOT
            try:
                report.MANIFEST_DIR = manifest_dir
                report.evidence_index.ROOT = migration_root
                create_private_json(manifest_dir, f"{self.run_id}.json", self._legacy_manifest(migration_root))
                output = io.StringIO()
                with mock.patch.object(sys, "argv", ["acceptance_report.py", self.run_id, "--reconciliation", "none"]), \
                     contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    self.assertEqual(report.main(), 0, output.getvalue())
                artifact = read_private_json(manifest_dir, f"acceptance-report-{self.run_id}.json")
                missing_count = artifact["source_inventory"]["entity_counts"][0]
                self.assertEqual(missing_count["entity"], "Orders")
                self.assertIsNone(missing_count["rows"])
                self.assertIsNone(missing_count["payload_bytes"])
                self.assertEqual(missing_count["extraction_status"], "unverified_legacy")
                self.assertEqual(missing_count["count_provenance"], "not_recorded")
                self.assertIsNone(artifact["mapping_review"]["rule_count"])
                self.assertEqual(artifact["mapping_review"]["rule_count_status"], "not_recorded")
            finally:
                report.MANIFEST_DIR, report.evidence_index.ROOT = previous_dir, previous_root

    def test_reconciliation_cannot_assert_source_count_missing_from_legacy_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="acceptance-report-legacy-reconcile-") as temporary:
            migration_root = Path(temporary) / "migration"
            manifest_dir = migration_root / "run-manifests"
            manifest_dir.mkdir(parents=True)
            migration_root.chmod(0o700)
            manifest_dir.chmod(0o700)
            previous_dir, previous_root = report.MANIFEST_DIR, report.evidence_index.ROOT
            try:
                report.MANIFEST_DIR = manifest_dir
                report.evidence_index.ROOT = migration_root
                create_private_json(manifest_dir, f"{self.run_id}.json", self._legacy_manifest(migration_root))
                create_private_json(manifest_dir, f"{self.run_id}.bronze-reconciliation.offline.json", {
                    "report_version": 2,
                    "run_id": self.run_id,
                    "report_type": "count_only_diagnostic",
                    "reconciliation_status": "count_match",
                    "cutover_ready": False,
                    "destination_count_query_performed": False,
                    "normalized_destination_tables": ["orders", "order_items"],
                    "counts": [
                        {
                            "source_entity": "Orders", "bronze_table": "orders", "status": "count_match",
                            "source_rows": 2, "dlt_prepared_rows": 2, "current_destination_rows": 2,
                            "source_count_provenance": "application_counted_canonical_json_payload",
                            "dlt_prepared_count_provenance": "dlt_1_32_0_last_normalize_info_row_counts",
                        },
                        {
                            "source_entity": "orderItems", "bronze_table": "order_items", "status": "count_match",
                            "source_rows": 2, "dlt_prepared_rows": 2, "current_destination_rows": 2,
                            "source_count_provenance": "application_counted_canonical_json_payload",
                            "dlt_prepared_count_provenance": "dlt_1_32_0_last_normalize_info_row_counts",
                        },
                    ],
                })
                output = io.StringIO()
                with mock.patch.object(sys, "argv", ["acceptance_report.py", self.run_id]), \
                     contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    self.assertEqual(report.main(), 1)
                self.assertIn("ValueError", output.getvalue())
                self.assertFalse((manifest_dir / f"acceptance-report-{self.run_id}.json").exists())
            finally:
                report.MANIFEST_DIR, report.evidence_index.ROOT = previous_dir, previous_root


if __name__ == "__main__":
    unittest.main()
