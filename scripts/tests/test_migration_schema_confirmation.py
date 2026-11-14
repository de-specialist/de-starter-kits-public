"""Offline regression checks for dry-run schema confirmation before replacement."""

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
from types import SimpleNamespace
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "migration"))

def _load_migration_runtime(importer=importlib.import_module, *, ci: bool | None = None):
    """Allow only genuinely absent optional runtime packages to skip local tests."""
    try:
        return importer("migration.run_migration")
    except ModuleNotFoundError as error:
        running_in_ci = (
            os.environ.get("CI", "").casefold() in {"1", "true", "yes"}
            if ci is None else ci
        )
        if running_in_ci or error.name not in {"dlt", "sqlalchemy"}:
            raise
        return None


run_migration = _load_migration_runtime()


@unittest.skipIf(run_migration is None, "migration runtime dependencies are not installed")
class MigrationSchemaConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.csv_dir = self.root / "csv"
        self.csv_dir.mkdir()
        self.dataset = "starterkit_migration_s"
        # Keep the fake dlt schema name below dlt's 64-character limit; the CLI's
        # generated name is not the subject of this source-schema confirmation test.
        self.pipeline_name = "schema_fixture"
        self.pipeline_dir = self.root / ".dlt" / "pipelines" / self.pipeline_name
        self.pipeline_dir.mkdir(parents=True)
        self.database = self.pipeline_dir / f"{self.pipeline_name}.duckdb"
        self.pipeline = self._fake_pipeline()

    def _write_csv(
        self,
        filename: str = "customers.csv",
        header: str = "customer_id,created_at,country",
        rows: str = "c1,2025-01-01T00:00:00Z,AU",
    ) -> None:
        (self.csv_dir / filename).write_text(f"{header}\n{rows}\n", encoding="utf-8")

    def _fake_pipeline(self):
        dataset = self.dataset
        database = self.database
        pipeline_name = self.pipeline_name
        pipeline_dir = self.pipeline_dir

        class DestinationConfig:
            local_dir = str(self.root)
            credentials = SimpleNamespace(database=str(database))

            @staticmethod
            def normalize_dataset_name(_schema):
                return dataset

        client = SimpleNamespace(
            config=DestinationConfig(),
            schema=SimpleNamespace(naming=run_migration.SnakeCaseNamingConvention()),
            capabilities=SimpleNamespace(casefold_identifier=str.casefold),
        )

        class LoadInfo:
            load_ids = ["synthetic-load-id"]
            load_packages = [
                SimpleNamespace(jobs={
                    "new_jobs": [], "started_jobs": [], "completed_jobs": [], "failed_jobs": [],
                })
            ]

            @staticmethod
            def raise_on_failed_jobs():
                return None

        class FakePipeline:
            def __init__(self):
                self.pipeline_name = pipeline_name
                self.working_dir = str(pipeline_dir)
                self.dataset_name = dataset
                self.destination = SimpleNamespace(destination_type="dlt.destinations.duckdb")
                self.staging = None
                self.config = SimpleNamespace(use_single_dataset=True, dataset_name_layout=None)
                self.has_pending_data = False
                self.extract_calls = 0
                self.load_calls = 0
                self.last_trace = None

            def destination_client(self):
                return client

            def extract(self, resources, **_kwargs):
                self.extract_calls += 1
                for resource in resources:
                    for _row in resource:
                        pass

            def normalize(self, **_kwargs):
                self.last_trace = SimpleNamespace(
                    last_normalize_info=SimpleNamespace(row_counts={"customers": 1})
                )

            def load(self, **_kwargs):
                self.load_calls += 1
                return LoadInfo()

        return FakePipeline()

    def _schema_confirmation(self) -> str:
        inventory = run_migration.inventory_csv_inputs(self.csv_dir, "*.csv", None, count_rows=False)
        return run_migration.schema_confirmation_fingerprint(
            run_migration.schema_fingerprint(inventory),
            run_migration.typed_schema_fingerprint(inventory),
            run_migration.mappings_fingerprint({}),
        )

    def _invoke(self, *extra_arguments: str):
        arguments = [
            "run_migration.py", "--source", "csv", "--destination", "duckdb",
            "--dataset", self.dataset, "--pipeline-name", "fixture", "--csv-dir", str(self.csv_dir),
            "--confirm-target", f"duckdb/{self.database}/{self.dataset}",
            "--confirm-tables", "customers",
            *extra_arguments,
        ]
        previous_root = run_migration.MIGRATION_ROOT
        previous_argv = sys.argv
        previous_umask = os.umask(0o077)
        stdout = io.StringIO()
        stderr = io.StringIO()
        confirmation_check = mock.patch.object(
            run_migration, "require_schema_confirmation", wraps=run_migration.require_schema_confirmation
        )
        try:
            run_migration.MIGRATION_ROOT = self.root
            sys.argv = arguments
            with confirmation_check as schema_check, \
                 mock.patch.object(run_migration.dlt, "pipeline", return_value=self.pipeline), \
                 contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = run_migration.main()
        finally:
            run_migration.MIGRATION_ROOT = previous_root
            sys.argv = previous_argv
            os.umask(previous_umask)
        return result, stdout.getvalue(), stderr.getvalue(), schema_check.call_args_list

    def _assert_no_destination_work(self) -> None:
        self.assertEqual(self.pipeline.extract_calls, 0)
        self.assertEqual(self.pipeline.load_calls, 0)
        self.assertFalse((self.root / "run-manifests").exists())

    def test_missing_schema_confirmation_fails_before_extract_or_load(self) -> None:
        self._write_csv()

        result, _stdout, stderr, calls = self._invoke()

        self.assertEqual(result, 1)
        self.assertIn("--confirm-schema-fingerprint from dry-run is required", stderr)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0].args[0])
        self._assert_no_destination_work()

    def test_wrong_schema_confirmation_fails_before_extract_or_load(self) -> None:
        self._write_csv()

        result, _stdout, stderr, calls = self._invoke("--confirm-schema-fingerprint", "0" * 64)

        self.assertEqual(result, 1)
        self.assertIn("differs from the reviewed fingerprint", stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0], "0" * 64)
        self._assert_no_destination_work()

    def test_changed_header_after_dry_run_confirmation_fails_before_extract_or_load(self) -> None:
        self._write_csv()
        reviewed_fingerprint = self._schema_confirmation()
        self._write_csv(header="customer_id,created_at,country,unexpected_field",
                        rows="c1,2025-01-01T00:00:00Z,AU,changed")

        result, _stdout, _stderr, calls = self._invoke("--confirm-schema-fingerprint", reviewed_fingerprint)

        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0], reviewed_fingerprint)
        self._assert_no_destination_work()

    def test_matching_schema_confirmation_reaches_existing_table_and_load_guards(self) -> None:
        self._write_csv()
        confirmed = self._schema_confirmation()

        result, stdout, stderr, calls = self._invoke("--confirm-schema-fingerprint", confirmed)

        self.assertEqual(result, 0, stderr)
        self.assertEqual(len(calls), 1)
        self.assertIn("Migration load completed", stdout)
        self.assertEqual(self.pipeline.extract_calls, 1)
        self.assertEqual(self.pipeline.load_calls, 1)
        manifest_files = list((self.root / "run-manifests").glob("*.json"))
        self.assertEqual(len(manifest_files), 1)
        manifest = json.loads(manifest_files[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_confirmation_fingerprint"], confirmed)
        self.assertEqual(manifest["schema_confirmation_algorithm"], "sha256-schema-and-mapping-v1")

    def test_pending_packages_refuse_before_extract_without_new_manifest(self) -> None:
        self._write_csv()
        self.pipeline.has_pending_data = True
        confirmed = self._schema_confirmation()

        result, _stdout, stderr, _calls = self._invoke("--confirm-schema-fingerprint", confirmed)

        self.assertEqual(result, 1)
        self.assertIn("pending dlt packages", stderr)
        self._assert_no_destination_work()

    def test_load_exception_records_possible_partial_write_without_exception_details(self) -> None:
        self._write_csv()
        confirmed = self._schema_confirmation()

        def fail_after_load_begins(**_kwargs):
            self.pipeline.load_calls += 1
            raise RuntimeError("private driver detail secret-marker")

        self.pipeline.load = fail_after_load_begins
        result, _stdout, stderr, _calls = self._invoke("--confirm-schema-fingerprint", confirmed)

        self.assertEqual(result, 1)
        self.assertIn("Migration stopped (RuntimeError)", stderr)
        self.assertNotIn("private driver detail", stderr)
        self.assertNotIn("secret-marker", stderr)
        self.assertEqual(self.pipeline.extract_calls, 1)
        self.assertEqual(self.pipeline.load_calls, 1)
        manifest_files = list((self.root / "run-manifests").glob("*.json"))
        self.assertEqual(len(manifest_files), 1)
        manifest = json.loads(manifest_files[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")
        self.assertTrue(manifest["destination_write_may_be_partial"])
        self.assertNotIn("private driver detail", json.dumps(manifest))
        self.assertNotIn("secret-marker", json.dumps(manifest))

    def test_mapping_change_after_review_fails_before_extract_or_load(self) -> None:
        self._write_csv("orders.csv", "order_id,legacy_order_id", "o1,old1")
        mapping_path = self.root / "mapping.json"
        reviewed_mapping = {
            "version": 1,
            "tables": {"orders": [{"source": "legacy_order_id", "target": "canonical_order_id", "cast": "string"}]},
        }
        mapping_path.write_text(json.dumps(reviewed_mapping), encoding="utf-8")
        inventory = run_migration.inventory_csv_inputs(self.csv_dir, "*.csv", None, count_rows=False)
        reviewed_mappings = run_migration.load_mapping_file(mapping_path)
        reviewed = run_migration.schema_confirmation_fingerprint(
            run_migration.schema_fingerprint(inventory),
            run_migration.typed_schema_fingerprint(inventory),
            run_migration.mappings_fingerprint(reviewed_mappings),
        )
        changed_mapping = {
            "version": 1,
            "tables": {"orders": [{"source": "legacy_order_id", "target": "canonical_order_id", "cast": "integer"}]},
        }
        mapping_path.write_text(json.dumps(changed_mapping), encoding="utf-8")
        previous_root = run_migration.MIGRATION_ROOT
        previous_argv = sys.argv
        previous_umask = os.umask(0o077)
        try:
            run_migration.MIGRATION_ROOT = self.root
            sys.argv = [
                "run_migration.py", "--source", "csv", "--destination", "duckdb",
                "--dataset", self.dataset, "--pipeline-name", "fixture", "--csv-dir", str(self.csv_dir),
                "--mapping-file", str(mapping_path),
                "--confirm-target", f"duckdb/{self.database}/{self.dataset}",
                "--confirm-tables", "orders", "--confirm-schema-fingerprint", reviewed,
            ]
            with mock.patch.object(run_migration.dlt, "pipeline", return_value=self.pipeline), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = run_migration.main()
        finally:
            run_migration.MIGRATION_ROOT = previous_root
            sys.argv = previous_argv
            os.umask(previous_umask)

        self.assertEqual(result, 1)
        self._assert_no_destination_work()

    def test_unexpected_value_error_remains_generic_and_does_not_leak(self) -> None:
        self._write_csv()
        confirmed = self._schema_confirmation()
        with mock.patch.object(run_migration, "inventory_csv_inputs", side_effect=ValueError("secret marker")):
            result, _stdout, stderr, _calls = self._invoke("--confirm-schema-fingerprint", confirmed)
        self.assertEqual(result, 1)
        self.assertIn("Migration stopped (ValueError)", stderr)
        self.assertNotIn("secret marker", stderr)
        self.assertEqual(self.pipeline.extract_calls, 0)
        self.assertEqual(self.pipeline.load_calls, 0)


if __name__ == "__main__":
    unittest.main()
