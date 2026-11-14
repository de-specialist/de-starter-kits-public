"""Offline tests for the optional --source-binding-id operator alias.

All fixtures are synthetic. Dry runs use a disposable local DuckDB configuration that never
opens a destination connection; run-manifest tests replace the dlt pipeline and destination
resolution with in-process fakes, so nothing contacts BigQuery, Snowflake, or any server.
"""

from __future__ import annotations

import argparse
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
sys.path.insert(0, str(ROOT / "migration"))
try:
    run_migration = importlib.import_module("run_migration")
    from _private_json import read_private_json
except ModuleNotFoundError as error:
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name not in {"dlt", "sqlalchemy"}:
        raise
    run_migration = None


DATASET = "starterkit_migration_binding_test"
FAKE_IDENTITY = f"duckdb/fake-local-runtime/{DATASET}"
SYNTHETIC_CSV = "customer_id,created_at,country\nSYNTH_C1,2025-01-01T00:00:00Z,AU\nSYNTH_C2,2025-01-02T00:00:00Z,NZ\n"
VALID_ALIAS = "synthetic_retail_v1"
# Every rejected value carries "canary" so tests can prove no part of it reaches stderr.
INVALID_ALIASES = (
    "Canary_upper",
    "9canary_digit_first",
    "_canary_underscore_first",
    "canary-hyphen",
    "canary space",
    "canary.dot",
    "canary\n",
    "canary\x00nul",
    "canaryé_non_ascii",
    "canary_" + "x" * 34,  # 41 characters
    "postgresql://canary:secret@host/db",
)


def _base_argv(csv_dir: Path) -> list[str]:
    return [
        "--source", "csv", "--destination", "duckdb", "--dataset", DATASET,
        "--csv-dir", str(csv_dir),
    ]


class _FakePipeline:
    """Minimal in-process stand-in for the dlt pipeline used by a real (non-dry) run."""

    has_pending_data = False

    def __init__(self) -> None:
        self.row_counts: dict[str, int] = {}
        self.last_trace = None

    def extract(self, data, workers: int = 1) -> None:
        for resource in data:
            self.row_counts[resource.name] = sum(1 for _ in resource)

    def normalize(self, workers: int = 1) -> None:
        self.last_trace = SimpleNamespace(last_normalize_info=SimpleNamespace(row_counts=dict(self.row_counts)))

    def load(self, raise_on_failed_jobs: bool = True):
        package = SimpleNamespace(jobs={"completed_jobs": [object()], "failed_jobs": []})
        return SimpleNamespace(
            loads_ids=["1700000000.000001"],
            load_packages=[package],
            raise_on_failed_jobs=lambda: None,
        )


def _fake_client():
    naming = run_migration.SnakeCaseNamingConvention()
    return SimpleNamespace(
        schema=SimpleNamespace(naming=naming),
        capabilities=SimpleNamespace(casefold_identifier=lambda value: value),
    )


@unittest.skipIf(run_migration is None, "migration runtime dependencies are not installed")
class SourceBindingIdParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = run_migration.build_parser()
        self.argv = _base_argv(Path("synthetic_csv"))

    def _parse_error(self, value: str) -> str:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            self.parser.parse_args([*self.argv, "--source-binding-id", value])
        self.assertEqual(raised.exception.code, 2)
        return stderr.getvalue()

    def test_valid_aliases_are_returned_exactly(self) -> None:
        for value in ("a", "z9", VALID_ALIAS, "a" + "0" * 39, "a" + "_" * 39):
            with self.subTest(value=value):
                args = self.parser.parse_args([*self.argv, "--source-binding-id", value])
                self.assertEqual(args.source_binding_id, value)

    def test_equals_form_is_accepted(self) -> None:
        args = self.parser.parse_args([*self.argv, f"--source-binding-id={VALID_ALIAS}"])
        self.assertEqual(args.source_binding_id, VALID_ALIAS)

    def test_invalid_aliases_are_rejected_with_fixed_value_free_error(self) -> None:
        expected_line = f"argument --source-binding-id: {run_migration._SOURCE_BINDING_ID_ERROR}"
        messages = set()
        for value in (*INVALID_ALIASES, ""):
            with self.subTest(value=value):
                stderr = self._parse_error(value)
                self.assertIn(expected_line, stderr)
                self.assertNotIn("canary", stderr.casefold())
                self.assertNotIn("secret", stderr.casefold())
                self.assertNotIn(repr(value), stderr)
                messages.add(stderr)
        # Every rejection produces byte-identical output, so nothing value-dependent is shown.
        self.assertEqual(len(messages), 1)

    def test_validator_uses_fullmatch_and_a_constant_message(self) -> None:
        for value in (*INVALID_ALIASES, ""):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError) as raised:
                    run_migration.source_binding_id(value)
                self.assertEqual(str(raised.exception), run_migration._SOURCE_BINDING_ID_ERROR)
                self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(run_migration.source_binding_id("a" + "1" * 39), "a" + "1" * 39)
        with self.assertRaises(argparse.ArgumentTypeError):
            run_migration.source_binding_id("a" + "1" * 40)
        self.assertNotIn("{", run_migration._SOURCE_BINDING_ID_ERROR)
        self.assertNotIn("%", run_migration._SOURCE_BINDING_ID_ERROR)

    def test_omitted_alias_is_none_and_not_read_from_environment(self) -> None:
        hostile_env = {
            "SOURCE_BINDING_ID": "env_alias",
            "MIGRATION_SOURCE_BINDING_ID": "env_alias",
            "SOURCE_BINDING__ID": "env_alias",
        }
        with mock.patch.dict(os.environ, hostile_env):
            args = self.parser.parse_args(self.argv)
        self.assertIsNone(args.source_binding_id)


@unittest.skipIf(run_migration is None, "migration runtime dependencies are not installed")
class SourceBindingIdEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="source-binding-test-")
        self.addCleanup(self._temp.cleanup)
        # Resolve so the loader's symlink-component guard sees a real path (e.g. macOS /tmp).
        self.root = Path(self._temp.name).resolve()
        self.csv_dir = self.root / "csv"
        self.csv_dir.mkdir()
        (self.csv_dir / "customers.csv").write_text(SYNTHETIC_CSV, encoding="utf-8")
        self.migration_root = self.root / "migration_root"
        self.migration_root.mkdir(mode=0o700)
        previous_umask = os.umask(0o022)
        os.umask(previous_umask)
        self.addCleanup(os.umask, previous_umask)

    def _main(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["run_migration.py", *argv]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = run_migration.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def _dry_run(self, *extra: str) -> dict[str, object]:
        code, stdout, stderr = self._main([*_base_argv(self.csv_dir), "--dry-run", *extra])
        self.assertEqual(code, 0, stderr)
        return json.loads(stdout)

    def _fake_run(self, report: dict[str, object], *extra: str) -> tuple[dict[str, object], str]:
        argv = [
            *_base_argv(self.csv_dir),
            "--confirm-target", FAKE_IDENTITY,
            "--confirm-tables", str(report["table_confirmation"]),
            "--confirm-schema-fingerprint", str(report["schema_confirmation_fingerprint"]),
            *extra,
        ]
        resolve = mock.Mock(return_value=(_fake_client(), None, FAKE_IDENTITY, DATASET))
        pipeline_factory = mock.Mock(side_effect=lambda **_: _FakePipeline())
        with mock.patch.object(run_migration, "MIGRATION_ROOT", self.migration_root), \
                mock.patch.object(run_migration, "_resolve_dlt_destination", resolve), \
                mock.patch.object(run_migration.dlt, "pipeline", pipeline_factory):
            code, stdout, stderr = self._main(argv)
        self.assertEqual(code, 0, stderr)
        pipeline_factory.assert_called_once()
        manifest_dir = self.migration_root / "run-manifests"
        manifests = sorted(manifest_dir.glob("*.json"))
        self.assertEqual(len(manifests), 1)
        manifest = read_private_json(manifest_dir, manifests[0].name)
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["target_identity"], FAKE_IDENTITY)
        return manifest, stdout + stderr

    def test_dry_run_records_exact_alias(self) -> None:
        report = self._dry_run("--source-binding-id", VALID_ALIAS)
        self.assertEqual(report["mode"], "dry_run")
        self.assertIs(report["writes_performed"], False)
        self.assertIn("source_binding_id", report)
        self.assertEqual(report["source_binding_id"], VALID_ALIAS)

    def test_dry_run_omits_field_when_alias_absent(self) -> None:
        report = self._dry_run()
        self.assertEqual(report["mode"], "dry_run")
        self.assertNotIn("source_binding_id", report)

    def test_alias_does_not_change_existing_dry_run_fields(self) -> None:
        with_alias = self._dry_run("--source-binding-id", VALID_ALIAS)
        without_alias = self._dry_run()
        self.assertEqual(with_alias.pop("source_binding_id"), VALID_ALIAS)
        self.assertEqual(with_alias, without_alias)

    def test_run_manifest_records_alias_equal_to_dry_run(self) -> None:
        report = self._dry_run("--source-binding-id", VALID_ALIAS)
        manifest, _ = self._fake_run(report, "--source-binding-id", VALID_ALIAS)
        self.assertIn("source_binding_id", manifest)
        self.assertEqual(manifest["source_binding_id"], VALID_ALIAS)
        self.assertEqual(manifest["source_binding_id"], report["source_binding_id"])
        self.assertEqual(manifest["schema_confirmation_fingerprint"], report["schema_confirmation_fingerprint"])

    def test_run_manifest_omits_field_when_alias_absent(self) -> None:
        report = self._dry_run()
        manifest, _ = self._fake_run(report)
        self.assertNotIn("source_binding_id", manifest)
        self.assertEqual(manifest["source_row_counts"]["customers"]["rows"], 2)

    def test_failed_run_manifest_keeps_alias(self) -> None:
        report = self._dry_run("--source-binding-id", VALID_ALIAS)

        class _FailingPipeline(_FakePipeline):
            def load(self, raise_on_failed_jobs: bool = True):
                raise RuntimeError("synthetic load failure canary")

        argv = [
            *_base_argv(self.csv_dir),
            "--confirm-target", FAKE_IDENTITY,
            "--confirm-tables", str(report["table_confirmation"]),
            "--confirm-schema-fingerprint", str(report["schema_confirmation_fingerprint"]),
            "--source-binding-id", VALID_ALIAS,
        ]
        resolve = mock.Mock(return_value=(_fake_client(), None, FAKE_IDENTITY, DATASET))
        with mock.patch.object(run_migration, "MIGRATION_ROOT", self.migration_root), \
                mock.patch.object(run_migration, "_resolve_dlt_destination", resolve), \
                mock.patch.object(run_migration.dlt, "pipeline", side_effect=lambda **_: _FailingPipeline()):
            code, _, stderr = self._main(argv)
        self.assertEqual(code, 1)
        self.assertNotIn("canary", stderr)
        manifest_dir = self.migration_root / "run-manifests"
        (manifest_path,) = manifest_dir.glob("*.json")
        manifest = read_private_json(manifest_dir, manifest_path.name)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["source_binding_id"], VALID_ALIAS)


if __name__ == "__main__":
    unittest.main()
