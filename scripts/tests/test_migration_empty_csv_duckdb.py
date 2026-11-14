"""Verify exact empty-table authorization and replacement behavior in local DuckDB."""

from __future__ import annotations

import json
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

from scripts.tests._dlt_isolated_runtime import isolated_environment, prepare_runtime, run_script


ROOT = Path(__file__).resolve().parents[2]
try:
    if importlib.util.find_spec("dlt") is None:
        raise ModuleNotFoundError("migration runtime package dlt is unavailable", name="dlt")
    import duckdb
except ModuleNotFoundError as error:
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name not in {"dlt", "sqlalchemy", "duckdb"}:
        raise
    duckdb = None


@unittest.skipIf(duckdb is None, "migration runtime dependencies are not installed")
class EmptyCsvDuckDBTests(unittest.TestCase):
    def _invoke(self, runtime: Path, env: dict[str, str], argv: list[str]) -> tuple[int, str]:
        result = run_script(runtime, "run_migration.py", argv, env)
        return result.returncode, result.stdout + result.stderr

    def _base_args(self, source_dir: Path, dataset: str, prefix: str, selected: list[str]) -> list[str]:
        return [
            "run_migration.py", "--source", "csv", "--destination", "duckdb",
            "--dataset", dataset, "--pipeline-name", prefix, "--csv-dir", str(source_dir),
            "--tables", ",".join(selected),
        ]

    def _run_confirmed(self, base_args: list[str], allow_empty: str | None = None) -> dict[str, object]:
        dry_args = [*base_args, "--dry-run"]
        if allow_empty:
            dry_args.extend(("--allow-empty-tables", allow_empty))
        dry_status, dry_output = self._invoke(self.runtime, self.env, dry_args)
        self.assertEqual(dry_status, 0, dry_output)
        dry = json.loads(dry_output)
        manifest_directory = self.runtime / "run-manifests"
        before = {path.name for path in manifest_directory.glob("*.json")}
        load_args = [
            *base_args,
            "--confirm-target", dry["target_identity"],
            "--confirm-tables", dry["table_confirmation"],
            "--confirm-schema-fingerprint", dry["schema_confirmation_fingerprint"],
        ]
        if allow_empty:
            load_args.extend(("--allow-empty-tables", allow_empty))
        load_status, load_output = self._invoke(self.runtime, self.env, load_args)
        self.assertEqual(load_status, 0, load_output)
        new_manifests = [
            path for path in manifest_directory.glob("*.json")
            if path.name not in before and not path.name.startswith("evidence-index-")
        ]
        self.assertEqual(len(new_manifests), 1)
        return json.loads(new_manifests[0].read_text(encoding="utf-8"))

    def _counts(self, manifest: dict[str, object]) -> dict[str, int]:
        tail = str(manifest["target_identity"]).removeprefix("duckdb/")
        database, separator, namespace = tail.rpartition("/")
        self.assertTrue(separator)
        with duckdb.connect(database, read_only=True) as connection:
            return {
                table: connection.execute(f'SELECT count(*) FROM "{namespace}"."{table}"').fetchone()[0]
                for table in manifest["destination_tables"]
            }

    def test_empty_default_refusal_exact_authorization_clears_only_selected_table(self) -> None:
        previous_umask = os.umask(0o077)
        try:
            with tempfile.TemporaryDirectory(prefix="migration-empty-csv-") as temporary:
                root = Path(temporary)
                self.root = root
                self.runtime = prepare_runtime(root)
                self.env = isolated_environment(self.runtime, root)
                source_dir = root / "csv"
                source_dir.mkdir()
                customers = source_dir / "customers.csv"
                customers.write_text(
                    "customer_id,created_at,country\nc1,2025-01-01T00:00:00Z,AU\n",
                    encoding="utf-8",
                )
                (source_dir / "products.csv").write_text(
                    "product_id,product_name,category\np1,Tote,Bags\n",
                    encoding="utf-8",
                )

                # Independent target for the default-denial case. It starts populated;
                # an empty replacement without authorization must not modify it.
                default_dataset = "starterkit_migration_empty_default"
                default_args = self._base_args(source_dir, default_dataset, "empty_default", ["customers", "products"])
                default_initial = self._run_confirmed(default_args)
                self.assertEqual(self._counts(default_initial), {"customers": 1, "products": 1})
                customers.write_text("customer_id,created_at,country\n", encoding="utf-8")

                empty_default_args = self._base_args(source_dir, default_dataset, "empty_default", ["customers"])
                dry_status, dry_output = self._invoke(self.runtime, self.env, [*empty_default_args, "--dry-run"])
                self.assertEqual(dry_status, 0, dry_output)
                dry = json.loads(dry_output)
                self.assertIn("empty_inputs_not_authorized", dry["blockers"])
                manifests_before = {
                    path.name for path in (self.runtime / "run-manifests").glob("*.json")
                }
                refused_status, refused_output = self._invoke(self.runtime, self.env, [
                    *empty_default_args,
                    "--confirm-target", dry["target_identity"],
                    "--confirm-tables", dry["table_confirmation"],
                    "--confirm-schema-fingerprint", dry["schema_confirmation_fingerprint"],
                ])
                self.assertEqual(refused_status, 1)
                self.assertEqual(self._counts(default_initial), {"customers": 1, "products": 1})
                new_manifests = [
                    path for path in (self.runtime / "run-manifests").glob("*.json")
                    if path.name not in manifests_before and not path.name.startswith("evidence-index-")
                ]
                self.assertEqual(len(new_manifests), 1)
                refused_manifest = json.loads(new_manifests[0].read_text(encoding="utf-8"))
                self.assertEqual(refused_manifest["status"], "failed")
                self.assertFalse(refused_manifest["destination_write_may_be_partial"])

                # A separate populated target covers rejection of an authorization
                # naming an unselected table, followed by the exact authorized clear.
                customers.write_text(
                    "customer_id,created_at,country\nc1,2025-01-01T00:00:00Z,AU\n",
                    encoding="utf-8",
                )
                authorized_dataset = "starterkit_migration_empty_authorized"
                authorized_args = self._base_args(source_dir, authorized_dataset, "empty_authorized", ["customers", "products"])
                initial = self._run_confirmed(authorized_args)
                self.assertEqual(self._counts(initial), {"customers": 1, "products": 1})
                customers.write_text("customer_id,created_at,country\n", encoding="utf-8")

                unselected_args = self._base_args(source_dir, authorized_dataset, "empty_authorized", ["customers"])
                unselected_dry_status, unselected_dry_output = self._invoke(self.runtime, self.env, [
                    *unselected_args, "--dry-run", "--allow-empty-tables", "products",
                ])
                self.assertEqual(unselected_dry_status, 0, unselected_dry_output)
                unselected_dry = json.loads(unselected_dry_output)
                before_unselected = {path.name for path in (self.runtime / "run-manifests").glob("*.json")}
                unselected_status, _unselected_output = self._invoke(self.runtime, self.env, [
                    *unselected_args,
                    "--allow-empty-tables", "products",
                    "--confirm-target", unselected_dry["target_identity"],
                    "--confirm-tables", unselected_dry["table_confirmation"],
                    "--confirm-schema-fingerprint", unselected_dry["schema_confirmation_fingerprint"],
                ])
                self.assertEqual(unselected_status, 1)
                self.assertEqual(self._counts(initial), {"customers": 1, "products": 1})
                self.assertEqual(
                    {path.name for path in (self.runtime / "run-manifests").glob("*.json")},
                    before_unselected,
                    "unselected empty-table authorization must stop before a run manifest is created",
                )

                authorized_empty_args = self._base_args(source_dir, authorized_dataset, "empty_authorized", ["customers"])
                cleared = self._run_confirmed(authorized_empty_args, allow_empty="customers")
                self.assertEqual(cleared["status"], "completed")
                self.assertEqual(cleared["source_row_counts"]["customers"]["rows"], 0)
                self.assertEqual(cleared["dlt_prepared_row_counts"]["customers"]["rows"], 0)
                self.assertFalse(cleared["destination_write_may_be_partial"])
                self.assertEqual(self._counts(cleared), {"customers": 0})
                self.assertEqual(self._counts(initial), {"customers": 0, "products": 1})
        finally:
            os.umask(previous_umask)


if __name__ == "__main__":
    unittest.main()
