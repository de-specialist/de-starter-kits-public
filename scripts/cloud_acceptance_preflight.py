#!/usr/bin/env python3
"""Offline readiness check before a cloud acceptance run against a disposable target.

This preflight makes no network call, does not run dbt or any warehouse client, and
never plans or applies Terraform. It reads only the selected edition's project files and
the selected dbt profile, and it never prints environment-variable or profile values.
Passing it does not prove credentials, permissions, cost, or that the target is truly
disposable; `dbt debug` remains the first command that contacts the warehouse.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

OFFLINE_NOTICE = (
    "Offline check only: this preflight makes no network call, does not run dbt or any "
    "warehouse client, and never plans or applies Terraform."
)


@dataclass(frozen=True)
class Edition:
    key: str
    directory: tuple[str, ...]
    project_name: str
    adapter_type: str


EDITIONS = {
    "bigquery": Edition("bigquery", ("Bigquery",), "retail_analytics", "bigquery"),
    "snowflake": Edition("snowflake", ("Snowflake", "kit"), "reliable_pipeline", "snowflake"),
}

# Identity contracts mirror the Terraform variable validations and CI guards.
BQ_PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
BQ_DATASET_PATTERN = re.compile(r"^starterkit_[a-z0-9_]{1,90}$")
SF_DATABASE_PATTERN = re.compile(r"^(DBT_KIT_(?:DEV|CI)(?:_[A-Z0-9]+)*)_DB$")
# Snowflake account identifier as dbt-snowflake expects it: the preferred
# `orgname-accountname` form (optionally `.privatelink`), or a legacy account locator
# with optional region, cloud, and privatelink segments. Never a host name or URL.
SF_ACCOUNT_PATTERN = re.compile(
    r"[A-Za-z][A-Za-z0-9]*-[A-Za-z][A-Za-z0-9_]*(?:\.privatelink)?"
    r"|[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z0-9][A-Za-z0-9-]*){0,3}"
)
SF_ACCOUNT_MAX_LENGTH = 255
SF_IDENTITY_SEPARATOR = "/"
FORBIDDEN_BQ_PROJECTS = frozenset({"bigquery-public-data"})
SNOWFLAKE_ADMIN_ROLES = frozenset(
    {"ACCOUNTADMIN", "ORGADMIN", "PUBLIC", "SECURITYADMIN", "SYSADMIN", "USERADMIN"}
)
PRODUCTION_TOKENS = frozenset(
    {
        "prod",
        "prd",
        "production",
        "live",
        "main",
        "master",
        "primary",
        "release",
        "customer",
        "customers",
        "client",
        "clients",
        "pii",
        "shared",
        "public",
    }
)
BQ_MAX_BYTES_BILLED_CEILING = 1_000_000_000
# Only these well-known auth mode names are echoed; any other profile value (for example
# an Okta URL authenticator or a templated method) is reported without printing it.
KNOWN_BQ_METHODS = frozenset({"oauth", "oauth-secrets", "service-account", "service-account-json", "external-oauth-wif"})
KNOWN_SF_AUTHENTICATORS = frozenset(
    {"snowflake", "snowflake_jwt", "externalbrowser", "oauth", "username_password_mfa", "programmatic_access_token"}
)
ENV_VAR_REFERENCE = re.compile(
    r"""^\s*\{\{\s*env_var\(\s*(['"])([A-Za-z_][A-Za-z0-9_]*)\1\s*"""
    r"""(?:,\s*(['"])(.*?)\3\s*)?\)\s*\}\}\s*$"""
)


def identity_problems(name: str) -> list[str]:
    """Return reasons a disposable-target identifier is refused, without echoing it."""
    problems: list[str] = []
    normalized = name.upper().replace("-", "_")
    if "DBT_DEV" in normalized:
        problems.append("contains DBT_DEV, the shared development default; use a dedicated acceptance target")
    tokens = [
        token
        for token in re.split(r"[^a-z0-9]+|(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])", name.lower())
        if token
    ]
    if any(
        token in PRODUCTION_TOKENS or (token.startswith("prod") and not token.startswith("product"))
        for token in tokens
    ):
        problems.append("looks production-like (for example prod, live, main, customer, or shared)")
    return problems


@dataclass(frozen=True)
class Resolution:
    value: str | None
    env_name: str | None = None
    used_default: bool = False
    unresolvable: bool = False


def resolve_profile_value(raw: Any, environ: Mapping[str, str]) -> Resolution:
    """Statically resolve a literal or a simple `{{ env_var('X', 'default') }}` reference."""
    if raw is None:
        return Resolution(None)
    if not isinstance(raw, str):
        return Resolution(str(raw))
    if "{{" not in raw and "{%" not in raw:
        return Resolution(raw.strip() or None)
    match = ENV_VAR_REFERENCE.match(raw)
    if match is None:
        return Resolution(None, unresolvable=True)
    env_name, default = match.group(2), match.group(4)
    current = environ.get(env_name, "")
    if current:
        return Resolution(current, env_name=env_name)
    if default is not None and default != "":
        return Resolution(default, env_name=env_name, used_default=True)
    return Resolution(None, env_name=env_name)


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def default_workspace(edition: Edition) -> Path:
    return REPOSITORY_ROOT.joinpath("editions", *edition.directory)


def display_path(path: Path) -> str:
    """Show a checkout-relative path; never print an absolute local path."""
    try:
        relative = path.resolve().relative_to(REPOSITORY_ROOT)
    except (OSError, ValueError):
        return "a directory outside this checkout (path not printed)"
    text = relative.as_posix()
    if text == ".":
        return "the repository root"
    return text if text.isprintable() else "a checkout path (not printed)"


def display_auth_mode(key: str, value: str, known: frozenset[str]) -> str:
    """Name a well-known auth mode; redact arbitrary or templated profile values."""
    if value in known:
        return f"{key} `{value}`"
    return f"a custom {key} (value not printed)"


class Reporter:
    def __init__(self) -> None:
        self.failures = 0

    def __call__(self, label: str, passed: bool, detail: str, fix: str | None = None) -> bool:
        print(f"{'PASS' if passed else 'NEEDS ACTION'} {label}: {detail}")
        if not passed:
            self.failures += 1
            if fix:
                print(f"  Next step: {fix}")
        return passed


def load_yaml(path: Path) -> tuple[Any, str | None]:
    """Load YAML without ever surfacing parser messages, which can quote file contents."""
    try:
        import yaml  # PyYAML ships with dbt-core and the offline test lock.
    except ImportError:
        return None, "PyYAML is unavailable; activate the edition environment that has dbt installed"
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None, "could not be read as YAML (contents not printed)"


def describe(key: str, resolution: Resolution) -> str:
    if resolution.unresolvable:
        return f"`{key}` uses Jinja this offline check cannot resolve; use a literal or a simple env_var() reference"
    if resolution.env_name:
        return f"`{key}` from {resolution.env_name} is not set and has no default"
    return f"`{key}` is missing from the selected target"


def check_identity_field(
    report: Reporter,
    target: Mapping[str, Any],
    keys: tuple[str, ...],
    expected: str,
    flag: str,
    environ: Mapping[str, str],
    *,
    case_insensitive: bool,
) -> None:
    key = next((candidate for candidate in keys if candidate in target), keys[0])
    resolution = resolve_profile_value(target.get(key), environ)
    label = f"profile {key}"
    if resolution.value is None:
        report(label, False, describe(key, resolution), f"Make the selected target resolve `{key}` to the {flag} value.")
        return
    actual = resolution.value.upper() if case_insensitive else resolution.value
    wanted = expected.upper() if case_insensitive else expected
    if actual == wanted:
        report(label, True, f"resolves to the explicit {flag} identity.")
        return
    source = (
        f"the env_var() default ({resolution.env_name} is unset)"
        if resolution.used_default
        else (f"{resolution.env_name}" if resolution.env_name else "a hard-coded literal")
    )
    detail = f"resolves from {source} to a different value than {flag} (value not printed)."
    if identity_problems(resolution.value):
        detail += " That value is also DBT_DEV or production-like."
    fix = (
        f"Export {resolution.env_name} to the {flag} value in this shell."
        if resolution.env_name
        else f"Use a dedicated acceptance target whose `{key}` is env_var() driven or equals {flag}."
    )
    report(label, False, detail, fix)


def check_required(
    report: Reporter, target: Mapping[str, Any], key: str, environ: Mapping[str, str], label: str | None = None
) -> Resolution:
    resolution = resolve_profile_value(target.get(key), environ)
    report(
        label or f"profile {key}",
        resolution.value is not None,
        "configured (value not printed)." if resolution.value is not None else describe(key, resolution),
        None if resolution.value is not None else f"Set `{key}` for the selected target without committing it.",
    )
    return resolution


def resolve_auth_mode(
    report: Reporter, target: Mapping[str, Any], key: str, default: str, environ: Mapping[str, str]
) -> str | None:
    """Resolve an auth mode literal or env_var() reference; refuse what cannot be resolved."""
    if key not in target:
        return default
    resolution = resolve_profile_value(target.get(key), environ)
    if resolution.value is None:
        report(
            "credentials",
            False,
            describe(key, resolution) + "; the auth mode decides which credential is checked.",
            f"Set `{key}` to a literal or a simple env_var() reference that resolves.",
        )
        return None
    return resolution.value.strip().lower()


def check_file_reference(
    report: Reporter, target: Mapping[str, Any], key: str, environ: Mapping[str, str], label: str
) -> None:
    resolution = resolve_profile_value(target.get(key), environ)
    if resolution.value is None:
        report(label, False, describe(key, resolution), f"Point `{key}` at a local key file outside the repository.")
        return
    exists = Path(resolution.value).expanduser().is_file()
    report(
        label,
        exists,
        "file exists (path and contents not printed)." if exists else "referenced file does not exist (path not printed).",
        None if exists else f"Fix `{key}` so it names an existing local file; the file is never opened here.",
    )


def check_bigquery_target(
    report: Reporter, target: Mapping[str, Any], args: argparse.Namespace, environ: Mapping[str, str]
) -> None:
    check_identity_field(report, target, ("project", "database"), args.project, "--project", environ, case_insensitive=False)
    check_identity_field(report, target, ("dataset", "schema"), args.dataset, "--dataset", environ, case_insensitive=False)

    location = resolve_profile_value(target.get("location"), environ)
    report(
        "profile location",
        location.value is not None and location.value.upper() == "US",
        "must resolve to the US multi-region that hosts the public TheLook source.",
        "Set `location: US` (or BQ_LOCATION=US) on the selected target.",
    )

    max_bytes = resolve_profile_value(target.get("maximum_bytes_billed"), environ)
    try:
        limit = int(max_bytes.value) if max_bytes.value is not None else None
    except ValueError:
        limit = None
    report(
        "cost guard",
        limit is not None and 0 < limit <= BQ_MAX_BYTES_BILLED_CEILING,
        "maximum_bytes_billed must be a positive integer no greater than the documented 1 GB guard.",
        "Keep `maximum_bytes_billed: 1000000000` on the selected target.",
    )

    # execution_project bills and runs every query; it must stay on the disposable project.
    if "execution_project" in target:
        check_identity_field(
            report, target, ("execution_project",), args.project, "--project", environ, case_insensitive=False
        )

    method = resolve_auth_mode(report, target, "method", "oauth", environ)
    if method is None:
        return
    if method == "service-account":
        check_file_reference(report, target, "keyfile", environ, "credentials")
    elif method == "service-account-json":
        check_required(report, target, "keyfile_json", environ, "credentials")
    else:
        report(
            "credentials",
            True,
            f"{display_auth_mode('method', method, KNOWN_BQ_METHODS)} is not inspected offline; "
            "dbt debug performs the authenticated check.",
        )


def check_snowflake_target(
    report: Reporter, target: Mapping[str, Any], args: argparse.Namespace, environ: Mapping[str, str]
) -> None:
    # Bind the explicit --account to the account the selected target resolves to, so
    # DATABASE.SCHEMA cannot be confirmed while the profile points at another account.
    check_identity_field(report, target, ("account",), args.account, "--account", environ, case_insensitive=True)
    check_required(report, target, "user", environ)
    check_identity_field(report, target, ("database",), args.database, "--database", environ, case_insensitive=True)
    check_identity_field(report, target, ("schema",), args.schema, "--schema", environ, case_insensitive=True)
    check_required(report, target, "warehouse", environ)

    role = resolve_profile_value(target.get("role"), environ)
    if role.value is None:
        report(
            "profile role",
            False,
            describe("role", role) + "; an implicit default role may be an administrator role.",
            "Set an explicit non-admin custom role on the selected target.",
        )
    else:
        is_admin = role.value.strip().strip('"').upper() in SNOWFLAKE_ADMIN_ROLES
        report(
            "profile role",
            not is_admin,
            "a built-in admin or PUBLIC role is refused (value not printed)." if is_admin else "explicit non-admin role configured (value not printed).",
            "Use the dedicated non-admin role granted on the Terraform-created database and schema.",
        )

    authenticator = resolve_auth_mode(report, target, "authenticator", "snowflake", environ)
    if authenticator is None:
        return
    if authenticator == "snowflake_jwt":
        if "private_key" in target and "private_key_path" not in target:
            check_required(report, target, "private_key", environ, "credentials")
        else:
            check_file_reference(report, target, "private_key_path", environ, "credentials")
    elif authenticator == "snowflake":
        check_required(report, target, "password", environ, "credentials")
    else:
        report(
            "credentials",
            True,
            f"{display_auth_mode('authenticator', authenticator, KNOWN_SF_AUTHENTICATORS)} is not inspected offline; "
            "dbt debug performs the authenticated check.",
        )


def validate_identity(report: Reporter, args: argparse.Namespace) -> str | None:
    """Validate the explicit identity flags and return the canonical identity when valid."""
    if args.edition == "bigquery":
        parts = (("--project", args.project, BQ_PROJECT_PATTERN, "a valid Google Cloud project ID"),
                 ("--dataset", args.dataset, BQ_DATASET_PATTERN, "a dedicated starterkit_-prefixed dataset"))
    else:
        parts = (("--database", args.database, SF_DATABASE_PATTERN, "a Terraform-style DBT_KIT_DEV..._DB or DBT_KIT_CI..._DB name"),)

    valid = True
    for flag, value, pattern, expectation in parts:
        problems = []
        if not pattern.match(value):
            problems.append(f"must be {expectation}")
        if flag == "--project" and value in FORBIDDEN_BQ_PROJECTS:
            problems.append("names the public source project, which is read-only")
        problems.extend(identity_problems(value))
        valid &= report(
            f"identity {flag}",
            not problems,
            "; ".join(problems) + " (value not printed)." if problems else "explicit, disposable-style name accepted.",
            "Create or choose a dedicated disposable target; see docs/cloud-acceptance-runbook.md." if problems else None,
        )

    if args.edition == "snowflake":
        valid &= report(
            "identity --account",
            snowflake_account_is_valid(args.account),
            "must be a Snowflake account identifier (orgname-accountname or a legacy account locator), "
            "not a host name or URL (value not printed).",
            "Pass the account identifier exactly as the selected target's `account` resolves, "
            "without .snowflakecomputing.com.",
        )
        match = SF_DATABASE_PATTERN.match(args.database)
        expected_schema = f"{match.group(1)}_SCHEMA" if match else None
        problems = identity_problems(args.schema)
        if expected_schema is None or args.schema != expected_schema:
            problems.insert(0, "must use the --database prefix with the _SCHEMA suffix, as Terraform provisions")
        valid &= report(
            "identity --schema",
            not problems,
            "; ".join(problems) + " (value not printed)." if problems else "matches the Terraform database prefix.",
            "Pass the schema Terraform created alongside the database (for example DBT_KIT_CI_ACCEPT_SCHEMA)." if problems else None,
        )

    if not valid:
        return None
    if args.edition == "bigquery":
        return f"{args.project}.{args.dataset}"
    return f"{args.account}{SF_IDENTITY_SEPARATOR}{args.database}.{args.schema}"


def snowflake_account_is_valid(value: str) -> bool:
    """Accept only a bare account identifier; fullmatch also refuses a trailing newline."""
    return (
        0 < len(value) <= SF_ACCOUNT_MAX_LENGTH
        and SF_ACCOUNT_PATTERN.fullmatch(value) is not None
        and "snowflakecomputing" not in value.lower()
    )


def printable_identity(edition: Edition, args: argparse.Namespace) -> str:
    """The confirmed identity for the summary line; the Snowflake account is never printed."""
    if edition.key == "bigquery":
        return f"{args.project}.{args.dataset}"
    return f"{args.database}.{args.schema} (account bound to --account; value not printed)"


def check_edition(report: Reporter, edition: Edition, workspace: Path) -> None:
    project_file = workspace / "dbt_project.yml"
    if not report(
        "edition project",
        project_file.is_file(),
        f"expected the {edition.key} dbt_project.yml under {display_path(workspace)}.",
        "Run from a complete checkout or pass the edition's dbt project directory with --workspace.",
    ):
        return
    project, error = load_yaml(project_file)
    matches = (
        error is None
        and isinstance(project, dict)
        and project.get("name") == edition.project_name
        and project.get("profile") == edition.project_name
    )
    report(
        "edition identity",
        matches,
        error or f"dbt_project.yml must declare name and profile `{edition.project_name}` for the {edition.key} edition.",
        "Select the edition that matches this workspace; the editions must not be mixed.",
    )
    requirements_root = workspace.parent if edition.key == "snowflake" else workspace
    report(
        "edition dependencies",
        (requirements_root / "requirements.txt").is_file() and (requirements_root / "requirements-lock.txt").is_file(),
        f"requirements.txt and requirements-lock.txt must exist under {display_path(requirements_root)}.",
        "Restore the edition's dependency files from version control.",
    )
    report(
        "edition profile example",
        (workspace / "profiles.example.yml").is_file(),
        "profiles.example.yml documents the expected environment variables.",
        "Restore profiles.example.yml from version control.",
    )


def check_profile(
    report: Reporter,
    edition: Edition,
    args: argparse.Namespace,
    environ: Mapping[str, str],
) -> None:
    configured = args.profiles_dir or environ.get("DBT_PROFILES_DIR")
    if not report(
        "profiles directory",
        bool(configured),
        "an explicit --profiles-dir or DBT_PROFILES_DIR is required so dbt cannot fall back to a shared ~/.dbt profile.",
        "Pass --profiles-dir here and the same --profiles-dir to every dbt command.",
    ):
        return
    profile_file = Path(configured).expanduser().resolve() / "profiles.yml"
    if not report(
        "profiles file",
        profile_file.is_file(),
        "looked for profiles.yml in the selected profiles directory (path not printed); "
        "contents are parsed in memory and never printed.",
        "Create it from the edition's profiles.example.yml without overwriting an existing profile (`cp -n`).",
    ):
        return
    profiles, error = load_yaml(profile_file)
    if not report("profiles format", error is None and isinstance(profiles, dict), error or "profiles.yml must be a YAML mapping."):
        return
    profile = profiles.get(edition.project_name)
    outputs = profile.get("outputs") if isinstance(profile, dict) else None
    if not report(
        "profile key",
        isinstance(outputs, dict),
        f"profiles.yml must define `{edition.project_name}` with an outputs mapping.",
        f"Merge the `{edition.project_name}` block from profiles.example.yml into profiles.yml.",
    ):
        return
    target = outputs.get(args.target)
    if not report(
        "profile target",
        isinstance(target, dict),
        f"target `{args.target}` must exist under `{edition.project_name}.outputs`.",
        "Add a dedicated acceptance target or pass an existing one with --target.",
    ):
        return
    report(
        "profile adapter",
        str(target.get("type", "")).lower() == edition.adapter_type,
        f"target type must be `{edition.adapter_type}` for the {edition.key} edition.",
        "Select the profile target that belongs to this edition.",
    )
    checker: Callable[..., None] = check_bigquery_target if edition.key == "bigquery" else check_snowflake_target
    checker(report, target, args, environ)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        epilog=OFFLINE_NOTICE + " See docs/cloud-acceptance-runbook.md.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--confirm-disposable-target",
        required=True,
        metavar="IDENTITY",
        help=(
            "Re-type the full identity to confirm it is disposable: PROJECT.DATASET for BigQuery, "
            "ACCOUNT/DATABASE.SCHEMA for Snowflake."
        ),
    )
    common.add_argument("--target", required=True, help="dbt profile target the acceptance run will use.")
    common.add_argument("--profiles-dir", help="dbt profiles directory; defaults to DBT_PROFILES_DIR.")
    common.add_argument("--workspace", type=Path, help="Edition dbt project directory; defaults to this checkout.")

    editions = parser.add_subparsers(dest="edition", required=True, metavar="{bigquery,snowflake}")
    bigquery = editions.add_parser("bigquery", parents=[common], help="Disposable BigQuery project and dataset.")
    bigquery.add_argument("--project", required=True, help="Dedicated non-production Google Cloud project ID.")
    bigquery.add_argument("--dataset", required=True, help="Dedicated starterkit_-prefixed dataset.")
    snowflake = editions.add_parser("snowflake", parents=[common], help="Disposable Snowflake database and schema.")
    snowflake.add_argument(
        "--account",
        required=True,
        help="Snowflake account identifier the selected target must resolve to (orgname-accountname or locator).",
    )
    snowflake.add_argument("--database", required=True, help="Terraform-created DBT_KIT_DEV..._DB or DBT_KIT_CI..._DB.")
    snowflake.add_argument("--schema", required=True, help="Matching <prefix>_SCHEMA.")
    return parser


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    environ = os.environ if environ is None else environ
    edition = EDITIONS[args.edition]
    report = Reporter()

    print(OFFLINE_NOTICE)
    identity = validate_identity(report, args)
    if identity is not None:
        report(
            "disposable confirmation",
            args.confirm_disposable_target == identity,
            "--confirm-disposable-target must exactly repeat the explicit identity.",
            "Re-type the identity exactly; this guards against a copied or stale target.",
        )

    workspace = (args.workspace or default_workspace(edition)).expanduser().resolve()
    check_edition(report, edition, workspace)
    check_profile(report, edition, args, environ)

    if identity is not None:
        print(f"Explicit acceptance identity ({edition.key}): {printable_identity(edition, args)}")
    print(f"Workspace: {display_path(workspace)}")
    if report.failures:
        print(f"Cloud acceptance preflight found {report.failures} item(s) to resolve. Nothing was contacted or changed.")
        return 1
    print(
        "Cloud acceptance preflight passed locally. This does not prove credentials, permissions, cost, "
        "or that the target is disposable. Follow docs/cloud-acceptance-runbook.md; dbt debug is the first "
        "command that contacts the warehouse."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
