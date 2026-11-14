"""Verify a real process interruption after extract is detected before another replacement."""

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
class InterruptedExtractionRecoveryTests(unittest.TestCase):
    def _invoke(self, runtime: Path, env: dict[str, str], *args: str):
        return run_script(runtime, "run_migration.py", list(args), env)

    def _rows(self, database: str, namespace: str) -> list[tuple]:
        with duckdb.connect(database, read_only=True) as destination:
            return destination.execute(
                f'SELECT customer_id, created_at, country FROM "{namespace}"."customers" ORDER BY customer_id'
            ).fetchall()

    def test_post_extract_process_exit_is_refused_on_followup_invocation(self) -> None:
        previous_umask = os.umask(0o077)
        try:
            with tempfile.TemporaryDirectory(prefix="migration-interrupted-extract-") as temporary:
                root = Path(temporary)
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
                    "--dataset", "starterkit_migration_interrupted_extract",
                    "--pipeline-name", "interrupted_extract",
                    "--csv-dir", str(source_dir), "--tables", "customers",
                ]
                dry_baseline = json.loads(self._invoke(runtime, env, *base, "--dry-run").stdout)
                baseline = self._invoke(
                    runtime, env, *base,
                    "--confirm-target", dry_baseline["target_identity"],
                    "--confirm-tables", dry_baseline["table_confirmation"],
                    "--confirm-schema-fingerprint", dry_baseline["schema_confirmation_fingerprint"],
                )
                self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
                manifest_dir = runtime / "run-manifests"
                baseline_files = {path.name for path in manifest_dir.glob("*.json")}
                baseline_manifest_path = next(manifest_dir.glob("*.json"))
                baseline_manifest = json.loads(baseline_manifest_path.read_text(encoding="utf-8"))
                target_tail = baseline_manifest["target_identity"].removeprefix("duckdb/")
                database, separator, namespace = target_tail.rpartition("/")
                self.assertTrue(separator)
                original_rows = self._rows(database, namespace)

                source.write_text(
                    "customer_id,created_at,country\n"
                    "c1,2025-01-01T00:00:00Z,AU\n"
                    "c2,2025-01-02T00:00:00Z,NZ\n",
                    encoding="utf-8",
                )
                dry = json.loads(self._invoke(runtime, env, *base, "--dry-run").stdout)
                self.assertEqual(dry["target_identity"], baseline_manifest["target_identity"])

                interrupter = runtime / "interrupt_after_extract.py"
                interrupter.write_text("""import os
import run_migration

original_factory = run_migration.dlt.pipeline
def factory(*args, **kwargs):
    pipeline = original_factory(*args, **kwargs)
    original_extract = pipeline.extract
    def extract_then_exit(*extract_args, **extract_kwargs):
        original_extract(*extract_args, **extract_kwargs)
        os._exit(73)
    pipeline.extract = extract_then_exit
    return pipeline

run_migration.dlt.pipeline = factory
raise SystemExit(run_migration.main())
""", encoding="utf-8")
                interrupted = run_script(runtime, "interrupt_after_extract.py", [
                    *base,
                    "--confirm-target", dry["target_identity"],
                    "--confirm-tables", dry["table_confirmation"],
                    "--confirm-schema-fingerprint", dry["schema_confirmation_fingerprint"],
                ], env)
                self.assertEqual(interrupted.returncode, 73, interrupted.stdout + interrupted.stderr)
                interrupted_manifests = [
                    path for path in manifest_dir.glob("*.json")
                    if path.name not in baseline_files and not path.name.startswith("evidence-index-")
                ]
                self.assertEqual(len(interrupted_manifests), 1)
                interrupted_manifest = json.loads(interrupted_manifests[0].read_text(encoding="utf-8"))
                self.assertEqual(interrupted_manifest["status"], "running")
                self.assertFalse(interrupted_manifest["destination_write_may_be_partial"])

                before_retry_files = {path.name for path in manifest_dir.glob("*.json")}
                retry = self._invoke(
                    runtime, env, *base,
                    "--confirm-target", dry["target_identity"],
                    "--confirm-tables", dry["table_confirmation"],
                    "--confirm-schema-fingerprint", dry["schema_confirmation_fingerprint"],
                )
                self.assertEqual(retry.returncode, 1, retry.stdout + retry.stderr)
                self.assertIn("has pending dlt packages", retry.stdout + retry.stderr)
                self.assertIn("resolve them before starting another replacement", retry.stdout + retry.stderr)
                self.assertEqual({path.name for path in manifest_dir.glob("*.json")}, before_retry_files)
                self.assertEqual(self._rows(database, namespace), original_rows)
        finally:
            os.umask(previous_umask)


if __name__ == "__main__":
    unittest.main()
