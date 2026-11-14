#!/usr/bin/env python3
"""Check local, credential-free prerequisites before a dbt first run."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import re
import shutil
import sys
from pathlib import Path


def default_workspace(edition: str) -> Path:
    script_root = Path(__file__).resolve().parent.parent
    if (script_root / "editions").is_dir():
        return script_root / "editions" / ("Bigquery" if edition == "bigquery" else "Snowflake") / (
            "kit" if edition == "snowflake" else ""
        )
    candidate = script_root
    if edition == "snowflake" and (candidate / "kit" / "dbt_project.yml").is_file():
        return candidate / "kit"
    return candidate


# Kept in step with cloud_acceptance_preflight.PRODUCTION_TOKENS. This script is copied
# alone into scaffolded workspaces and release archives, so it must stay standalone.
PRODUCTION_TOKENS = frozenset(
    {
        "prod", "prd", "production", "live", "main", "master", "primary", "release",
        "customer", "customers", "client", "clients", "pii", "shared", "public",
    }
)


def target_looks_unsafe(value: str) -> bool:
    """True for production-like or shared-default identifiers. Never echoes the value."""
    if "DBT_DEV" in value.upper().replace("-", "_") or value == "bigquery-public-data":
        return True
    tokens = [
        token
        for token in re.split(r"[^a-z0-9]+|(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])", value.lower())
        if token
    ]
    return any(
        token in PRODUCTION_TOKENS or (token.startswith("prod") and not token.startswith("product"))
        for token in tokens
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("edition", choices=("bigquery", "snowflake"))
    parser.add_argument("--workspace", type=Path, help="dbt project directory; defaults to this edition")
    args = parser.parse_args()

    workspace = (args.workspace or default_workspace(args.edition)).expanduser().resolve()
    project_root = workspace
    requirements_root = workspace
    if args.edition == "snowflake":
        if not (project_root / "dbt_project.yml").is_file() and (workspace / "kit" / "dbt_project.yml").is_file():
            project_root = workspace / "kit"
        requirements_root = project_root.parent

    failures = 0

    def report(label: str, passed: bool, detail: str, fix: str | None = None) -> None:
        nonlocal failures
        print(f"{'PASS' if passed else 'NEEDS ACTION'} {label}: {detail}")
        if not passed and fix:
            print(f"  Next step: {fix}")
        failures += not passed

    report(
        "Python",
        sys.version_info[:2] == (3, 11),
        "Python 3.11 is required by the documented lock matrix.",
        "Create and activate the environment with Python 3.11 (for example, `python3.11 -m venv .venv && source .venv/bin/activate`).",
    )
    report(
        "dbt CLI",
        shutil.which("dbt") is not None,
        "Install the edition's hash-locked requirements first.",
        f"Activate the edition environment, then run `python -m pip install --require-hashes -r {requirements_root / 'requirements-lock.txt'}`.",
    )
    report(
        "project",
        (project_root / "dbt_project.yml").is_file(),
        f"Expected dbt_project.yml under {project_root}.",
        "Pass the directory containing this edition's dbt_project.yml with `--workspace`.",
    )

    adapter = "dbt-bigquery" if args.edition == "bigquery" else "dbt-snowflake"
    try:
        core_version = importlib.metadata.version("dbt-core")
        adapter_version = importlib.metadata.version(adapter)
    except importlib.metadata.PackageNotFoundError:
        core_version = adapter_version = None
    report(
        "dbt packages",
        core_version is not None and adapter_version is not None,
        f"Expected dbt-core and {adapter} from the edition lock; installed versions are not printed.",
        f"Activate the edition environment, then install its hash-locked dependencies with `python -m pip install --require-hashes -r {requirements_root / 'requirements-lock.txt'}`.",
    )

    lock_ok = (requirements_root / "requirements-lock.txt").is_file()
    requirements_ok = (requirements_root / "requirements.txt").is_file()
    report(
        "dependency files",
        lock_ok and requirements_ok,
        "Both requirements.txt and requirements-lock.txt must be present.",
        f"Run this check from a complete edition checkout; expected files under {requirements_root}.",
    )

    profiles_dir = project_root / ".dbt"
    profile_file = profiles_dir / "profiles.yml"
    example_profile = project_root / "profiles.example.yml"
    if args.edition == "snowflake" and not example_profile.is_file():
        example_profile = requirements_root / "kit" / "profiles.example.yml"
    report(
        "local profile",
        profile_file.is_file(),
        "Create the edition-local .dbt/profiles.yml from its example file.",
        f"Run `mkdir -p {profiles_dir} && cp -n {example_profile} {profile_file}`; `cp -n` keeps any existing profile intact.",
    )
    configured_profiles_dir = os.environ.get("DBT_PROFILES_DIR")
    profiles_path_matches = configured_profiles_dir is not None and Path(configured_profiles_dir).expanduser().resolve() == profiles_dir.resolve()
    report(
        "profile isolation",
        profiles_path_matches,
        "Set DBT_PROFILES_DIR to this workspace's .dbt directory; no profile contents are read.",
        f"Run `export DBT_PROFILES_DIR={profiles_dir}` in this shell before running dbt.",
    )

    target_vars = (
        ("BQ_PROJECT", "BQ_DATASET") if args.edition == "bigquery" else ("SNOWFLAKE_DATABASE", "SNOWFLAKE_SCHEMA")
    )
    required_vars = ("BQ_PROJECT",) if args.edition == "bigquery" else target_vars
    for name in target_vars:
        value = os.environ.get(name, "")
        if not value:
            if name in required_vars:
                report(
                    f"{name}",
                    False,
                    "not set; dbt has no safe target without it (only presence is checked, values are never printed).",
                    f"Run `export {name}=<dedicated-non-production-value>` in this shell.",
                )
            continue
        unsafe = target_looks_unsafe(value)
        report(
            f"{name}",
            not unsafe,
            "looks production-like or uses the shared DBT_DEV/public default; refused (value not printed)."
            if unsafe
            else "set to a non-production-looking name (value not printed).",
            f"Point {name} at a dedicated disposable development target, not prod, live, shared, or customer data."
            if unsafe
            else None,
        )

    print(f"Workspace: {project_root}")
    if failures:
        print(f"Preflight found {failures} item(s) to resolve; do not run dbt yet. This check does not contact a warehouse or validate credentials.")
        return 1
    print("Local preflight passed. Next run dbt debug, then dbt build against the intended non-production target.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
