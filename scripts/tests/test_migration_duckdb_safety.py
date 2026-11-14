"""Offline regression checks for local DuckDB migration target guards."""

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

from scripts.tests._dlt_isolated_runtime import isolated_environment, prepare_runtime, run_python


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


class MigrationRuntimeImportPolicyTests(unittest.TestCase):
    def test_internal_import_error_is_never_skipped(self) -> None:
        def broken_internal_import(_name: str):
            raise ModuleNotFoundError("missing migration helper", name="migration._broken_helper")

        with self.assertRaisesRegex(ModuleNotFoundError, "missing migration helper"):
            _load_migration_runtime(broken_internal_import, ci=False)

    def test_missing_optional_package_can_skip_only_outside_ci(self) -> None:
        def missing_dlt(_name: str):
            raise ModuleNotFoundError("missing dlt", name="dlt")

        self.assertIsNone(_load_migration_runtime(missing_dlt, ci=False))
        with self.assertRaisesRegex(ModuleNotFoundError, "missing dlt"):
            _load_migration_runtime(missing_dlt, ci=True)


@unittest.skipIf(run_migration is None, "migration runtime dependencies are not installed")
class DuckDBMigrationSafetyTests(unittest.TestCase):
    def _pipeline(self, root: Path, dataset: str, pipeline_name: str, database: Path | None = None):
        pipeline_dir = root / ".dlt" / "pipelines" / pipeline_name
        pipeline_dir.mkdir(parents=True, exist_ok=True)
        database = database or pipeline_dir / f"{pipeline_name}.duckdb"

        class DestinationConfig:
            local_dir = str(root)
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
                self.client = client

            def destination_client(self):
                return self.client

            def extract(self, *_args, **_kwargs):
                self.extract_calls += 1

            def load(self, *_args, **_kwargs):
                self.load_calls += 1

        return FakePipeline(), database

    def test_wrong_target_confirmation_stops_before_any_write_and_preserves_unrelated_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = "starterkit_migration_x"
            source = root / "csv"
            source.mkdir()
            (source / "customers.csv").write_text(
                "customer_id,created_at,country\n1,2025-01-01T00:00:00Z,AU\n",
                encoding="utf-8",
            )
            unrelated = root / "unrelated.keep"
            unrelated.write_bytes(b"preserve this file")
            pipeline_name = run_migration._pipeline_name(
                SimpleNamespace(pipeline_name="fixture", destination="duckdb"),
                dataset,
                f"duckdb/{dataset}",
            )
            pipeline, database = self._pipeline(root, dataset, pipeline_name)
            database.write_bytes(b"preexisting DuckDB target")
            previous_root = run_migration.MIGRATION_ROOT
            previous_argv = sys.argv
            previous_umask = os.umask(0o077)
            stderr = io.StringIO()
            try:
                run_migration.MIGRATION_ROOT = root
                sys.argv = [
                    "run_migration.py", "--source", "csv", "--destination", "duckdb",
                    "--dataset", dataset, "--pipeline-name", "fixture", "--csv-dir", str(source),
                    "--confirm-target", "wrong-target",
                ]
                with mock.patch.object(run_migration.dlt, "pipeline", return_value=pipeline), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                    result = run_migration.main()
            finally:
                run_migration.MIGRATION_ROOT = previous_root
                sys.argv = previous_argv
                os.umask(previous_umask)

            self.assertEqual(result, 1)
            self.assertIn("--confirm-target does not match the resolved target", stderr.getvalue())
            self.assertEqual(pipeline.extract_calls, 0)
            self.assertEqual(pipeline.load_calls, 0)
            self.assertEqual(database.read_bytes(), b"preexisting DuckDB target")
            self.assertEqual(unrelated.read_bytes(), b"preserve this file")
            self.assertFalse((root / "run-manifests").exists())

    def test_wrong_table_confirmation_stops_before_extract_and_preserves_sibling_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = "starterkit_migration_x"
            source = root / "csv"
            source.mkdir()
            (source / "customers.csv").write_text(
                "customer_id,created_at,country\n1,2025-01-01T00:00:00Z,AU\n",
                encoding="utf-8",
            )
            pipeline_name = run_migration._pipeline_name(
                SimpleNamespace(pipeline_name="fixture", destination="duckdb"),
                dataset,
                f"duckdb/{dataset}",
            )
            pipeline, database = self._pipeline(root, dataset, pipeline_name)
            database.write_bytes(b"preexisting DuckDB target")
            unrelated = database.parent / "unrelated.duckdb"
            unrelated.write_bytes(b"unrelated database content")
            exact_target = f"duckdb/{database}/{dataset}"
            inventory = run_migration.inventory_csv_inputs(source, "*.csv", None, count_rows=False)
            schema_confirmation = run_migration.schema_confirmation_fingerprint(
                run_migration.schema_fingerprint(inventory),
                run_migration.typed_schema_fingerprint(inventory),
                run_migration.mappings_fingerprint({}),
            )
            previous_root = run_migration.MIGRATION_ROOT
            previous_argv = sys.argv
            previous_umask = os.umask(0o077)
            stderr = io.StringIO()
            try:
                run_migration.MIGRATION_ROOT = root
                sys.argv = [
                    "run_migration.py", "--source", "csv", "--destination", "duckdb",
                    "--dataset", dataset, "--pipeline-name", "fixture", "--csv-dir", str(source),
                    "--confirm-target", exact_target, "--confirm-tables", "wrong_table",
                    "--confirm-schema-fingerprint", schema_confirmation,
                ]
                with mock.patch.object(run_migration.dlt, "pipeline", return_value=pipeline), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                    result = run_migration.main()
            finally:
                run_migration.MIGRATION_ROOT = previous_root
                sys.argv = previous_argv
                os.umask(previous_umask)

            self.assertEqual(result, 1)
            self.assertIn("--confirm-tables does not match the resolved destination tables", stderr.getvalue())
            self.assertEqual(pipeline.extract_calls, 0)
            self.assertEqual(pipeline.load_calls, 0)
            self.assertEqual(database.read_bytes(), b"preexisting DuckDB target")
            self.assertEqual(unrelated.read_bytes(), b"unrelated database content")
            self.assertFalse((root / "run-manifests").exists())

    def test_resolver_rejects_database_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = "starterkit_migration_test"
            pipeline_name = "short_pipeline"
            outside = root / "outside" / f"{pipeline_name}.duckdb"
            pipeline, _ = self._pipeline(root, dataset, pipeline_name, outside)

            with self.assertRaisesRegex(ValueError, "default"):
                run_migration._resolve_dlt_destination(pipeline, "duckdb", None, dataset)

            self.assertFalse(outside.exists())

    def test_duckdb_pipeline_credential_resolves_inside_pipeline_working_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = "starterkit_migration_test"
            pipeline_name = "safety_fixture"
            pipelines_root = root / ".dlt" / "pipelines"
            pipeline = run_migration.dlt.pipeline(
                pipeline_name=pipeline_name,
                pipelines_dir=str(pipelines_root),
                destination=run_migration._dlt_destination("duckdb"),
                dataset_name=dataset,
                dev_mode=False,
            )

            database = Path(pipeline.destination_client().config.credentials.database).resolve()
            working_dir = Path(pipeline.working_dir).resolve()
            confined_root = pipelines_root.resolve()

            self.assertEqual(database.parent, working_dir)
            self.assertEqual(database.name, f"{pipeline_name}.duckdb")
            self.assertTrue(database.is_relative_to(confined_root))

    def test_generated_pipeline_name_preserves_valid_name_and_compacts_oversized_name(self) -> None:
        args = SimpleNamespace(pipeline_name="a" * 30, destination="duckdb")
        accepted = run_migration._pipeline_name(args, "d", "duckdb/d")
        self.assertEqual(len(accepted), 64)
        self.assertEqual(accepted, f"{'a' * 30}_duckdb_d_{run_migration.sha256('duckdb/d'.encode('utf-8')).hexdigest()[:24]}")

        args.pipeline_name += "a"
        compacted = run_migration._pipeline_name(args, "d", "duckdb/d")
        self.assertLessEqual(len(compacted), 64)
        self.assertTrue(compacted.startswith(f"{args.pipeline_name}_duckdb_c"))
        self.assertEqual(compacted, run_migration._pipeline_name(args, "d", "duckdb/d"))
        self.assertNotEqual(compacted, run_migration._pipeline_name(args, "e", "duckdb/e"))

    def test_oversized_default_name_compacts_without_changing_default_prefix(self) -> None:
        args = SimpleNamespace(pipeline_name="starterkit_migration", destination="snowflake")

        name = run_migration._pipeline_name(args, "starterkit_migration_bronze", "ACCOUNT/DATABASE/starterkit_migration_bronze")

        self.assertLessEqual(len(name), 64)
        self.assertTrue(name.startswith("starterkit_migration_snowflake_c"))

    def test_case_variants_of_same_effective_target_resolve_to_same_pipeline_name(self) -> None:
        args = SimpleNamespace(pipeline_name="fixture", destination="bigquery")
        lowercase = run_migration._pipeline_name(
            args, "starterkit_migration_sales", "example-project/starterkit_migration_sales",
        )
        mixed_case = run_migration._pipeline_name(
            args, "STARTERKIT_MIGRATION_SALES", "EXAMPLE-PROJECT/STARTERKIT_MIGRATION_SALES",
        )

        self.assertEqual(mixed_case, lowercase)

    def test_dry_run_reports_overlong_generated_name_before_config_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "csv"
            source.mkdir()
            (source / "customers.csv").write_text(
                "customer_id,created_at,country\nc1,2025-01-01T00:00:00Z,AU\n",
                encoding="utf-8",
            )
            previous_root = run_migration.MIGRATION_ROOT
            previous_argv = sys.argv
            previous_umask = os.umask(0o077)
            output = io.StringIO()
            pipeline_factory = mock.patch.object(run_migration.dlt, "pipeline")
            try:
                run_migration.MIGRATION_ROOT = root
                sys.argv = [
                    "run_migration.py", "--source", "csv", "--destination", "duckdb",
                    "--dataset", "starterkit_migration_limit", "--pipeline-name", "p" * 40,
                    "--csv-dir", str(source), "--dry-run",
                ]
                with pipeline_factory as factory, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    result = run_migration.main()
            finally:
                run_migration.MIGRATION_ROOT = previous_root
                sys.argv = previous_argv
                os.umask(previous_umask)

            self.assertEqual(result, 1)
            self.assertIn("even after deterministic compaction", output.getvalue())
            self.assertNotIn('"target_identity": null', output.getvalue())
            factory.assert_not_called()

    def test_reconciler_attaches_with_pipeline_local_duckdb_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = prepare_runtime(root)
            env = isolated_environment(runtime, root)
            probe = """
import json
from pathlib import Path
import run_migration
import reconcile_bronze
dataset = 'starterkit_migration_attach'
pipeline_name = 'attach_fixture'
pipelines_root = Path(run_migration.MIGRATION_ROOT) / '.dlt' / 'pipelines'
original = run_migration.dlt.pipeline(
    pipeline_name=pipeline_name,
    pipelines_dir=str(pipelines_root),
    destination=run_migration._dlt_destination('duckdb'),
    dataset_name=dataset,
    dev_mode=False,
)
expected = run_migration._resolve_dlt_destination(original, 'duckdb', None, dataset)[2]
attached = reconcile_bronze._attach_pipeline(pipeline_name, 'duckdb', dataset, pipelines_root)
actual = run_migration._resolve_dlt_destination(attached, 'duckdb', None, dataset)[2]
print(json.dumps({'expected': expected, 'actual': actual}))
"""
            result = run_python(runtime, probe, env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["actual"], json.loads(result.stdout)["expected"])

    def test_reconciler_accepts_compacted_default_and_legacy_pipeline_names(self) -> None:
        import importlib

        reconcile_bronze = importlib.import_module("migration.reconcile_bronze")
        namespace = "starterkit_migration_bronze"
        compact_name = run_migration._pipeline_name(
            SimpleNamespace(pipeline_name="starterkit_migration", destination="duckdb"),
            namespace,
            f"duckdb/{namespace}",
        )
        self.assertIn("_duckdb_c", compact_name)
        common = {
            "destination": "duckdb",
            "bronze_namespace": namespace,
            "destination_tables": ["orders"],
            "entities": ["orders"],
        }
        compact_manifest = {
            **common,
            "pipeline_name": compact_name,
            "target_identity": f"duckdb//tmp/{compact_name}/{compact_name}.duckdb/{namespace}",
        }
        reconcile_bronze._validate_manifest_target(compact_manifest)

        legacy_namespace = "starterkit_migration_short"
        legacy_target_hint = f"duckdb/{legacy_namespace}"
        legacy_hash = run_migration.sha256(legacy_target_hint.encode("utf-8")).hexdigest()[:24]
        legacy_name = f"oldprefix_duckdb_{legacy_namespace}_{legacy_hash}"
        legacy_manifest = {
            **common,
            "bronze_namespace": legacy_namespace,
            "pipeline_name": legacy_name,
            "target_identity": f"duckdb//tmp/{legacy_name}/{legacy_name}.duckdb/{legacy_namespace}",
        }
        reconcile_bronze._validate_manifest_target(legacy_manifest)

        tampered_manifest = {**compact_manifest, "pipeline_name": compact_name[:-1] + ("0" if compact_name[-1] != "0" else "1")}
        with self.assertRaisesRegex(ValueError, "hash does not match"):
            reconcile_bronze._validate_manifest_target(tampered_manifest)

    def test_resolver_rejects_symlink_database_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = "starterkit_migration_test"
            pipeline_name = "short_pipeline"
            pipeline_dir = root / ".dlt" / "pipelines" / pipeline_name
            pipeline_dir.mkdir(parents=True)
            database = pipeline_dir / f"{pipeline_name}.duckdb"
            unrelated = root / "unrelated.duckdb"
            unrelated.write_bytes(b"untouched")
            database.symlink_to(unrelated)
            pipeline, _ = self._pipeline(root, dataset, pipeline_name, database)

            with self.assertRaisesRegex(ValueError, "symlinks"):
                run_migration._resolve_dlt_destination(pipeline, "duckdb", None, dataset)

            self.assertTrue(database.is_symlink())
            self.assertEqual(unrelated.read_bytes(), b"untouched")

    def test_dry_run_blocks_table_names_colliding_after_dlt_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "csv"
            source.mkdir()
            (source / "foo_bar.csv").write_text("id\n1\n", encoding="utf-8")
            (source / "foo__bar.csv").write_text("id\n2\n", encoding="utf-8")
            client = SimpleNamespace(
                schema=SimpleNamespace(naming=run_migration.SnakeCaseNamingConvention()),
                capabilities=SimpleNamespace(casefold_identifier=str.casefold),
            )

            class DryPipeline:
                def destination_client(self):
                    return client

            previous_argv = sys.argv
            sys.argv = [
                "run_migration.py", "--source", "csv", "--destination", "bigquery",
                "--dataset", "starterkit_migration_col", "--csv-dir", str(source),
                "--dry-run", "--confirm-tables", "foo_bar,foo_bar",
            ]
            output = io.StringIO()
            try:
                with mock.patch.object(run_migration.dlt, "pipeline", return_value=DryPipeline()), \
                     contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                    result = run_migration.main()
            finally:
                sys.argv = previous_argv

            report = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(report["destination_tables"], ["foo_bar", "foo_bar"])
            self.assertTrue(report["table_confirmation_verified"])
            self.assertIn("normalized_destination_table_name_collision", report["blockers"])
            self.assertFalse(report["preflight_ready"])


if __name__ == "__main__":
    unittest.main()
