"""Helpers for integration tests that need a hermetic dlt project/config root."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_FILES = (
    "run_migration.py",
    "postgres_snapshot_guard.py",
    "reconcile_bronze.py",
    "acceptance_report.py",
    "evidence_index.py",
    "_private_json.py",
    "destination_type_profiles.py",
    "schema_drift.py",
    "mapping_contract.py",
)
OVERRIDE_PREFIXES = (
    "DESTINATION__", "DESTINATIONS__", "SOURCES__", "DUCKDB__", "BIGQUERY__", "SNOWFLAKE__", "DLT_",
    "SNOWFLAKE_", "SNOWSQL_", "BIGQUERY_", "BQ_", "GOOGLE_", "GCP_", "CLOUDSDK_",
    "AWS_", "AZURE_", "MIGRATION_SOURCE_",
)


def prepare_runtime(test_root: Path) -> Path:
    """Copy only runtime scripts into a clean temporary dlt project."""
    runtime = test_root / "isolated-project" / "migration"
    runtime.mkdir(parents=True)
    source = REPOSITORY_ROOT / "migration"
    for filename in MIGRATION_FILES:
        shutil.copyfile(source / filename, runtime / filename)
    (runtime / ".dlt").mkdir()
    return runtime


def isolated_environment(runtime: Path, test_root: Path, *, base: dict[str, str] | None = None) -> dict[str, str]:
    """Discard ambient dlt/provider settings and point every dlt root into test state."""
    environment = dict(os.environ if base is None else base)
    for name in tuple(environment):
        if name.startswith(OVERRIDE_PREFIXES):
            environment.pop(name, None)
    home = test_root / "isolated-home"
    home.mkdir(exist_ok=True)
    environment["HOME"] = str(home)
    environment["DLT_PROJECT_DIR"] = str(runtime)
    environment["DLT_CONFIG_FOLDER"] = ".dlt"
    environment["DLT_DATA_DIR"] = str(test_root / "isolated-dlt-data")
    environment["DLT_LOCAL_DIR"] = str(test_root / "isolated-dlt-local")
    environment["DLT_DISABLE_PLUGINS"] = "true"
    environment["RUNTIME__DLTHUB_TELEMETRY"] = "false"
    return environment


def run_script(runtime: Path, script_name: str, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    if args and args[0] == script_name:
        args = args[1:]
    return subprocess.run(
        [sys.executable, str(runtime / script_name), *args],
        cwd=runtime,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def run_python(runtime: Path, code: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a small dlt API probe in a fresh interpreter and isolated project."""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=runtime,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
