"""Offline failure-manifest checks for per-resource extraction progress."""

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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "migration"))
try:
    migration = importlib.import_module("migration.run_migration")
except ModuleNotFoundError as error:
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name not in {"dlt", "sqlalchemy"}:
        raise
    migration = None


@unittest.skipIf(migration is None, "migration runtime dependencies are not installed")
class ExtractionStatusTests(unittest.TestCase):
    def test_rejection_marks_a_registered_entity_incomplete(self):
        budget = migration.ExtractionBudget(SimpleNamespace(
            max_rows_per_table=10, max_bytes_per_row=1000, max_bytes_per_table=1000,
            max_rows_total=20, max_bytes_total=2000,
        ))
        budget.register_tables(["sql_table"])
        self.assertEqual(budget.extraction_status["sql_table"], "not_started")

        budget.reject("sql_table", 1, "mapping_cast_failed")

        self.assertEqual(budget.extraction_status["sql_table"], "incomplete")
        counts, progress = budget.manifest_counts(["sql_table"])
        self.assertIsNone(counts["sql_table"]["rows"])
        self.assertEqual(progress["sql_table"], {"rows": 0, "payload_bytes": 0})

    def test_malformed_first_resource_leaves_second_not_started_and_does_not_load(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            csv_dir = root / "csv"
            csv_dir.mkdir()
            (csv_dir / "a_first.csv").write_text("id,name\none\n", encoding="utf-8")
            (csv_dir / "b_second.csv").write_text("id,name\ntwo,valid\n", encoding="utf-8")
            dataset = "starterkit_migration_status"
            pipeline_name = "status_fixture"
            pipeline_dir = root / ".dlt" / "pipelines" / pipeline_name
            pipeline_dir.mkdir(parents=True)
            database = pipeline_dir / f"{pipeline_name}.duckdb"

            class DestinationConfig:
                local_dir = str(root)
                credentials = SimpleNamespace(database=str(database))

                @staticmethod
                def normalize_dataset_name(_schema):
                    return dataset

            client = SimpleNamespace(
                config=DestinationConfig(),
                schema=SimpleNamespace(naming=migration.SnakeCaseNamingConvention()),
                capabilities=SimpleNamespace(casefold_identifier=str.casefold),
            )

            class FakePipeline:
                destination = SimpleNamespace(destination_type="dlt.destinations.duckdb")
                staging = None
                config = SimpleNamespace(use_single_dataset=True, dataset_name_layout=None)
                has_pending_data = False
                last_trace = None

                def __init__(self):
                    self.pipeline_name = pipeline_name
                    self.working_dir = str(pipeline_dir)
                    self.dataset_name = dataset
                    self.extract_calls = 0
                    self.load_calls = 0

                def destination_client(self):
                    return client

                def extract(self, resources, **_kwargs):
                    self.extract_calls += 1
                    for resource in resources:
                        for _row in resource:
                            pass

                def normalize(self, **_kwargs):
                    self.last_trace = SimpleNamespace(last_normalize_info=SimpleNamespace(row_counts={}))

                def load(self, **_kwargs):
                    self.load_calls += 1
                    raise AssertionError("load must not run after extraction rejection")

            pipeline = FakePipeline()
            inventory = migration.inventory_csv_inputs(csv_dir, "*.csv", None, count_rows=False)
            confirmation = migration.schema_confirmation_fingerprint(
                migration.schema_fingerprint(inventory), migration.typed_schema_fingerprint(inventory),
                migration.mappings_fingerprint({}),
            )
            args = [
                "run_migration.py", "--source", "csv", "--destination", "duckdb",
                "--dataset", dataset, "--pipeline-name", "fixture", "--csv-dir", str(csv_dir),
                "--confirm-target", f"duckdb/{database}/{dataset}",
                "--confirm-tables", "a_first,b_second",
                "--confirm-schema-fingerprint", confirmation,
            ]
            previous_root, previous_argv = migration.MIGRATION_ROOT, sys.argv
            previous_umask = os.umask(0o077)
            try:
                migration.MIGRATION_ROOT = root
                sys.argv = args
                with mock.patch.object(migration.dlt, "pipeline", return_value=pipeline), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    result = migration.main()
            finally:
                migration.MIGRATION_ROOT, sys.argv = previous_root, previous_argv
                os.umask(previous_umask)

            self.assertEqual(result, 1)
            self.assertEqual(pipeline.extract_calls, 1)
            self.assertEqual(pipeline.load_calls, 0)
            manifest_path, = (root / "run-manifests").glob("*.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            counts = manifest["source_row_counts"]
            self.assertEqual(counts["a_first"]["extraction_status"], "incomplete")
            self.assertIsNone(counts["a_first"]["rows"])
            self.assertEqual(counts["b_second"]["extraction_status"], "not_started")
            self.assertIsNone(counts["b_second"]["rows"])
            self.assertEqual(manifest["accepted_extraction_progress"]["a_first"]["rows"], 0)
            self.assertEqual(manifest["accepted_extraction_progress"]["b_second"]["rows"], 0)
            self.assertEqual(manifest["extraction_reject_summary"]["reason_code"], "malformed_or_unreadable_csv")


if __name__ == "__main__":
    unittest.main()
