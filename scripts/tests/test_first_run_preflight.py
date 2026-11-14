"""Offline tests for scripts/first_run_preflight.py.

Synthetic fixtures only: a temporary edition workspace, a temporary HOME, and a
socket tripwire injected through sitecustomize so any network attempt fails the run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "first_run_preflight.py"
SENTINEL = "sentinel-secret-value-7f3a"

SOCKET_TRIPWIRE = textwrap.dedent(
    """
    import socket

    def _refuse(*args, **kwargs):
        raise SystemExit("network access attempted during first-run preflight test")

    socket.socket.connect = _refuse
    socket.socket.connect_ex = _refuse
    socket.create_connection = _refuse
    socket.getaddrinfo = _refuse
    socket.gethostbyname = _refuse
    """
)


class FirstRunPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.tripwire = self.root / "tripwire"
        self.tripwire.mkdir()
        (self.tripwire / "sitecustomize.py").write_text(SOCKET_TRIPWIRE, encoding="utf-8")

    def make_workspace(self, edition: str) -> Path:
        if edition == "bigquery":
            workspace = self.root / "Bigquery"
            requirements_root = workspace
        else:
            requirements_root = self.root / "Snowflake"
            workspace = requirements_root / "kit"
        workspace.mkdir(parents=True)
        (workspace / "dbt_project.yml").write_text("name: synthetic\n", encoding="utf-8")
        (workspace / "profiles.example.yml").write_text("synthetic: {}\n", encoding="utf-8")
        for name in ("requirements.txt", "requirements-lock.txt"):
            (requirements_root / name).write_text("# synthetic\n", encoding="utf-8")
        return workspace

    def run_preflight(self, edition: str, workspace: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.defpath,
            "HOME": str(self.home),
            "PYTHONPATH": str(self.tripwire),
            "PYTHONNOUSERSITE": "1",
        }
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, str(SCRIPT), edition, "--workspace", str(workspace)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )

    def test_fails_closed_without_credentials_or_profile(self) -> None:
        for edition in ("bigquery", "snowflake"):
            with self.subTest(edition=edition):
                workspace = self.make_workspace(edition)
                result = self.run_preflight(edition, workspace)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("NEEDS ACTION local profile", result.stdout)
                self.assertIn("NEEDS ACTION profile isolation", result.stdout)
                self.assertIn("cp -n", result.stdout)
                self.assertIn("do not run dbt yet", result.stdout)
                self.assertNotIn("network access attempted", result.stdout + result.stderr)
                required = ("BQ_PROJECT",) if edition == "bigquery" else ("SNOWFLAKE_DATABASE", "SNOWFLAKE_SCHEMA")
                for name in required:
                    self.assertIn(f"NEEDS ACTION {name}: not set", result.stdout)
                shutil.rmtree(workspace.parent if edition == "snowflake" else workspace)

    def test_never_writes_profiles_or_touches_home(self) -> None:
        workspace = self.make_workspace("bigquery")
        profiles_dir = workspace / ".dbt"
        profiles_dir.mkdir()
        existing = profiles_dir / "profiles.yml"
        existing.write_text(f"password: {SENTINEL}\n", encoding="utf-8")
        before = existing.read_bytes()
        result = self.run_preflight("bigquery", workspace, {"DBT_PROFILES_DIR": str(profiles_dir)})
        self.assertEqual(existing.read_bytes(), before)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertIn("PASS local profile", result.stdout)
        self.assertIn("PASS profile isolation", result.stdout)
        self.assertNotIn(SENTINEL, result.stdout + result.stderr)

    def test_refuses_production_like_targets_without_printing_values(self) -> None:
        cases = {
            "bigquery": [
                {"BQ_PROJECT": "acme-prod-analytics"},
                {"BQ_PROJECT": "bigquery-public-data"},
                {"BQ_PROJECT": "acme-dev-sandbox", "BQ_DATASET": "customer_live"},
            ],
            "snowflake": [
                {"SNOWFLAKE_DATABASE": "DBT_DEV", "SNOWFLAKE_SCHEMA": "DBT_KIT_DEV_A_SCHEMA"},
                {"SNOWFLAKE_DATABASE": "DBT_KIT_DEV_A_DB", "SNOWFLAKE_SCHEMA": "PRD2_SCHEMA"},
                {"SNOWFLAKE_DATABASE": "SHARED_DB", "SNOWFLAKE_SCHEMA": "DBT_KIT_DEV_A_SCHEMA"},
            ],
        }
        for edition, environments in cases.items():
            workspace = self.make_workspace(edition)
            for environment in environments:
                with self.subTest(edition=edition, names=sorted(environment)):
                    result = self.run_preflight(edition, workspace, environment)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("looks production-like", result.stdout)
                    for value in environment.values():
                        if value not in ("DBT_KIT_DEV_A_SCHEMA", "acme-dev-sandbox", "DBT_DEV"):  # DBT_DEV is named in guidance
                            self.assertNotIn(value, result.stdout + result.stderr)

    def test_accepts_disposable_names(self) -> None:
        cases = {
            "bigquery": {"BQ_PROJECT": "acme-starterkit-dev", "BQ_DATASET": "starterkit_product_sales"},
            "snowflake": {"SNOWFLAKE_DATABASE": "DBT_KIT_DEV_A1_DB", "SNOWFLAKE_SCHEMA": "DBT_KIT_DEV_A1_SCHEMA"},
        }
        for edition, environment in cases.items():
            with self.subTest(edition=edition):
                workspace = self.make_workspace(edition)
                result = self.run_preflight(edition, workspace, environment)
                for name in environment:
                    self.assertIn(f"PASS {name}: set to a non-production-looking name (value not printed)", result.stdout)
                for value in environment.values():
                    self.assertNotIn(value, result.stdout + result.stderr)

    def test_script_has_no_network_or_subprocess_imports(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for module in ("socket", "subprocess", "urllib", "http", "requests"):
            self.assertNotIn(f"import {module}", source)


if __name__ == "__main__":
    unittest.main()
