"""Local fault-injection tests for interrupted normalization and failed partial loads."""

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
class MigrationNormalizeLoadRecoveryTests(unittest.TestCase):
    dataset = "starterkit_migration_fault_recovery"
    pipeline_name = "fault_recovery"

    def _setup(self, root: Path):
        runtime = prepare_runtime(root)
        env = isolated_environment(runtime, root)
        source_dir = root / "csv"
        source_dir.mkdir()
        source = source_dir / "customers.csv"
        source.write_text(
            "customer_id,created_at,country\nc1,2025-01-01T00:00:00Z,AU\n",
            encoding="utf-8",
        )
        base = [
            "--source", "csv", "--destination", "duckdb",
            "--dataset", self.dataset,
            "--pipeline-name", self.pipeline_name,
            "--csv-dir", str(source_dir), "--tables", "customers",
        ]
        return runtime, env, source, base

    def _run(self, runtime: Path, env: dict[str, str], *args: str):
        return run_script(runtime, "run_migration.py", list(args), env)

    def _baseline(self, runtime: Path, env: dict[str, str], source: Path, base: list[str]):
        dry = json.loads(self._run(runtime, env, *base, "--dry-run").stdout)
        result = self._run(
            runtime,
            env,
            *base,
            "--confirm-target", dry["target_identity"],
            "--confirm-tables", dry["table_confirmation"],
            "--confirm-schema-fingerprint", dry["schema_confirmation_fingerprint"],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest_dir = runtime / "run-manifests"
        baseline_path = next(manifest_dir.glob("*.json"))
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        tail = baseline["target_identity"].removeprefix("duckdb/")
        database, separator, namespace = tail.rpartition("/")
        self.assertTrue(separator)
        return dry, manifest_dir, baseline, database, namespace

    def _rows(self, database: str, namespace: str):
        with duckdb.connect(database, read_only=True) as destination:
            return destination.execute(
                f'SELECT customer_id, country FROM "{namespace}"."customers" ORDER BY customer_id'
            ).fetchall()

    def _confirm_args(self, base: list[str], dry: dict[str, object]) -> list[str]:
        return [
            *base,
            "--confirm-target", str(dry["target_identity"]),
            "--confirm-tables", str(dry["table_confirmation"]),
            "--confirm-schema-fingerprint", str(dry["schema_confirmation_fingerprint"]),
        ]

    def test_process_exit_after_normalization_leaves_pending_state_and_refuses_retry(self) -> None:
        previous_umask = os.umask(0o077)
        try:
            with tempfile.TemporaryDirectory(prefix="migration-normalize-interrupt-") as temporary:
                root = Path(temporary)
                runtime, env, source, base = self._setup(root)
                _dry_baseline, manifest_dir, _baseline, database, namespace = self._baseline(runtime, env, source, base)
                original_rows = self._rows(database, namespace)
                (runtime / "normalizer_interrupter.py").write_text("""import os
import run_migration

original_factory = run_migration.dlt.pipeline
def factory(*args, **kwargs):
    pipeline = original_factory(*args, **kwargs)
    original_normalize = pipeline.normalize
    def normalize_then_exit(*normalize_args, **normalize_kwargs):
        original_normalize(*normalize_args, **normalize_kwargs)
        os._exit(74)
    pipeline.normalize = normalize_then_exit
    return pipeline

run_migration.dlt.pipeline = factory
raise SystemExit(run_migration.main())
""", encoding="utf-8")
                source.write_text(
                    "customer_id,created_at,country\n"
                    "c1,2025-01-01T00:00:00Z,NZ\n"
                    "c2,2025-01-02T00:00:00Z,CA\n",
                    encoding="utf-8",
                )
                dry = json.loads(self._run(runtime, env, *base, "--dry-run").stdout)
                before = {path.name for path in manifest_dir.glob("*.json")}
                interrupted = run_script(runtime, "normalizer_interrupter.py", self._confirm_args(base, dry), env)
                self.assertEqual(interrupted.returncode, 74, interrupted.stdout + interrupted.stderr)
                interrupted_manifests = [
                    path for path in manifest_dir.glob("*.json") if path.name not in before
                ]
                self.assertEqual(len(interrupted_manifests), 1)
                manifest = json.loads(interrupted_manifests[0].read_text(encoding="utf-8"))
                self.assertEqual(manifest["status"], "running")
                self.assertFalse(manifest["destination_write_may_be_partial"])

                manifest_names = {path.name for path in manifest_dir.glob("*.json")}
                retry = self._run(runtime, env, *self._confirm_args(base, dry))
                self.assertEqual(retry.returncode, 1, retry.stdout + retry.stderr)
                self.assertIn("has pending dlt packages", retry.stdout + retry.stderr)
                self.assertEqual({path.name for path in manifest_dir.glob("*.json")}, manifest_names)
                self.assertEqual(self._rows(database, namespace), original_rows)
        finally:
            os.umask(previous_umask)

    def test_injected_post_load_failure_records_possible_partial_write_without_rollback_claim(self) -> None:
        previous_umask = os.umask(0o077)
        try:
            with tempfile.TemporaryDirectory(prefix="migration-load-failure-") as temporary:
                root = Path(temporary)
                runtime, env, source, base = self._setup(root)
                _dry_baseline, manifest_dir, _baseline, database, namespace = self._baseline(runtime, env, source, base)
                original_rows = self._rows(database, namespace)
                self.assertEqual(original_rows, [("c1", "AU")])
                source.write_text(
                    "customer_id,created_at,country\n"
                    "c1,2025-01-01T00:00:00Z,NZ\n"
                    "c2,2025-01-02T00:00:00Z,CA\n",
                    encoding="utf-8",
                )
                dry = json.loads(self._run(runtime, env, *base, "--dry-run").stdout)
                (runtime / "partial_load_injector.py").write_text("""import run_migration
import duckdb

original_factory = run_migration.dlt.pipeline
def factory(*args, **kwargs):
    pipeline = original_factory(*args, **kwargs)
    original_load = pipeline.load
    def load_then_inject_partial_state(*load_args, **load_kwargs):
        info = original_load(*load_args, **load_kwargs)
        _, _, identity, _ = run_migration._resolve_dlt_destination(
            pipeline, 'duckdb', None, pipeline.dataset_name
        )
        database, _, namespace = identity.removeprefix('duckdb/').rpartition('/')
        with duckdb.connect(database) as target:
            target.execute(f'DELETE FROM "{namespace}"."customers" WHERE customer_id = ?', ['c2'])
        raise RuntimeError('synthetic injected failure with private marker')
    pipeline.load = load_then_inject_partial_state
    return pipeline

run_migration.dlt.pipeline = factory
raise SystemExit(run_migration.main())
""", encoding="utf-8")
                before = {path.name for path in manifest_dir.glob("*.json")}
                failed_result = run_script(
                    runtime, "partial_load_injector.py", self._confirm_args(base, dry), env,
                )
                output = failed_result.stdout + failed_result.stderr
                self.assertEqual(failed_result.returncode, 1, output)
                self.assertIn("Migration stopped (RuntimeError)", output)
                self.assertNotIn("synthetic injected failure", output)
                self.assertNotIn("private marker", output)
                new_manifests = [
                    path for path in manifest_dir.glob("*.json") if path.name not in before
                ]
                self.assertEqual(len(new_manifests), 1)
                manifest = json.loads(new_manifests[0].read_text(encoding="utf-8"))
                self.assertEqual(manifest["status"], "failed")
                self.assertTrue(manifest["destination_write_may_be_partial"])
                self.assertIn("dlt_failure_evidence", manifest)
                self.assertNotIn("private marker", json.dumps(manifest))
                partial_rows = self._rows(database, namespace)
                self.assertEqual(partial_rows, [("c1", "NZ")])
                self.assertNotEqual(partial_rows, original_rows)
                # The test intentionally does not claim rollback or automatic resume.
        finally:
            os.umask(previous_umask)


if __name__ == "__main__":
    unittest.main()
