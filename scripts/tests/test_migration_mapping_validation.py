"""Offline checks for migration mapping and normalized-column rejection."""

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
class MigrationMappingValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.csv_dir = self.root / "csv"
        self.csv_dir.mkdir()
        self.dataset = "starterkit_migration_map"
        self.pipeline_name = run_migration._pipeline_name(
            SimpleNamespace(pipeline_name="fixture", destination="duckdb"),
            self.dataset,
            f"duckdb/{self.dataset}",
        )
        self.pipeline_dir = self.root / ".dlt" / "pipelines" / self.pipeline_name
        self.pipeline_dir.mkdir(parents=True)
        self.database = self.pipeline_dir / f"{self.pipeline_name}.duckdb"
        self.pipeline = self._fake_pipeline()

    def _fake_pipeline(self):
        dataset = self.dataset
        database = self.database

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

        class FakePipeline:
            pipeline_name = self.pipeline_name
            working_dir = str(self.pipeline_dir)
            dataset_name = dataset
            destination = SimpleNamespace(destination_type="dlt.destinations.duckdb")
            staging = None
            config = SimpleNamespace(use_single_dataset=True, dataset_name_layout=None)
            has_pending_data = False

            def __init__(self):
                self.extract_calls = 0
                self.load_calls = 0
                self.extracted_rows = []
                self.last_trace = None

            def destination_client(self):
                return client

            def extract(self, *_args, **_kwargs):
                self.extract_calls += 1
                self.extracted_rows = [row for resource in _args[0] for row in resource]

            def normalize(self, **_kwargs):
                self.last_trace = SimpleNamespace(
                    last_normalize_info=SimpleNamespace(row_counts={"customers": len(self.extracted_rows)})
                )

            def load(self, *_args, **_kwargs):
                self.load_calls += 1
                return SimpleNamespace(
                    loads_ids=["synthetic-load-id"],
                    load_packages=[SimpleNamespace(jobs={
                        "new_jobs": [], "started_jobs": [], "completed_jobs": [], "failed_jobs": [],
                    })],
                    raise_on_failed_jobs=lambda: None,
                )

        return FakePipeline()

    def _run(self, csv_name: str, csv_content: str, mapping_payload=None, *, confirm=True):
        (self.csv_dir / csv_name).write_text(csv_content, encoding="utf-8")
        arguments = [
            "run_migration.py", "--source", "csv", "--destination", "duckdb",
            "--dataset", self.dataset, "--pipeline-name", "fixture", "--csv-dir", str(self.csv_dir),
            "--confirm-target", f"duckdb/{self.database}/{self.dataset}",
        ]
        if mapping_payload is not None:
            mapping_path = self.root / "mapping.json"
            mapping_path.write_text(
                mapping_payload if isinstance(mapping_payload, str) else json.dumps(mapping_payload),
                encoding="utf-8",
            )
            arguments.extend(("--mapping-file", str(mapping_path)))
        if confirm:
            inventory = run_migration.inventory_csv_inputs(self.csv_dir, "*.csv", None, count_rows=False)
            try:
                mappings = run_migration.load_mapping_file(self.root / "mapping.json") if mapping_payload is not None else {}
            except run_migration.MappingConfigurationError:
                mappings = {}
            confirmation = run_migration.schema_confirmation_fingerprint(
                run_migration.schema_fingerprint(inventory),
                run_migration.typed_schema_fingerprint(inventory),
                run_migration.mappings_fingerprint(mappings),
            )
            arguments.extend((
                "--confirm-tables", ",".join(item["table"] for item in inventory),
                "--confirm-schema-fingerprint", confirmation,
            ))

        previous_root = run_migration.MIGRATION_ROOT
        previous_argv = sys.argv
        previous_umask = os.umask(0o077)
        output = io.StringIO()
        factory = mock.patch.object(run_migration.dlt, "pipeline", return_value=self.pipeline)
        try:
            run_migration.MIGRATION_ROOT = self.root
            sys.argv = arguments
            with factory as pipeline_factory, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = run_migration.main()
        finally:
            run_migration.MIGRATION_ROOT = previous_root
            sys.argv = previous_argv
            os.umask(previous_umask)
        return result, output.getvalue(), pipeline_factory.call_count

    def _assert_rejected_before_load(self, result: int, pipeline_calls: int, *, expected_pipeline_calls: int = 1) -> None:
        self.assertEqual(result, 1)
        self.assertEqual(pipeline_calls, expected_pipeline_calls)
        self.assertEqual(self.pipeline.extract_calls, 0)
        self.assertEqual(self.pipeline.load_calls, 0)
        self.assertFalse((self.root / "run-manifests").exists())

    def test_alias_to_existing_normalized_target_is_rejected_before_load(self) -> None:
        mapping = {
            "version": 1,
            "tables": {
                "orders": [
                    {"source": "legacy_order_id", "target": "order_id", "cast": "string"}
                ]
            },
        }
        result, output, pipeline_calls = self._run(
            "orders.csv",
            "order_id,legacy_order_id,customer_id,order_status,order_date,updated_at\n"
            "o-1,old-1,c-1,complete,2025-01-01,2025-01-01T00:00:00Z\n",
            mapping,
        )

        self._assert_rejected_before_load(result, pipeline_calls)
        self.assertIn("Migration stopped (ValueError)", output)

    def test_source_columns_that_normalize_alike_are_rejected_before_load(self) -> None:
        result, output, pipeline_calls = self._run(
            "customers.csv",
            "customerId,customer_id,created_at,country\n"
            "c-1,c-1,2025-01-01T00:00:00Z,AU\n",
        )

        self._assert_rejected_before_load(result, pipeline_calls)
        self.assertIn("Migration stopped (ValueError)", output)

    def test_raw_punctuation_source_names_are_allowed_by_mapping_v1(self) -> None:
        mapping = {
            "version": 1,
            "tables": {
                "customers": [
                    {"source": "Customer ID", "target": "customer_id", "cast": "string"}
                ]
            },
        }
        result, output, pipeline_calls = self._run(
            "customers.csv",
            " Customer ID ,created_at,country\nc-1,2025-01-01T00:00:00Z,AU\n",
            mapping,
            confirm=True,
        )

        self.assertEqual(result, 0, output)
        self.assertEqual(pipeline_calls, 1)
        self.assertEqual(self.pipeline.extract_calls, 1)
        self.assertEqual(self.pipeline.load_calls, 1)
        self.assertEqual(self.pipeline.extracted_rows[0]["customer_id"], "c-1")
        self.assertNotIn("Customer ID", self.pipeline.extracted_rows[0])

    def test_raw_sql_identifier_maps_by_exact_reflected_name(self) -> None:
        from sqlalchemy import MetaData, Table, create_engine, select

        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE "orders" ("Order Date" TEXT, "order_id" TEXT)')
            connection.exec_driver_sql('INSERT INTO "orders" VALUES (?, ?)', ("2025-01-01", "o-1"))
            table = Table("orders", MetaData(), autoload_with=connection)
            selected_columns = (table.c["Order Date"], table.c["order_id"])
            row = connection.execute(select(*selected_columns)).one()
            raw_row = dict(zip((column.name for column in selected_columns), tuple(row)))

        mapping = {"version": 1, "tables": {"orders": [{"source": "Order Date", "target": "order_date", "cast": "date"}]}}
        path = self.root / "sql-mapping.json"
        path.write_text(json.dumps(mapping), encoding="utf-8")
        rules = run_migration.load_mapping_file(path)["orders"]
        inventory = [{"table": "orders", "columns": [{"name": "Order Date"}, {"name": "order_id"}]}]

        self.assertTrue(run_migration.validate_mappings(inventory, {"orders": rules})["valid"])
        self.assertEqual(run_migration.apply_mappings(raw_row, rules)["order_date"].isoformat(), "2025-01-01")

    def test_mapping_source_control_characters_are_rejected(self) -> None:
        mapping = {"version": 1, "tables": {"orders": [{"source": "Order\nDate", "target": "order_date", "cast": "string"}]}}
        path = self.root / "unsafe-mapping.json"
        path.write_text(json.dumps(mapping), encoding="utf-8")

        with self.assertRaises(run_migration.MappingConfigurationError) as error:
            run_migration.load_mapping_file(path)
        self.assertIn("without control characters or newlines", str(error.exception))

    def test_raw_source_target_collision_with_existing_column_is_rejected(self) -> None:
        inventory = [{"table": "orders", "columns": [{"name": "Order Date"}, {"name": "order_date"}]}]
        mappings = {"orders": [{"source": "Order Date", "target": "order_date", "cast": "string"}]}

        report = run_migration.validate_mappings(inventory, mappings)

        self.assertFalse(report["valid"])
        self.assertEqual(report["errors"][0]["reason"], "target_column_already_exists")

    def test_mapping_retains_source_when_target_has_a_different_normalized_name(self) -> None:
        rules = [{"source": "Order Date", "target": "event_date", "cast": "string"}]

        mapped = run_migration.apply_mappings({"Order Date": "2025-01-01"}, rules)

        self.assertEqual(mapped["Order Date"], "2025-01-01")
        self.assertEqual(mapped["event_date"], "2025-01-01")

    def test_malformed_mapping_rule_is_rejected_before_pipeline_creation(self) -> None:
        malformed = {
            "version": 1,
            "tables": {
                "orders": [
                    {"source": "legacy_id", "target": "order_id", "cast": "string", "extra": "x"}
                ]
            },
        }
        result, output, pipeline_calls = self._run(
            "orders.csv", "order_id\no-1\n", malformed
        )

        self._assert_rejected_before_load(result, pipeline_calls, expected_pipeline_calls=0)
        self.assertIn("Each mapping rule must contain only source, target, and cast", output)

    def test_unknown_mapping_table_and_source_column_are_rejected_before_load(self) -> None:
        cases = (
            {
                "version": 1,
                "tables": {"absent_table": [{"source": "legacy_id", "target": "order_id", "cast": "string"}]},
            },
            {
                "version": 1,
                "tables": {"orders": [{"source": "absent_column", "target": "order_id", "cast": "string"}]},
            },
        )
        for mapping in cases:
            with self.subTest(mapping=mapping):
                self.pipeline = self._fake_pipeline()
                result, output, pipeline_calls = self._run(
                    "orders.csv",
                    "legacy_order_id,customer_id,order_status,order_date,updated_at\n"
                    "o-1,c-1,complete,2025-01-01,2025-01-01T00:00:00Z\n",
                    mapping,
                )

                self._assert_rejected_before_load(result, pipeline_calls)
                self.assertIn("Migration stopped (ValueError)", output)


if __name__ == "__main__":
    unittest.main()
