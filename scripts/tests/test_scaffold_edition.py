"""Offline tests for edition scaffolding and its exclusion/refusal behavior."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scaffold_edition  # noqa: E402


class ScaffoldEditionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.script_dir = self.repo / "scripts"
        self.script_dir.mkdir(parents=True)
        shutil.copyfile(
            Path(scaffold_edition.__file__).resolve().with_name("first_run_preflight.py"),
            self.script_dir / "first_run_preflight.py",
        )
        self.edition = self.repo / "editions" / "Bigquery"
        self.edition.mkdir(parents=True)
        self.destination = self.root / "client-copy"
        (self.edition / "README.md").write_text("edition guide\n", encoding="utf-8")
        (self.edition / "models").mkdir()
        (self.edition / "models" / "orders.sql").write_text("select 1\n", encoding="utf-8")

    def _run_main(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with patch.object(scaffold_edition, "__file__", str(self.script_dir / "scaffold_edition.py")), patch(
            "sys.argv", ["scaffold_edition.py", *arguments]
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                result = scaffold_edition.main()
            except SystemExit as error:
                result = int(error.code)
        return result, output.getvalue()

    def test_copies_independent_edition_and_omits_local_state_and_symlink(self) -> None:
        (self.edition / ".dbt").mkdir()
        (self.edition / ".dbt" / "profiles.yml").write_text("local profile", encoding="utf-8")
        (self.edition / "target").mkdir()
        (self.edition / "target" / "manifest.json").write_text("generated", encoding="utf-8")
        linked_file = self.root / "outside.txt"
        linked_file.write_text("outside", encoding="utf-8")
        (self.edition / "linked.txt").symlink_to(linked_file)

        result, _ = self._run_main("bigquery", str(self.destination))

        self.assertEqual(result, 0)
        self.assertEqual((self.destination / "README.md").read_text(encoding="utf-8"), "edition guide\n")
        self.assertEqual((self.destination / "models" / "orders.sql").read_text(encoding="utf-8"), "select 1\n")
        self.assertFalse((self.destination / ".dbt").exists())
        self.assertFalse((self.destination / "target").exists())
        self.assertFalse((self.destination / "linked.txt").exists())

        (self.destination / "README.md").write_text("changed copy\n", encoding="utf-8")
        self.assertEqual((self.edition / "README.md").read_text(encoding="utf-8"), "edition guide\n")

    def test_refuses_included_file_with_obvious_secret_marker(self) -> None:
        (self.edition / "settings.txt").write_text("password = synthetic-secret-value\n", encoding="utf-8")

        result, output = self._run_main("bigquery", str(self.destination))

        self.assertEqual(result, 2)
        self.assertIn("refusing to scaffold credential-like content", output)
        self.assertFalse(self.destination.exists())

    def test_sensitive_paths_are_excluded_without_scanning_payload(self) -> None:
        (self.edition / ".ssh").mkdir()
        (self.edition / ".ssh" / "id_rsa").write_text("synthetic placeholder", encoding="utf-8")
        (self.edition / "profiles.yml").write_text("synthetic placeholder", encoding="utf-8")

        result, _ = self._run_main("bigquery", str(self.destination))

        self.assertEqual(result, 0)
        self.assertFalse((self.destination / ".ssh").exists())
        self.assertFalse((self.destination / "profiles.yml").exists())

    def test_snowflake_scaffold_uses_snowflake_source_and_excludes_archive_state(self) -> None:
        snowflake = self.repo / "editions" / "Snowflake"
        snowflake.mkdir()
        (snowflake / "README.md").write_text("synthetic Snowflake guide\n", encoding="utf-8")
        (snowflake / "kit").mkdir()
        (snowflake / "kit" / "dbt_project.yml").write_text("name: fixture\n", encoding="utf-8")
        (snowflake / "kit" / "target").mkdir()
        (snowflake / "kit" / "target" / "manifest.json").write_text("generated", encoding="utf-8")

        result, _ = self._run_main("snowflake", str(self.destination))

        self.assertEqual(result, 0)
        self.assertEqual((self.destination / "README.md").read_text(encoding="utf-8"), "synthetic Snowflake guide\n")
        self.assertEqual((self.destination / "kit" / "dbt_project.yml").read_text(encoding="utf-8"), "name: fixture\n")
        self.assertFalse((self.destination / "kit" / "target").exists())

    def test_dry_run_lists_planned_files_and_writes_nothing(self) -> None:
        (self.edition / "target").mkdir()
        (self.edition / "target" / "manifest.json").write_text("generated", encoding="utf-8")
        before = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))

        result, output = self._run_main("bigquery", str(self.destination), "--dry-run")

        self.assertEqual(result, 0)
        report = json.loads(output)
        self.assertEqual(report["status"], "plan_only")
        self.assertFalse(report["writes_performed"])
        self.assertEqual(
            report["planned_files"],
            ["README.md", "models/orders.sql", "scripts/first_run_preflight.py"],
        )
        self.assertNotIn(str(self.root), output)
        self.assertEqual(before, sorted(path.relative_to(self.root) for path in self.root.rglob("*")))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_unwritable_parent_is_a_clean_usage_error(self) -> None:
        parent = self.root / "read-only"
        parent.mkdir()
        os.chmod(parent, 0o500)
        self.addCleanup(os.chmod, parent, 0o700)

        result, output = self._run_main("bigquery", str(parent / "client-copy"))

        self.assertEqual(result, 2)
        self.assertIn("could not create a staging folder", output)
        self.assertNotIn("Traceback", output)
        self.assertEqual(list(parent.iterdir()), [])

    def test_copy_failure_is_a_clean_usage_error_and_cleans_staging(self) -> None:
        with patch.object(scaffold_edition.shutil, "copytree", side_effect=OSError(13, "synthetic")):
            result, output = self._run_main("bigquery", str(self.destination))

        self.assertEqual(result, 2)
        self.assertIn("could not stage the workspace copy", output)
        self.assertNotIn("Traceback", output)
        self.assertFalse(self.destination.exists())
        self.assertEqual([p for p in self.root.iterdir() if p.name.startswith(".client-copy.scaffold-")], [])

    def test_refuses_existing_destination_and_fifo_is_skipped(self) -> None:
        os.mkfifo(self.edition / "pipe")
        result, _ = self._run_main("bigquery", str(self.destination))
        self.assertEqual(result, 0)
        self.assertFalse((self.destination / "pipe").exists())

        result, output = self._run_main("bigquery", str(self.destination))
        self.assertEqual(result, 2)
        self.assertIn("destination already exists", output)


if __name__ == "__main__":
    unittest.main()
