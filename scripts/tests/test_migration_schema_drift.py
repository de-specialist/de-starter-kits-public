"""Offline checks for field-level schema drift review and fresh-confirmation refusal."""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "migration"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import schema_drift  # noqa: E402  (pure module: no dlt/sqlalchemy dependency)
from _private_json import create_private_json  # noqa: E402

import test_migration_schema_confirmation as confirmation_fixtures  # noqa: E402

run_migration = confirmation_fixtures._load_migration_runtime(importlib.import_module)


def _csv_column(name: str) -> dict[str, str]:
    return {"name": name, "type": "CSV source field text; destination inference unverified"}


def _inventory(*tables: tuple[str, list[tuple[str, str]]]) -> list[dict[str, object]]:
    return [
        {
            "table": table,
            "status": "ready",
            "row_count": 3,
            "columns": [{"name": name, "type": type_label} for name, type_label in columns],
        }
        for table, columns in tables
    ]


def _legacy_name_fingerprint(inventory):
    """The pre-refactor schema_fingerprint body, kept to pin byte compatibility."""
    schema = []
    for item in inventory:
        if not isinstance(item.get("table"), str):
            continue
        schema.append({"table": item["table"], "columns": [
            str(column["name"]) for column in item.get("columns", [])
            if isinstance(column, dict) and isinstance(column.get("name"), str)
        ]})
    schema.sort(key=lambda item: item["table"].casefold())
    return sha256(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _record(inventory, mapping_fingerprint="a" * 64, **overrides):
    snapshot = schema_drift.schema_snapshot(inventory)
    name = schema_drift.snapshot_name_fingerprint(snapshot)
    typed = schema_drift.snapshot_typed_fingerprint(snapshot)
    record = {
        "mode": "dry_run",
        "source_schema_fingerprint": name,
        "typed_schema_fingerprint": typed,
        "source_mapping_fingerprint": mapping_fingerprint,
        "schema_confirmation_fingerprint": schema_drift.schema_confirmation_fingerprint(name, typed, mapping_fingerprint),
        "schema_confirmation_algorithm": schema_drift.SCHEMA_CONFIRMATION_ALGORITHM,
        "source_schema_snapshot": snapshot,
        "source_schema_snapshot_algorithm": schema_drift.SNAPSHOT_ALGORITHM,
    }
    record.update(overrides)
    return record


class SchemaSnapshotComparisonTests(unittest.TestCase):
    BASE = ("orders", [("order_id", "INTEGER"), ("customer_id", "VARCHAR(20)"), ("order_date", "DATE"),
                       ("total", "NUMERIC(10, 2)")])

    def _compare(self, current_columns, *, table="orders", extra_tables=()):
        baseline = schema_drift.schema_snapshot(_inventory(self.BASE))
        current = schema_drift.schema_snapshot(_inventory((table, current_columns), *extra_tables))
        return schema_drift.compare_snapshots(baseline, current)

    def test_identical_schema_reports_no_drift(self) -> None:
        report = self._compare(self.BASE[1])
        self.assertFalse(report["drift_detected"])
        self.assertEqual(report["table_changes"], [])
        self.assertFalse(report["row_values_inspected"])

    def test_added_removed_retyped_and_reordered_columns_are_reported_separately(self) -> None:
        report = self._compare([
            ("customer_id", "VARCHAR(20)"), ("order_id", "BIGINT"), ("order_date", "TIMESTAMP"), ("channel", "TEXT"),
        ])
        self.assertTrue(report["drift_detected"])
        (change,) = report["table_changes"]
        self.assertEqual(change["table"], "orders")
        self.assertEqual(change["added_columns"], [{"name": "channel", "position": 4, "type_family": "text"}])
        self.assertEqual(change["removed_columns"], [{"name": "total", "position": 4, "type_family": "numeric"}])
        self.assertEqual(change["retyped_columns"], [
            {"name": "order_date", "baseline_type_family": "date", "current_type_family": "timestamp"},
        ])
        # BIGINT and INTEGER share the integer family, so order_id is not retyped.
        self.assertEqual(len(change["reordered_columns"]), 1)
        self.assertIn(change["reordered_columns"][0]["name"], {"order_id", "customer_id"})
        self.assertEqual(report["summary"], {
            "tables_added": 0, "tables_removed": 0, "tables_changed": 1,
            "added_columns": 1, "removed_columns": 1, "reordered_columns": 1, "retyped_columns": 1,
        })

    def test_insertion_shifts_positions_without_reporting_reorder(self) -> None:
        report = self._compare([
            ("new_first", "TEXT"), ("order_id", "INTEGER"), ("customer_id", "VARCHAR(20)"),
            ("order_date", "DATE"), ("total", "NUMERIC(10, 2)"),
        ])
        (change,) = report["table_changes"]
        self.assertEqual([column["name"] for column in change["added_columns"]], ["new_first"])
        self.assertEqual(change["reordered_columns"], [])

    def test_moved_column_is_the_minimal_reorder(self) -> None:
        report = self._compare([
            ("customer_id", "VARCHAR(20)"), ("order_date", "DATE"), ("total", "NUMERIC(10, 2)"), ("order_id", "INTEGER"),
        ])
        (change,) = report["table_changes"]
        self.assertEqual(change["reordered_columns"], [
            {"name": "order_id", "baseline_position": 1, "current_position": 4},
        ])

    def test_table_changes_match_case_insensitively_and_report_added_and_removed_tables(self) -> None:
        report = self._compare(self.BASE[1], table="ORDERS", extra_tables=(("returns", [("return_id", "INTEGER")]),))
        self.assertEqual(report["table_changes"], [])
        self.assertEqual(report["tables_added"], ["returns"])
        baseline = schema_drift.schema_snapshot(_inventory(self.BASE, ("legacy", [("id", "INTEGER")])))
        current = schema_drift.schema_snapshot(_inventory(self.BASE))
        self.assertEqual(schema_drift.compare_snapshots(baseline, current)["tables_removed"], ["legacy"])

    def test_column_names_are_exact_so_case_renames_show_as_remove_and_add(self) -> None:
        report = self._compare([("Order_ID", "INTEGER"), *self.BASE[1][1:]])
        (change,) = report["table_changes"]
        self.assertEqual([c["name"] for c in change["added_columns"]], ["Order_ID"])
        self.assertEqual([c["name"] for c in change["removed_columns"]], ["order_id"])

    def test_snapshot_holds_only_schema_metadata(self) -> None:
        inventory = _inventory(self.BASE)
        inventory[0]["sample_values"] = ["secret-row-value"]
        snapshot = schema_drift.schema_snapshot(inventory)
        self.assertEqual(set(snapshot[0]), {"table", "columns"})
        self.assertTrue(all(set(column) == {"name", "type_family"} for column in snapshot[0]["columns"]))
        self.assertNotIn("secret-row-value", json.dumps(snapshot))
        self.assertNotIn("row_count", json.dumps(snapshot))

    def test_snapshot_fingerprints_are_byte_compatible_with_existing_fingerprints(self) -> None:
        inventory = _inventory(self.BASE, ("customers", [("customer_id", "TEXT")]))
        inventory.append({"table": None, "status": "no_matching_files", "columns": []})
        snapshot = schema_drift.schema_snapshot(inventory)
        self.assertEqual(schema_drift.snapshot_name_fingerprint(snapshot), _legacy_name_fingerprint(inventory))
        self.assertEqual(
            schema_drift.snapshot_typed_fingerprint(snapshot),
            sha256(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest(),
        )
        if run_migration is not None:
            self.assertEqual(run_migration.schema_fingerprint(inventory), _legacy_name_fingerprint(inventory))
            self.assertEqual(run_migration.typed_schema_fingerprint(inventory), schema_drift.snapshot_typed_fingerprint(snapshot))


class ReviewedBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "reviews"
        self.directory.mkdir(mode=0o700)
        self.inventory = _inventory(("orders", [("order_id", "INTEGER"), ("order_date", "DATE")]))

    def test_dry_run_and_manifest_records_are_accepted(self) -> None:
        self.assertEqual(schema_drift.reviewed_schema_from_record(_record(self.inventory))["kind"], "dry_run")
        manifest = _record(self.inventory, mode=None, run_id="00000000-0000-0000-0000-000000000000")
        manifest.update({
            "status": "completed",
            "source_schema_fingerprint_status": "matched_at_extraction",
            "typed_schema_fingerprint_status": "matched_at_extraction",
            "source_schema_fingerprint_observed": manifest["source_schema_fingerprint"],
            "typed_schema_fingerprint_observed": manifest["typed_schema_fingerprint"],
        })
        self.assertEqual(schema_drift.reviewed_schema_from_record(manifest)["kind"], "run_manifest")
        incomplete = dict(manifest, status="failed")
        with self.assertRaises(schema_drift.SchemaBaselineError):
            schema_drift.reviewed_schema_from_record(incomplete)

    def test_tampered_or_legacy_baselines_are_refused(self) -> None:
        tampered = _record(self.inventory)
        tampered["source_schema_snapshot"][0]["columns"].append({"name": "hidden", "type_family": "text"})
        legacy = _record(self.inventory)
        del legacy["source_schema_snapshot"], legacy["source_schema_snapshot_algorithm"]
        extra_key = _record(self.inventory)
        extra_key["source_schema_snapshot"][0]["columns"][0]["sample"] = "value"
        bad_confirmation = _record(self.inventory, schema_confirmation_fingerprint="0" * 64)
        for record in (tampered, legacy, extra_key, bad_confirmation):
            with self.subTest(record=sorted(record)), self.assertRaises(schema_drift.SchemaBaselineError):
                schema_drift.reviewed_schema_from_record(record)

    def test_baseline_file_must_be_owner_only_and_not_a_symlink(self) -> None:
        create_private_json(self.directory, "baseline.json", _record(self.inventory))
        self.assertEqual(schema_drift.load_reviewed_schema(self.directory / "baseline.json")["kind"], "dry_run")
        os.chmod(self.directory / "baseline.json", 0o644)
        with self.assertRaises(schema_drift.SchemaBaselineError):
            schema_drift.load_reviewed_schema(self.directory / "baseline.json")
        os.chmod(self.directory / "baseline.json", 0o600)
        os.symlink(self.directory / "baseline.json", self.directory / "link.json")
        with self.assertRaises(schema_drift.SchemaBaselineError):
            schema_drift.load_reviewed_schema(self.directory / "link.json")

    def test_drift_review_requires_the_current_not_the_stale_confirmation(self) -> None:
        baseline = schema_drift.reviewed_schema_from_record(_record(self.inventory))
        changed = _record(_inventory(("orders", [("order_id", "INTEGER"), ("order_date", "TIMESTAMP")])))
        current_confirmation = changed["schema_confirmation_fingerprint"]
        args = (baseline, changed["source_schema_snapshot"], "a" * 64, current_confirmation)

        stale = schema_drift.drift_review(*args, baseline["schema_confirmation_fingerprint"])
        self.assertTrue(stale["fresh_confirmation_required"])
        self.assertFalse(stale["fresh_confirmation_verified"])
        self.assertTrue(stale["stale_baseline_confirmation_supplied"])
        self.assertEqual(stale["drift"]["summary"]["retyped_columns"], 1)

        fresh = schema_drift.drift_review(*args, current_confirmation)
        self.assertTrue(fresh["fresh_confirmation_verified"])
        self.assertFalse(fresh["stale_baseline_confirmation_supplied"])

    def test_mapping_only_change_requires_fresh_confirmation_without_field_drift(self) -> None:
        baseline = schema_drift.reviewed_schema_from_record(_record(self.inventory))
        remapped = _record(self.inventory, mapping_fingerprint="b" * 64)
        review = schema_drift.drift_review(
            baseline, remapped["source_schema_snapshot"], "b" * 64, remapped["schema_confirmation_fingerprint"], None,
        )
        self.assertFalse(review["drift"]["drift_detected"])
        self.assertTrue(review["mapping_fingerprint_changed"])
        self.assertFalse(review["fresh_confirmation_verified"])

    def test_offline_cli_exit_codes(self) -> None:
        create_private_json(self.directory, "before.json", _record(self.inventory))
        create_private_json(self.directory, "same.json", _record(self.inventory))
        create_private_json(self.directory, "after.json", _record(
            _inventory(("orders", [("order_id", "INTEGER"), ("order_date", "DATE"), ("channel", "TEXT")]))
        ))
        (self.directory / "public.json").write_text(json.dumps(_record(self.inventory)), encoding="utf-8")
        os.chmod(self.directory / "public.json", 0o644)

        def run(current: str):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = schema_drift.main([
                    "--baseline", str(self.directory / "before.json"), "--current", str(self.directory / current),
                ])
            return code, stdout.getvalue(), stderr.getvalue()

        self.assertEqual(run("same.json")[0], 0)
        code, stdout, _ = run("after.json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout)["drift"]["table_changes"][0]["added_columns"][0]["name"], "channel")
        code, stdout, stderr = run("public.json")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("owner-only", stderr)


@unittest.skipIf(run_migration is None, "migration runtime dependencies are not installed")
class RunMigrationSchemaDriftTests(unittest.TestCase):
    """Drive the real CLI with the fake dlt pipeline from the schema-confirmation tests."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.csv_dir = self.root / "csv"
        self.csv_dir.mkdir()
        self.reviews = self.root / "reviews"
        self.reviews.mkdir(mode=0o700)
        self.dataset = "starterkit_migration_s"
        self.pipeline_name = "schema_fixture"
        self.pipeline_dir = self.root / ".dlt" / "pipelines" / self.pipeline_name
        self.pipeline_dir.mkdir(parents=True)
        self.database = self.pipeline_dir / f"{self.pipeline_name}.duckdb"
        self.pipeline = confirmation_fixtures.MigrationSchemaConfirmationTests._fake_pipeline(self)

    def _write_csv(self, header: str, row: str) -> None:
        (self.csv_dir / "customers.csv").write_text(f"{header}\n{row}\n", encoding="utf-8")

    def _invoke(self, *extra: str, dry_run: bool = False):
        arguments = [
            "run_migration.py", "--source", "csv", "--destination", "duckdb",
            "--dataset", self.dataset, "--pipeline-name", "fixture", "--csv-dir", str(self.csv_dir),
            "--confirm-target", f"duckdb/{self.database}/{self.dataset}", "--confirm-tables", "customers",
            *(["--dry-run"] if dry_run else []), *extra,
        ]
        previous_root, previous_argv = run_migration.MIGRATION_ROOT, sys.argv
        previous_umask = os.umask(0o077)
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            run_migration.MIGRATION_ROOT = self.root
            sys.argv = arguments
            with mock.patch.object(run_migration.dlt, "pipeline", return_value=self.pipeline), \
                 contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = run_migration.main()
        finally:
            run_migration.MIGRATION_ROOT = previous_root
            sys.argv = previous_argv
            os.umask(previous_umask)
        return result, stdout.getvalue(), stderr.getvalue()

    def _save_reviewed_dry_run(self, name: str = "reviewed.json") -> dict[str, object]:
        result, stdout, stderr = self._invoke(dry_run=True)
        self.assertEqual(result, 0, stderr)
        report = json.loads(stdout)
        create_private_json(self.reviews, name, report)
        return report

    def _assert_no_destination_work(self) -> None:
        self.assertEqual(self.pipeline.extract_calls, 0)
        self.assertEqual(self.pipeline.load_calls, 0)
        self.assertFalse((self.root / "run-manifests").exists())

    def test_dry_run_emits_snapshot_and_blocks_unconfirmed_drift(self) -> None:
        self._write_csv("customer_id,created_at,country", "c1,2025-01-01T00:00:00Z,private-country-value")
        reviewed = self._save_reviewed_dry_run()
        self.assertEqual(reviewed["source_schema_snapshot_algorithm"], schema_drift.SNAPSHOT_ALGORITHM)
        self.assertIsNone(reviewed["schema_drift_review"])
        self._write_csv("country,customer_id,created_at,segment", "AU,c1,2025-01-01T00:00:00Z,private-segment-value")

        result, stdout, stderr = self._invoke("--schema-baseline", str(self.reviews / "reviewed.json"), dry_run=True)

        self.assertEqual(result, 0, stderr)
        report = json.loads(stdout)
        self.assertIn("schema_drift_not_confirmed", report["blockers"])
        (change,) = report["schema_drift_review"]["drift"]["table_changes"]
        self.assertEqual([c["name"] for c in change["added_columns"]], ["segment"])
        self.assertEqual([c["name"] for c in change["reordered_columns"]], ["country"])
        self.assertNotIn("private-segment-value", stdout)

        result, stdout, _ = self._invoke(
            "--schema-baseline", str(self.reviews / "reviewed.json"),
            "--confirm-schema-fingerprint", report["schema_confirmation_fingerprint"], dry_run=True,
        )
        self.assertNotIn("schema_drift_not_confirmed", json.loads(stdout)["blockers"])

    def test_stale_confirmation_after_drift_is_refused_with_field_level_detail(self) -> None:
        self._write_csv("customer_id,created_at,country", "c1,2025-01-01T00:00:00Z,AU")
        reviewed = self._save_reviewed_dry_run()
        self._write_csv("customer_id,created_at,segment", "c1,2025-01-01T00:00:00Z,private-segment-value")

        result, _stdout, stderr = self._invoke(
            "--schema-baseline", str(self.reviews / "reviewed.json"),
            "--confirm-schema-fingerprint", reviewed["schema_confirmation_fingerprint"],
        )

        self.assertEqual(result, 1)
        self.assertIn("drifted from the reviewed baseline", stderr)
        detail = json.loads(stderr[:stderr.index("Migration stopped")])["schema_drift_review"]
        self.assertTrue(detail["stale_baseline_confirmation_supplied"])
        (change,) = detail["drift"]["table_changes"]
        self.assertEqual([c["name"] for c in change["added_columns"]], ["segment"])
        self.assertEqual([c["name"] for c in change["removed_columns"]], ["country"])
        self.assertNotIn("private-segment-value", stderr)
        self._assert_no_destination_work()

    def test_fresh_confirmation_after_drift_loads_and_records_review(self) -> None:
        self._write_csv("customer_id,created_at,country", "c1,2025-01-01T00:00:00Z,AU")
        self._save_reviewed_dry_run()
        self._write_csv("customer_id,country,created_at", "c1,AU,2025-01-01T00:00:00Z")
        fresh = self._save_reviewed_dry_run("fresh.json")

        result, stdout, stderr = self._invoke(
            "--schema-baseline", str(self.reviews / "reviewed.json"),
            "--confirm-schema-fingerprint", fresh["schema_confirmation_fingerprint"],
        )

        self.assertEqual(result, 0, stderr)
        self.assertIn("Migration load completed", stdout)
        (manifest_path,) = (self.root / "run-manifests").glob("*.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(manifest["schema_drift_review"]["fresh_confirmation_verified"])
        self.assertEqual(manifest["schema_drift_review"]["drift"]["summary"]["reordered_columns"], 1)
        self.assertEqual(manifest["source_schema_snapshot_algorithm"], schema_drift.SNAPSHOT_ALGORITHM)
        # The new manifest is itself a usable baseline for the next review.
        self.assertEqual(schema_drift.reviewed_schema_from_record(manifest)["kind"], "run_manifest")

    def test_unusable_baseline_is_refused_before_extract_or_load(self) -> None:
        self._write_csv("customer_id,created_at,country", "c1,2025-01-01T00:00:00Z,AU")
        reviewed = self._save_reviewed_dry_run()
        reviewed["source_schema_snapshot"][0]["columns"].pop()
        create_private_json(self.reviews, "tampered.json", reviewed)

        result, _stdout, stderr = self._invoke(
            "--schema-baseline", str(self.reviews / "tampered.json"),
            "--confirm-schema-fingerprint", reviewed["schema_confirmation_fingerprint"],
        )

        self.assertEqual(result, 1)
        self.assertIn("--schema-baseline must be an owner-only", stderr)
        self._assert_no_destination_work()


if __name__ == "__main__":
    unittest.main()
