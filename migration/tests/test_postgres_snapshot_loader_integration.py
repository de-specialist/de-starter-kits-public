"""Offline contract checks for PostgreSQL snapshot guard wiring."""

from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path
import subprocess
import sys
import unittest

from dlt.sources.sql_database import sql_database

import run_migration


class LoaderIntegrationTests(unittest.TestCase):
    def test_dlt_sql_database_supports_engine_adapter_callback(self):
        self.assertIn("engine_adapter_callback", inspect.signature(sql_database).parameters)

    def test_guard_call_order_covers_extraction_before_load(self):
        tree = ast.parse(Path(run_migration.__file__).read_text(encoding="utf-8"))
        main = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        calls = []
        for node in ast.walk(main):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
            elif isinstance(node.func, ast.Name):
                name = node.func.id
            else:
                continue
            if name in {"require_operator_gate", "pipeline", "extract", "confirm", "normalize", "load"}:
                calls.append((node.lineno, name))
        first_line = lambda name: min(line for line, called in calls if called == name)
        self.assertLess(first_line("require_operator_gate"), first_line("pipeline"))
        self.assertLess(first_line("extract"), first_line("confirm"))
        self.assertLess(first_line("confirm"), first_line("normalize"))
        self.assertLess(first_line("normalize"), first_line("load"))

    def test_unacknowledged_postgres_is_refused_before_source_or_pipeline_io(self):
        migration_dir = Path(run_migration.__file__).parent
        env = dict(os.environ)
        env["MIGRATION_SOURCE_URL"] = "postgresql+psycopg://user:secret@127.0.0.1:1/db"
        result = subprocess.run(
            [
                sys.executable, str(migration_dir / "run_migration.py"),
                "--source", "sql", "--destination", "duckdb",
                "--source-schema", "public", "--tables", "customers",
            ],
            cwd=migration_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("enable the snapshot guard", result.stderr)
        self.assertNotIn("secret", result.stdout + result.stderr)
        self.assertFalse((migration_dir / ".dlt" / "pipelines").exists())
        self.assertFalse((migration_dir / "run-manifests").exists())


if __name__ == "__main__":
    unittest.main()
