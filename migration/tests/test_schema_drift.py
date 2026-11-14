"""Offline tests for reviewed schema snapshots and private baseline files."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _private_json import create_private_json, read_private_json, update_private_json
from schema_drift import (
    SCHEMA_CONFIRMATION_ALGORITHM,
    SNAPSHOT_ALGORITHM,
    SchemaBaselineError,
    compare_snapshots,
    drift_review,
    load_reviewed_schema,
    reviewed_schema_from_record,
    schema_confirmation_fingerprint,
    schema_snapshot,
    snapshot_name_fingerprint,
    snapshot_typed_fingerprint,
    validate_snapshot,
)

try:
    from sqlalchemy.sql.elements import quoted_name
except ModuleNotFoundError as error:
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name != "sqlalchemy":
        raise
    quoted_name = None

_MIGRATION_DIR = Path(__file__).resolve().parents[1]
_SCRIPT = _MIGRATION_DIR / "schema_drift.py"
_SENTINEL = "PRIVATE_SENTINEL_9c41"
_LONE = "\ud800"


class _Str(str):
    pass


class _Dict(dict):
    pass


class _List(list):
    pass


def _record(snapshot: list[dict[str, object]], *, mode: str = "dry_run") -> dict[str, object]:
    names = snapshot_name_fingerprint(snapshot)
    typed = snapshot_typed_fingerprint(snapshot)
    mapping = "a" * 64
    return {
        "mode": mode,
        "run_id": "123e4567-e89b-42d3-a456-426614174000" if mode != "dry_run" else None,
        "source_schema_snapshot_algorithm": SNAPSHOT_ALGORITHM,
        "schema_confirmation_algorithm": SCHEMA_CONFIRMATION_ALGORITHM,
        "source_schema_snapshot": snapshot,
        "source_schema_fingerprint": names,
        "typed_schema_fingerprint": typed,
        "source_mapping_fingerprint": mapping,
        "schema_confirmation_fingerprint": schema_confirmation_fingerprint(names, typed, mapping),
    }


class SchemaDriftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = schema_snapshot([
            {"table": "orders", "columns": [
                {"name": "order_id", "type": "INTEGER"},
                {"name": "amount", "type": "NUMERIC(12,2)"},
            ]},
        ])

    def test_snapshot_normalizes_types_and_sorts_tables(self) -> None:
        result = schema_snapshot([
            {"table": "z", "columns": [{"name": "flag", "type": "BOOLEAN"}]},
            {"table": "a", "columns": [{"name": "id", "type": "BIGINT"}]},
        ])
        self.assertEqual([row["table"] for row in result], ["a", "z"])
        self.assertEqual(result[0]["columns"][0]["type_family"], "integer")
        self.assertEqual(result[1]["columns"][0]["type_family"], "boolean")

    def test_compare_reports_tables_and_each_column_change(self) -> None:
        current = [
            {"table": "orders", "columns": [
                {"name": "amount", "type_family": "text"},
                {"name": "order_id", "type_family": "integer"},
                {"name": "status", "type_family": "text"},
            ]},
            {"table": "new_table", "columns": []},
        ]
        report = compare_snapshots(self.snapshot, current)
        self.assertEqual(report["tables_added"], ["new_table"])
        self.assertEqual(report["table_changes"][0]["added_columns"][0]["name"], "status")
        self.assertEqual(report["table_changes"][0]["retyped_columns"][0]["name"], "amount")
        self.assertEqual(report["table_changes"][0]["reordered_columns"][0]["name"], "order_id")
        self.assertTrue(report["drift_detected"])

    def test_rejects_malformed_and_duplicate_tables(self) -> None:
        for value in (None, {}, [{"table": "x", "columns": [], "extra": 1}],
                      [{"table": "x", "columns": []}, {"table": "X", "columns": []}]):
            with self.subTest(value=value), self.assertRaises(SchemaBaselineError):
                validate_snapshot(value)

    def test_record_fingerprints_are_verified(self) -> None:
        record = _record(self.snapshot)
        self.assertEqual(reviewed_schema_from_record(record)["snapshot"], self.snapshot)
        record["source_schema_snapshot"][0]["columns"][0]["name"] = "tampered"
        with self.assertRaises(SchemaBaselineError):
            reviewed_schema_from_record(record)

    def test_only_completed_schema_matched_run_can_be_a_baseline(self) -> None:
        completed = _record(self.snapshot, mode="load")
        completed.update({
            "status": "completed",
            "source_schema_fingerprint_status": "matched_at_extraction",
            "typed_schema_fingerprint_status": "matched_at_extraction",
            "source_schema_fingerprint_observed": completed["source_schema_fingerprint"],
            "typed_schema_fingerprint_observed": completed["typed_schema_fingerprint"],
        })
        self.assertEqual(reviewed_schema_from_record(completed)["kind"], "run_manifest")
        for field, value in (
            ("status", "failed"),
            ("status", "running"),
            ("source_schema_fingerprint_status", "mismatch"),
            ("source_schema_fingerprint_observed", "e" * 64),
            ("typed_schema_fingerprint_observed", "d" * 64),
            ("typed_schema_fingerprint_status", None),
        ):
            candidate = dict(completed)
            candidate[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(SchemaBaselineError):
                reviewed_schema_from_record(candidate)

    def test_current_confirmation_required_after_schema_or_mapping_change(self) -> None:
        baseline = reviewed_schema_from_record(_record(self.snapshot))
        changed = [*self.snapshot]
        current_fingerprint = "b" * 64
        without_confirmation = drift_review(baseline, changed, "c" * 64, current_fingerprint, None)
        self.assertTrue(without_confirmation["fresh_confirmation_required"])
        self.assertFalse(without_confirmation["fresh_confirmation_verified"])
        with_confirmation = drift_review(baseline, changed, "c" * 64, current_fingerprint, current_fingerprint)
        self.assertTrue(with_confirmation["fresh_confirmation_verified"])

    @unittest.skipUnless(os.name == "posix", "owner-only file checks require POSIX")
    def test_private_json_round_trip_and_symlink_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / "private"
            private.mkdir(mode=0o700)
            os.chmod(private, 0o700)
            create_private_json(private, "baseline.json", _record(self.snapshot))
            self.assertEqual(load_reviewed_schema(private / "baseline.json")["snapshot"], self.snapshot)
            self.assertEqual(stat.S_IMODE((private / "baseline.json").stat().st_mode), 0o600)

            target_parent = root / "actual" / "nested"
            target_parent.mkdir(parents=True, mode=0o700)
            os.chmod(root / "actual", 0o700)
            os.chmod(target_parent, 0o700)
            create_private_json(target_parent, "baseline.json", _record(self.snapshot))
            link = root / "link"
            link.symlink_to(root / "actual", target_is_directory=True)
            linked_parent = link / "nested"
            with self.assertRaises(SchemaBaselineError):
                load_reviewed_schema(linked_parent / "baseline.json")
            with self.assertRaises(OSError):
                read_private_json(linked_parent, "baseline.json")
            with self.assertRaises(OSError):
                create_private_json(linked_parent, "new.json", _record(self.snapshot))
            with self.assertRaises(OSError):
                update_private_json(linked_parent, "baseline.json", _record(self.snapshot))

    def _assert_fixed_error(self, context) -> None:
        error = context.exception
        for text in (str(error), repr(error)):
            self.assertNotIn(_SENTINEL, text)
            self.assertNotIn(_LONE, text)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_lone_surrogate_names_are_rejected_without_echoing_them(self) -> None:
        bad = f"{_SENTINEL}{_LONE}"
        variants = (
            [{"table": bad, "columns": []}],
            [{"table": "orders", "columns": [{"name": bad, "type_family": "text"}]}],
            [{"table": "orders", "columns": [{"name": "id", "type_family": bad}]}],
        )
        for snapshot in variants:
            with self.subTest(snapshot=repr(snapshot)):
                with self.assertRaises(SchemaBaselineError) as context:
                    validate_snapshot(snapshot)
                self._assert_fixed_error(context)
                record = _record(self.snapshot)
                record["source_schema_snapshot"] = snapshot
                with self.assertRaises(SchemaBaselineError) as context:
                    reviewed_schema_from_record(record)
                self._assert_fixed_error(context)

    def test_fingerprinting_unencodable_text_raises_fixed_error(self) -> None:
        snapshot = [{"table": "orders", "columns": [{"name": f"{_SENTINEL}{_LONE}", "type_family": "text"}]}]
        for fingerprint in (snapshot_name_fingerprint, snapshot_typed_fingerprint):
            with self.subTest(fingerprint=fingerprint.__name__):
                with self.assertRaises(SchemaBaselineError) as context:
                    fingerprint(snapshot)
                self._assert_fixed_error(context)

    def test_rejects_container_and_text_subclasses(self) -> None:
        column = {"name": "id", "type_family": "integer"}
        variants = (
            _List([{"table": "orders", "columns": [column]}]),
            [_Dict({"table": "orders", "columns": [column]})],
            [{"table": "orders", "columns": _List([column])}],
            [{"table": "orders", "columns": [_Dict(column)]}],
            [{"table": _Str("orders"), "columns": [column]}],
            [{"table": "orders", "columns": [{"name": _Str("id"), "type_family": "integer"}]}],
            [{"table": "orders", "columns": [{"name": "id", "type_family": _Str("integer")}]}],
        )
        for snapshot in variants:
            with self.subTest(snapshot=repr(snapshot)), self.assertRaises(SchemaBaselineError):
                validate_snapshot(snapshot)
        validate_snapshot([{"table": "orders", "columns": [dict(column)]}])

    @unittest.skipIf(quoted_name is None, "sqlalchemy is not installed")
    def test_live_snapshot_converts_reflected_names_to_exact_str(self) -> None:
        inventory = [
            {"table": quoted_name("orders", None), "columns": [
                {"name": quoted_name("order_id", None), "type": "INTEGER"},
                {"name": "amount", "type": "NUMERIC(12,2)"},
                {"name": "note", "type": "TEXT"},
            ]},
            {"table": "\u00dcn\u00efcode_\u8868", "columns": [{"name": "\U0001f600", "type": "BIGINT"}]},
        ]
        snapshot = schema_snapshot(inventory)
        for entry in snapshot:
            self.assertIs(type(entry["table"]), str)
            for column in entry["columns"]:
                self.assertIs(type(column["name"]), str)
                self.assertIs(type(column["type_family"]), str)
        self.assertEqual(validate_snapshot(snapshot), snapshot)
        # Pinned from the pre-change schema_drift.py on this inventory:
        # converting names to exact str must not change any fingerprint.
        self.assertEqual(
            snapshot_name_fingerprint(snapshot),
            "34f0ed07ada8ef2fcb1900319a4e6734a4088e3714fdd3bff1ab786fb1657e4f",
        )
        self.assertEqual(
            snapshot_typed_fingerprint(snapshot),
            "6f7e62a6e16eb0d2c6c49e70c86f7c699028aece385a07857945328154d6e6b3",
        )
        baseline = reviewed_schema_from_record(_record(snapshot))
        review = drift_review(
            baseline, schema_snapshot(inventory), baseline["source_mapping_fingerprint"],
            baseline["schema_confirmation_fingerprint"], None,
        )
        self.assertFalse(review["drift"]["drift_detected"])
        self.assertTrue(review["fresh_confirmation_verified"])

    @unittest.skipIf(quoted_name is None, "sqlalchemy is not installed")
    @unittest.skipUnless(os.name == "posix", "owner-only file checks require POSIX")
    def test_sqlite_dry_run_accepts_its_own_dry_run_as_schema_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary) / "private"
            private.mkdir(mode=0o700)
            os.chmod(private, 0o700)
            database = private / "demo.sqlite"
            created = subprocess.run(
                [sys.executable, str(_MIGRATION_DIR / "create_sqlite_demo.py"), str(database)],
                capture_output=True, text=True, timeout=60, check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            env = {**os.environ, "MIGRATION_SOURCE_URL": f"sqlite:///{database}"}
            command = [
                sys.executable, str(_MIGRATION_DIR / "run_migration.py"),
                "--source", "sql", "--source-schema", "main", "--tables", "orders",
                "--destination", "duckdb", "--dry-run",
            ]
            first = subprocess.run(
                command, capture_output=True, text=True, timeout=120, check=False, cwd=temporary, env=env,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            descriptor = os.open(private / "baseline.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(first.stdout)
            second = subprocess.run(
                [*command, "--schema-baseline", str(private / "baseline.json")],
                capture_output=True, text=True, timeout=120, check=False, cwd=temporary, env=env,
            )
        self.assertEqual(second.returncode, 0, second.stderr)
        review = json.loads(second.stdout)["schema_drift_review"]
        self.assertIsNotNone(review)
        self.assertFalse(review["drift"]["drift_detected"])
        self.assertFalse(review["schema_or_mapping_changed"])

    @unittest.skipUnless(os.name == "posix", "owner-only file checks require POSIX")
    def test_cli_rejects_surrogate_baseline_without_echoing_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary) / "private"
            private.mkdir(mode=0o700)
            os.chmod(private, 0o700)
            create_private_json(private, "current.json", _record(self.snapshot))
            record = _record(self.snapshot)
            record["source_schema_snapshot"] = [{"table": "orders", "columns": [
                {"name": "SENTINEL_PLACEHOLDER", "type_family": "text"},
            ]}]
            # Write the lone surrogate as a JSON escape, exactly as a hand-edited file would hold it.
            text = json.dumps(record).replace("SENTINEL_PLACEHOLDER", f"{_SENTINEL}\\ud800")
            baseline = private / "baseline.json"
            descriptor = os.open(baseline, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(text)
            os.chmod(baseline, 0o600)
            result = subprocess.run(
                [sys.executable, str(_SCRIPT), "--baseline", str(baseline), "--current", str(private / "current.json")],
                capture_output=True, text=True, errors="backslashreplace", timeout=60, check=False,
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        for stream in (result.stdout, result.stderr):
            self.assertNotIn(_SENTINEL, stream)
            self.assertNotIn("ud800", stream)
            self.assertNotIn("Traceback", stream)
        self.assertIn("Schema drift review stopped:", result.stderr)

    def test_rejects_parent_directory_components(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / "private"
            private.mkdir(mode=0o700)
            os.chmod(private, 0o700)
            create_private_json(private, "baseline.json", _record(self.snapshot))
            with self.assertRaises(ValueError):
                read_private_json(private / ".." / "private", "baseline.json")


if __name__ == "__main__":
    unittest.main()
