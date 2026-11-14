"""Offline tests for the cloud acceptance preflight.

Every run uses synthetic fixtures, a temporary HOME, fake dbt/Terraform/warehouse CLIs that
only record invocations, and a socket tripwire. No cloud service is contacted.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "cloud_acceptance_preflight.py"
OFFLINE_NOTICE = "makes no network call, does not run dbt or any warehouse client, and never plans or applies Terraform"
GUARDED_COMMANDS = ("dbt", "terraform", "bq", "gcloud", "snowsql", "snow")

try:
    import yaml  # noqa: F401  # The preflight needs PyYAML, which dbt-core and the test lock provide.
except ImportError as error:  # pragma: no cover - exercised only in incomplete environments
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"}:
        raise
    raise unittest.SkipTest(f"PyYAML unavailable: {error}") from error

# Synthetic sentinel values. None of them may ever appear in preflight output.
SECRETS = {
    "SNOWFLAKE_ACCOUNT": "sentinelorg-account_91c2",
    "SNOWFLAKE_USER": "sentinel-user-44be",
    "SNOWFLAKE_PASSWORD": "sentinel-password-7f3a",
    "SNOWFLAKE_WAREHOUSE": "SENTINEL_WAREHOUSE_5D1E",
    "SNOWFLAKE_ROLE": "SENTINEL_ROLE_2C9B",
}

SF_ACCOUNT = SECRETS["SNOWFLAKE_ACCOUNT"]
SF_OTHER_ACCOUNT = "otherorg-sentinel_other_5a7e"
SF_DB = "DBT_KIT_CI_ACCEPT_DB"
SF_SCHEMA = "DBT_KIT_CI_ACCEPT_SCHEMA"
BQ_PROJECT = "starter-accept-1234"
BQ_DATASET = "starterkit_accept"

BIGQUERY_PROFILE = """\
retail_analytics:
  target: accept
  outputs:
    accept:
      type: bigquery
      method: oauth
      project: "{{ env_var('BQ_PROJECT') }}"
      dataset: "{{ env_var('BQ_DATASET', 'dbt_retail') }}"
      location: "{{ env_var('BQ_LOCATION', 'US') }}"
      threads: 4
      maximum_bytes_billed: 1000000000
"""

SNOWFLAKE_PROFILE = """\
reliable_pipeline:
  target: accept
  outputs:
    accept:
      type: snowflake
      account: "{{ env_var('SNOWFLAKE_ACCOUNT') }}"
      user: "{{ env_var('SNOWFLAKE_USER') }}"
      password: "{{ env_var('SNOWFLAKE_PASSWORD') }}"
      role: "{{ env_var('SNOWFLAKE_ROLE', 'TRANSFORMER') }}"
      database: "{{ env_var('SNOWFLAKE_DATABASE') }}"
      warehouse: "{{ env_var('SNOWFLAKE_WAREHOUSE') }}"
      schema: "{{ env_var('SNOWFLAKE_SCHEMA', 'DBT_DEV') }}"
      threads: 4
"""

SITECUSTOMIZE = """\
import os, socket

def _blocked(*args, **kwargs):
    with open(os.environ["NETWORK_CALL_LOG"], "a", encoding="utf-8") as stream:
        stream.write("network attempt\\n")
    raise OSError("network access is blocked in cloud acceptance preflight tests")

socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
"""


def bigquery_args(project: str = BQ_PROJECT, dataset: str = BQ_DATASET, confirm: str | None = None) -> list[str]:
    return [
        "bigquery",
        "--project", project,
        "--dataset", dataset,
        "--confirm-disposable-target", confirm or f"{project}.{dataset}",
        "--target", "accept",
    ]


def snowflake_args(
    database: str = SF_DB,
    schema: str = SF_SCHEMA,
    confirm: str | None = None,
    account: str = SF_ACCOUNT,
    target: str = "accept",
) -> list[str]:
    return [
        "snowflake",
        f"--account={account}",
        "--database", database,
        "--schema", schema,
        "--confirm-disposable-target", confirm or f"{account}/{database}.{schema}",
        "--target", target,
    ]


class CloudAcceptancePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)

        self.repo = self.root / "fixture-repository"
        (self.repo / "scripts").mkdir(parents=True)
        self.script = self.repo / "scripts" / "cloud_acceptance_preflight.py"
        shutil.copyfile(SCRIPT, self.script)
        self._write_edition(self.repo / "editions" / "Bigquery", "retail_analytics", self.repo / "editions" / "Bigquery")
        self._write_edition(self.repo / "editions" / "Snowflake" / "kit", "reliable_pipeline", self.repo / "editions" / "Snowflake")

        self.home = self.root / "home"
        self.home.mkdir()
        self.profiles_dir = self.root / "profiles"
        self.profiles_dir.mkdir()

        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.command_log = self.root / "commands.jsonl"
        for command in GUARDED_COMMANDS:
            shim = self.bin_dir / command
            shim.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "with open(os.environ['COMMAND_LOG'], 'a', encoding='utf-8') as stream:\n"
                "    stream.write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + '\\n')\n",
                encoding="utf-8",
            )
            shim.chmod(0o755)

        self.tripwire_dir = self.root / "tripwire"
        self.tripwire_dir.mkdir()
        (self.tripwire_dir / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
        self.network_log = self.root / "network.log"

    @staticmethod
    def _write_edition(project_dir: Path, name: str, requirements_dir: Path) -> None:
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "dbt_project.yml").write_text(f"name: {name}\nprofile: {name}\n", encoding="utf-8")
        (project_dir / "profiles.example.yml").write_text("# synthetic fixture\n", encoding="utf-8")
        (requirements_dir / "requirements.txt").write_text("# synthetic fixture\n", encoding="utf-8")
        (requirements_dir / "requirements-lock.txt").write_text("# synthetic fixture\n", encoding="utf-8")

    def write_profile(self, content: str) -> None:
        (self.profiles_dir / "profiles.yml").write_text(content, encoding="utf-8")

    def bigquery_env(self, **overrides: str | None) -> dict[str, str | None]:
        return {"BQ_PROJECT": BQ_PROJECT, "BQ_DATASET": BQ_DATASET, **overrides}

    def snowflake_env(self, **overrides: str | None) -> dict[str, str | None]:
        return {**SECRETS, "SNOWFLAKE_DATABASE": SF_DB, "SNOWFLAKE_SCHEMA": SF_SCHEMA, **overrides}

    def run_preflight(
        self,
        arguments: list[str],
        extra_env: dict[str, str | None] | None = None,
        *,
        script: Path | None = None,
        profiles_flag: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.home),
            "PYTHONPATH": str(self.tripwire_dir),
            "COMMAND_LOG": str(self.command_log),
            "NETWORK_CALL_LOG": str(self.network_log),
        }
        # A shared-library Python (setup-python's toolcache build) needs this to start.
        if "LD_LIBRARY_PATH" in os.environ:
            environment["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
        for key, value in (extra_env or {}).items():
            if value is not None:
                environment[key] = value
        if profiles_flag:
            arguments = [*arguments, "--profiles-dir", str(self.profiles_dir)]
        result = subprocess.run(
            [sys.executable, str(script or self.script), *arguments],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assert_offline(result)
        return result

    def assert_offline(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertFalse(self.command_log.exists(), "preflight invoked dbt, Terraform, or a warehouse CLI")
        self.assertFalse(self.network_log.exists(), "preflight attempted a network connection")
        output = result.stdout + result.stderr
        for value in SECRETS.values():
            self.assertNotIn(value, output, "preflight printed a configured value")

    def assert_ready(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(OFFLINE_NOTICE, result.stdout)
        self.assertNotIn("NEEDS ACTION", result.stdout)
        self.assertIn("preflight passed locally", result.stdout)

    def assert_blocked(self, result: subprocess.CompletedProcess[str], *fragments: str) -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(OFFLINE_NOTICE, result.stdout)
        self.assertIn("Nothing was contacted or changed", result.stdout)
        for fragment in fragments:
            self.assertIn(fragment, result.stdout)

    # --- Ready paths -------------------------------------------------------------------

    def test_bigquery_disposable_target_is_ready(self) -> None:
        self.write_profile(BIGQUERY_PROFILE)
        result = self.run_preflight(bigquery_args(), self.bigquery_env())
        self.assert_ready(result)
        self.assertIn(f"Explicit acceptance identity (bigquery): {BQ_PROJECT}.{BQ_DATASET}", result.stdout)

    def test_snowflake_password_target_is_ready_without_printing_values(self) -> None:
        self.write_profile(SNOWFLAKE_PROFILE)
        result = self.run_preflight(snowflake_args(), self.snowflake_env())
        self.assert_ready(result)
        self.assertIn(f"Explicit acceptance identity (snowflake): {SF_DB}.{SF_SCHEMA}", result.stdout)
        self.assertIn("PASS profile account: resolves to the explicit --account identity.", result.stdout)

    def test_snowflake_key_pair_target_checks_key_file_without_printing_path(self) -> None:
        key_path = self.root / "sentinel-key-path-0e4d.p8"
        key_path.write_text("synthetic placeholder, never a real key\n", encoding="utf-8")
        profile = SNOWFLAKE_PROFILE.replace(
            "      password: \"{{ env_var('SNOWFLAKE_PASSWORD') }}\"\n",
            "      authenticator: SNOWFLAKE_JWT\n"
            "      private_key_path: \"{{ env_var('SNOWFLAKE_PRIVATE_KEY_PATH') }}\"\n",
        )
        self.write_profile(profile)

        result = self.run_preflight(
            snowflake_args(), self.snowflake_env(SNOWFLAKE_PASSWORD=None, SNOWFLAKE_PRIVATE_KEY_PATH=str(key_path))
        )
        self.assert_ready(result)
        self.assertNotIn(key_path.name, result.stdout)

        key_path.unlink()
        self.assert_blocked(
            self.run_preflight(snowflake_args(), self.snowflake_env(SNOWFLAKE_PRIVATE_KEY_PATH=str(key_path))),
            "referenced file does not exist",
        )

    def test_product_in_a_name_is_not_mistaken_for_production(self) -> None:
        self.write_profile(BIGQUERY_PROFILE)
        dataset = "starterkit_product_accept"
        self.assert_ready(self.run_preflight(bigquery_args(dataset=dataset), self.bigquery_env(BQ_DATASET=dataset)))

    def test_repository_profile_examples_are_accepted_for_explicit_targets(self) -> None:
        shutil.copyfile(REPOSITORY_ROOT / "editions" / "Bigquery" / "profiles.example.yml", self.profiles_dir / "profiles.yml")
        args = [*bigquery_args()]
        args[args.index("accept")] = "dev"
        self.assert_ready(self.run_preflight(args, self.bigquery_env(), script=SCRIPT))

        shutil.copyfile(REPOSITORY_ROOT / "editions" / "Snowflake" / "kit" / "profiles.example.yml", self.profiles_dir / "profiles.yml")
        args = [*snowflake_args()]
        args[args.index("accept")] = "dev"
        self.assert_ready(self.run_preflight(args, self.snowflake_env(SNOWFLAKE_SCHEMA=SF_SCHEMA), script=SCRIPT))

        # The example has no schema fallback; with SNOWFLAKE_SCHEMA unset the schema cannot
        # resolve to --schema and must be refused.
        self.assert_blocked(
            self.run_preflight(args, self.snowflake_env(SNOWFLAKE_SCHEMA=None), script=SCRIPT),
            "NEEDS ACTION profile schema: `schema` from SNOWFLAKE_SCHEMA is not set and has no default",
            "Make the selected target resolve `schema` to the --schema value.",
        )

    # --- Explicit identity --------------------------------------------------------------

    def test_identity_arguments_are_required(self) -> None:
        self.write_profile(SNOWFLAKE_PROFILE)
        cases = {
            "bigquery without dataset": ["bigquery", "--project", BQ_PROJECT, "--confirm-disposable-target", "x", "--target", "accept"],
            "snowflake without schema": ["snowflake", "--account", SF_ACCOUNT, "--database", SF_DB, "--confirm-disposable-target", "x", "--target", "accept"],
            "snowflake without account": ["snowflake", "--database", SF_DB, "--schema", SF_SCHEMA, "--confirm-disposable-target", "x", "--target", "accept"],
            "missing confirmation": ["snowflake", "--database", SF_DB, "--schema", SF_SCHEMA, "--target", "accept"],
            "missing target": ["snowflake", "--database", SF_DB, "--schema", SF_SCHEMA, "--confirm-disposable-target", "x"],
            "missing edition": [],
        }
        for label, arguments in cases.items():
            with self.subTest(label):
                result = self.run_preflight(
                    arguments,
                    self.snowflake_env(),
                    profiles_flag=label != "missing edition",
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("required", result.stderr)

    def test_dbt_dev_identities_are_rejected(self) -> None:
        self.write_profile(SNOWFLAKE_PROFILE)
        cases = {
            "database and schema": ("DBT_DEV", "DBT_DEV"),
            "dbt_dev schema under a kit database": (SF_DB, "DBT_DEV"),
            "embedded in prefix": ("DBT_KIT_DEV_DBT_DEV_DB", "DBT_KIT_DEV_DBT_DEV_SCHEMA"),
        }
        for label, (database, schema) in cases.items():
            with self.subTest(label):
                result = self.run_preflight(
                    snowflake_args(database, schema),
                    self.snowflake_env(SNOWFLAKE_DATABASE=database, SNOWFLAKE_SCHEMA=schema),
                )
                self.assert_blocked(result, "NEEDS ACTION identity")
                self.assertNotIn("Explicit acceptance identity", result.stdout)
        self.assertIn(
            "contains DBT_DEV",
            self.run_preflight(snowflake_args("DBT_KIT_DEV_DBT_DEV_DB", "DBT_KIT_DEV_DBT_DEV_SCHEMA"), self.snowflake_env()).stdout,
        )

        self.write_profile(BIGQUERY_PROFILE)
        result = self.run_preflight(bigquery_args(dataset="starterkit_dbt_dev"), self.bigquery_env(BQ_DATASET="starterkit_dbt_dev"))
        self.assert_blocked(result, "contains DBT_DEV")

    def test_production_like_identities_are_rejected_without_echoing_them(self) -> None:
        self.write_profile(BIGQUERY_PROFILE)
        for project, dataset in (
            ("acme-prod-zq81", BQ_DATASET),
            ("acme-production-zq81", BQ_DATASET),
            ("acme-prod2-zq81", BQ_DATASET),
            ("acme-live-zq81", BQ_DATASET),
            (BQ_PROJECT, "starterkit_main_zq81"),
            (BQ_PROJECT, "starterkit_customer_zq81"),
        ):
            with self.subTest(project=project, dataset=dataset):
                result = self.run_preflight(bigquery_args(project, dataset), self.bigquery_env())
                self.assert_blocked(result, "looks production-like")
                rejected = project if project != BQ_PROJECT else dataset
                self.assertNotIn(rejected, result.stdout, "a rejected identity was echoed")

        self.write_profile(SNOWFLAKE_PROFILE)
        for database, schema in (
            ("DBT_KIT_CI_PROD_DB", "DBT_KIT_CI_PROD_SCHEMA"),
            ("DBT_KIT_DEV_LIVE_DB", "DBT_KIT_DEV_LIVE_SCHEMA"),
            ("DBT_KIT_CI_SHARED_DB", "DBT_KIT_CI_SHARED_SCHEMA"),
        ):
            with self.subTest(database=database):
                self.assert_blocked(self.run_preflight(snowflake_args(database, schema), self.snowflake_env()), "looks production-like")

    def test_names_outside_the_disposable_contract_are_rejected(self) -> None:
        self.write_profile(BIGQUERY_PROFILE)
        for project, dataset, fragment in (
            ("bigquery-public-data", BQ_DATASET, "public source project"),
            (BQ_PROJECT, "dbt_retail", "starterkit_-prefixed"),
            (BQ_PROJECT, "analytics", "starterkit_-prefixed"),
            ("Bad_Project", BQ_DATASET, "valid Google Cloud project ID"),
        ):
            with self.subTest(project=project, dataset=dataset):
                self.assert_blocked(self.run_preflight(bigquery_args(project, dataset), self.bigquery_env()), fragment)

        self.write_profile(SNOWFLAKE_PROFILE)
        for database, schema, fragment in (
            ("ANALYTICS", "DBT_KIT_CI_ACCEPT_SCHEMA", "Terraform-style"),
            ("dbt_kit_ci_accept_db", SF_SCHEMA, "Terraform-style"),
            (SF_DB, "DBT_KIT_CI_OTHER_SCHEMA", "_SCHEMA suffix"),
            (SF_DB, "CI", "_SCHEMA suffix"),
        ):
            with self.subTest(database=database, schema=schema):
                self.assert_blocked(self.run_preflight(snowflake_args(database, schema), self.snowflake_env()), fragment)

    def test_confirmation_must_repeat_the_identity_exactly(self) -> None:
        self.write_profile(SNOWFLAKE_PROFILE)
        for confirm in (
            SF_DB,
            f"{SF_DB}.{SF_SCHEMA}",  # the pre-account form no longer confirms a Snowflake target
            f"{SF_ACCOUNT}/{SF_DB}.DBT_KIT_CI_OTHER_SCHEMA",
            f"{SF_ACCOUNT}/{SF_DB.lower()}.{SF_SCHEMA.lower()}",
            f"{SF_OTHER_ACCOUNT}/{SF_DB}.{SF_SCHEMA}",
            f"{SF_ACCOUNT.upper()}/{SF_DB}.{SF_SCHEMA}",
            f"{SF_ACCOUNT}.{SF_DB}.{SF_SCHEMA}",
        ):
            with self.subTest(confirm=confirm):
                self.assert_blocked(
                    self.run_preflight(snowflake_args(confirm=confirm), self.snowflake_env()),
                    "NEEDS ACTION disposable confirmation",
                )

    # --- Snowflake account binding ------------------------------------------------------

    def test_snowflake_account_must_match_the_selected_target(self) -> None:
        self.write_profile(SNOWFLAKE_PROFILE)
        result = self.run_preflight(snowflake_args(), self.snowflake_env(SNOWFLAKE_ACCOUNT=SF_OTHER_ACCOUNT))
        self.assert_blocked(result, "NEEDS ACTION profile account", "from SNOWFLAKE_ACCOUNT", "--account", "value not printed")
        self.assertNotIn(SF_OTHER_ACCOUNT, result.stdout + result.stderr)

        # Snowflake account identifiers are case-insensitive.
        for variant in (SF_ACCOUNT.upper(), SF_ACCOUNT.lower()):
            with self.subTest(variant=variant):
                self.assert_ready(self.run_preflight(snowflake_args(), self.snowflake_env(SNOWFLAKE_ACCOUNT=variant)))

        # Separator variants are different identifiers to this check; never normalize them away.
        self.assert_blocked(
            self.run_preflight(snowflake_args(), self.snowflake_env(SNOWFLAKE_ACCOUNT=SF_ACCOUNT.replace("_", "-"))),
            "NEEDS ACTION profile account",
        )

    def test_snowflake_account_binding_uses_the_selected_target_only(self) -> None:
        other_target = (
            "    other:\n"
            "      type: snowflake\n"
            f"      account: {SF_OTHER_ACCOUNT}\n"
            "      user: \"{{ env_var('SNOWFLAKE_USER') }}\"\n"
            "      password: \"{{ env_var('SNOWFLAKE_PASSWORD') }}\"\n"
            "      role: \"{{ env_var('SNOWFLAKE_ROLE', 'TRANSFORMER') }}\"\n"
            "      database: \"{{ env_var('SNOWFLAKE_DATABASE') }}\"\n"
            "      warehouse: \"{{ env_var('SNOWFLAKE_WAREHOUSE') }}\"\n"
            "      schema: \"{{ env_var('SNOWFLAKE_SCHEMA') }}\"\n"
        )
        self.write_profile(SNOWFLAKE_PROFILE + other_target)
        self.assert_ready(self.run_preflight(snowflake_args(), self.snowflake_env()))

        result = self.run_preflight(snowflake_args(target="other"), self.snowflake_env())
        self.assert_blocked(result, "NEEDS ACTION profile account", "hard-coded literal")
        self.assertNotIn(SF_OTHER_ACCOUNT, result.stdout + result.stderr)
        self.assert_ready(
            self.run_preflight(snowflake_args(target="other", account=SF_OTHER_ACCOUNT), self.snowflake_env())
        )

    def test_snowflake_account_default_missing_or_unresolvable_fails_closed(self) -> None:
        self.write_profile(SNOWFLAKE_PROFILE.replace(
            "env_var('SNOWFLAKE_ACCOUNT')", f"env_var('SNOWFLAKE_ACCOUNT', '{SF_OTHER_ACCOUNT}')"
        ))
        result = self.run_preflight(snowflake_args(), self.snowflake_env(SNOWFLAKE_ACCOUNT=None))
        self.assert_blocked(result, "env_var() default (SNOWFLAKE_ACCOUNT is unset)")
        self.assertNotIn(SF_OTHER_ACCOUNT, result.stdout + result.stderr)

        self.write_profile(SNOWFLAKE_PROFILE.replace("      account: \"{{ env_var('SNOWFLAKE_ACCOUNT') }}\"\n", ""))
        self.assert_blocked(
            self.run_preflight(snowflake_args(), self.snowflake_env()), "`account` is missing from the selected target"
        )

        self.write_profile(SNOWFLAKE_PROFILE.replace(
            "env_var('SNOWFLAKE_ACCOUNT') }}", "env_var('SNOWFLAKE_ACCOUNT') | lower }}"
        ))
        self.assert_blocked(self.run_preflight(snowflake_args(), self.snowflake_env()), "`account` uses Jinja")

    def test_snowflake_account_syntax_is_validated_without_echoing(self) -> None:
        self.write_profile(SNOWFLAKE_PROFILE)
        for account in (
            "",
            "https://sentinelorg-bad_1.snowflakecomputing.com",
            "sentinelorg-bad_2.snowflakecomputing.com",
            "xy91c2e.us-east-1.snowflakecomputing.com",
            "sentinel_org-bad_3",
            "sentinelorg-bad-4",
            "9sentinelorg-bad_5",
            "sentinelorg-bad_6;drop",
            "sentinelorg-bad_7\n",
            "sentinelorg-bad_8 ",
            "sentinelorg-bad_9/" + SF_DB,
            "x" * 256,
        ):
            with self.subTest(account=account):
                result = self.run_preflight(
                    snowflake_args(account=account), self.snowflake_env(SNOWFLAKE_ACCOUNT=account)
                )
                self.assert_blocked(result, "NEEDS ACTION identity --account", "Snowflake account identifier")
                self.assertNotIn("Explicit acceptance identity", result.stdout)
                if account.strip():
                    self.assertNotIn(account.strip(), result.stdout + result.stderr, "a rejected account was echoed")

    def test_snowflake_account_identifier_forms_are_accepted(self) -> None:
        self.write_profile(SNOWFLAKE_PROFILE)
        for account in (
            "sentinelorg-accept_acct",
            "sentinelorg-accept_acct.privatelink",
            "xy91c2e",
            "xy91c2e.us-east-2.aws",
            "xy91c2e.east-us-2.azure",
            "xy91c2e.us-west-2.privatelink",
        ):
            with self.subTest(account=account):
                result = self.run_preflight(
                    snowflake_args(account=account), self.snowflake_env(SNOWFLAKE_ACCOUNT=account)
                )
                self.assert_ready(result)
                self.assertNotIn(account, result.stdout + result.stderr, "the account identity was printed")

    # --- Profile resolution -------------------------------------------------------------

    def test_profile_must_resolve_to_the_explicit_identity(self) -> None:
        self.write_profile(SNOWFLAKE_PROFILE)
        result = self.run_preflight(snowflake_args(), self.snowflake_env(SNOWFLAKE_DATABASE="DBT_KIT_CI_OTHER_DB"))
        self.assert_blocked(result, "NEEDS ACTION profile database", "from SNOWFLAKE_DATABASE", "value not printed")
        self.assertNotIn("DBT_KIT_CI_OTHER_DB", result.stdout)

        hard_coded = SNOWFLAKE_PROFILE.replace(
            "\"{{ env_var('SNOWFLAKE_DATABASE') }}\"", "DBT_DEV"
        ).replace("\"{{ env_var('SNOWFLAKE_SCHEMA', 'DBT_DEV') }}\"", "DBT_DEV")
        self.write_profile(hard_coded)
        self.assert_blocked(
            self.run_preflight(snowflake_args(), self.snowflake_env()),
            "hard-coded literal",
            "DBT_DEV or production-like",
        )

        self.write_profile(BIGQUERY_PROFILE)
        self.assert_blocked(
            self.run_preflight(bigquery_args(), self.bigquery_env(BQ_DATASET=None)),
            "env_var() default (BQ_DATASET is unset)",
        )

    def test_missing_required_configuration_is_named_but_not_printed(self) -> None:
        self.write_profile(SNOWFLAKE_PROFILE)
        for variable in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_DATABASE"):
            with self.subTest(variable=variable):
                result = self.run_preflight(snowflake_args(), self.snowflake_env(**{variable: None}))
                self.assert_blocked(result, f"from {variable} is not set and has no default")

        self.write_profile(BIGQUERY_PROFILE)
        self.assert_blocked(
            self.run_preflight(bigquery_args(), self.bigquery_env(BQ_PROJECT=None)),
            "from BQ_PROJECT is not set and has no default",
        )

    def test_snowflake_admin_or_implicit_role_is_rejected(self) -> None:
        self.write_profile(SNOWFLAKE_PROFILE)
        for role in ("ACCOUNTADMIN", "sysadmin", "PUBLIC"):
            with self.subTest(role=role):
                self.assert_blocked(
                    self.run_preflight(snowflake_args(), self.snowflake_env(SNOWFLAKE_ROLE=role)),
                    "built-in admin or PUBLIC role is refused",
                )
        self.write_profile(SNOWFLAKE_PROFILE.replace("      role: \"{{ env_var('SNOWFLAKE_ROLE', 'TRANSFORMER') }}\"\n", ""))
        self.assert_blocked(self.run_preflight(snowflake_args(), self.snowflake_env()), "implicit default role")

    def test_bigquery_location_and_cost_guard_are_required(self) -> None:
        self.write_profile(BIGQUERY_PROFILE)
        self.assert_blocked(
            self.run_preflight(bigquery_args(), self.bigquery_env(BQ_LOCATION="EU")), "NEEDS ACTION profile location"
        )
        for replacement in ("", "      maximum_bytes_billed: 50000000000\n", "      maximum_bytes_billed: 0\n"):
            with self.subTest(replacement=replacement):
                self.write_profile(BIGQUERY_PROFILE.replace("      maximum_bytes_billed: 1000000000\n", replacement))
                self.assert_blocked(self.run_preflight(bigquery_args(), self.bigquery_env()), "NEEDS ACTION cost guard")

    def test_unresolvable_jinja_fails_closed(self) -> None:
        self.write_profile(SNOWFLAKE_PROFILE.replace(
            "\"{{ env_var('SNOWFLAKE_DATABASE') }}\"", "\"{{ env_var('SNOWFLAKE_DATABASE') | upper }}\""
        ))
        self.assert_blocked(self.run_preflight(snowflake_args(), self.snowflake_env()), "cannot resolve")

    def test_templated_auth_mode_is_resolved_and_unresolvable_fails_closed(self) -> None:
        profile = SNOWFLAKE_PROFILE.replace(
            "      password: \"{{ env_var('SNOWFLAKE_PASSWORD') }}\"\n",
            "      authenticator: \"{{ env_var('SNOWFLAKE_AUTHENTICATOR', 'snowflake') }}\"\n"
            "      password: \"{{ env_var('SNOWFLAKE_PASSWORD') }}\"\n",
        )
        self.write_profile(profile)
        self.assert_ready(self.run_preflight(snowflake_args(), self.snowflake_env()))
        # The env_var() default selects password auth, so a missing password must be caught.
        self.assert_blocked(
            self.run_preflight(snowflake_args(), self.snowflake_env(SNOWFLAKE_PASSWORD=None)),
            "NEEDS ACTION credentials: `password` from SNOWFLAKE_PASSWORD is not set",
        )

        self.write_profile(profile.replace("{{ env_var('SNOWFLAKE_AUTHENTICATOR', 'snowflake') }}", "{{ var('auth') }}"))
        self.assert_blocked(
            self.run_preflight(snowflake_args(), self.snowflake_env()),
            "NEEDS ACTION credentials: `authenticator` uses Jinja",
        )

    def test_bigquery_execution_project_must_be_the_disposable_project(self) -> None:
        profile = BIGQUERY_PROFILE + "      execution_project: \"{{ env_var('BQ_EXECUTION_PROJECT') }}\"\n"
        self.write_profile(profile)
        self.assert_ready(
            self.run_preflight(bigquery_args(), self.bigquery_env(BQ_EXECUTION_PROJECT=BQ_PROJECT))
        )
        result = self.run_preflight(bigquery_args(), self.bigquery_env(BQ_EXECUTION_PROJECT="billing-sentinel-8e21"))
        self.assert_blocked(result, "NEEDS ACTION profile execution_project")
        self.assertNotIn("billing-sentinel-8e21", result.stdout)
        self.assert_blocked(
            self.run_preflight(bigquery_args(), self.bigquery_env()),
            "`execution_project` from BQ_EXECUTION_PROJECT is not set",
        )

    def test_profiles_location_and_structure_are_required(self) -> None:
        self.write_profile(SNOWFLAKE_PROFILE)
        self.assert_blocked(
            self.run_preflight(snowflake_args(), self.snowflake_env(), profiles_flag=False),
            "NEEDS ACTION profiles directory",
        )
        self.assert_ready(
            self.run_preflight(snowflake_args(), self.snowflake_env(DBT_PROFILES_DIR=str(self.profiles_dir)), profiles_flag=False)
        )

        missing_target = [*snowflake_args()]
        missing_target[missing_target.index("accept")] = "prod"
        self.assert_blocked(self.run_preflight(missing_target, self.snowflake_env()), "NEEDS ACTION profile target")

        self.write_profile(BIGQUERY_PROFILE)
        self.assert_blocked(self.run_preflight(snowflake_args(), self.snowflake_env()), "NEEDS ACTION profile key")

        self.write_profile(SNOWFLAKE_PROFILE.replace("type: snowflake", "type: bigquery"))
        self.assert_blocked(self.run_preflight(snowflake_args(), self.snowflake_env()), "NEEDS ACTION profile adapter")

        (self.profiles_dir / "profiles.yml").unlink()
        self.assert_blocked(self.run_preflight(snowflake_args(), self.snowflake_env()), "NEEDS ACTION profiles file")

    def test_malformed_profile_does_not_leak_contents(self) -> None:
        self.write_profile(
            "reliable_pipeline:\n  outputs:\n    accept:\n      password: sentinel-password-7f3a\n     bad: [\n"
        )
        result = self.run_preflight(snowflake_args(), self.snowflake_env())
        self.assert_blocked(result, "could not be read as YAML (contents not printed)")

    # --- Edition selection --------------------------------------------------------------

    def test_selected_edition_must_exist_and_match(self) -> None:
        self.write_profile(SNOWFLAKE_PROFILE)
        shutil.rmtree(self.repo / "editions" / "Snowflake")
        self.assert_blocked(self.run_preflight(snowflake_args(), self.snowflake_env()), "NEEDS ACTION edition project")

        self.write_profile(SNOWFLAKE_PROFILE)
        bigquery_workspace = self.repo / "editions" / "Bigquery"
        self.assert_blocked(
            self.run_preflight([*snowflake_args(), "--workspace", str(bigquery_workspace)], self.snowflake_env()),
            "NEEDS ACTION edition identity",
        )

        self.write_profile(BIGQUERY_PROFILE)
        (bigquery_workspace / "requirements-lock.txt").unlink()
        self.assert_blocked(self.run_preflight(bigquery_args(), self.bigquery_env()), "NEEDS ACTION edition dependencies")

    # --- Offline guarantees -------------------------------------------------------------

    def test_script_imports_no_network_or_process_modules(self) -> None:
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = {
            "socket", "ssl", "http", "urllib", "requests", "httpx", "subprocess", "shutil", "asyncio",
            "google", "snowflake", "dbt", "pty", "multiprocessing",
        }
        self.assertFalse(imported & forbidden, f"unexpected imports: {sorted(imported & forbidden)}")
        source = SCRIPT.read_text(encoding="utf-8")
        for call in ("os.system", "os.popen", "os.exec", "os.spawn", "os.fork"):
            self.assertNotIn(call, source)

    def test_help_states_offline_and_no_terraform_guarantee(self) -> None:
        result = self.run_preflight(["--help"], profiles_flag=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("never plans or applies Terraform", " ".join(result.stdout.split()))
        self.assertIn("no network call", " ".join(result.stdout.split()))

    def test_runbook_documents_the_offline_preflight(self) -> None:
        runbook = (REPOSITORY_ROOT / "docs" / "cloud-acceptance-runbook.md").read_text(encoding="utf-8")
        self.assertIn("scripts/cloud_acceptance_preflight.py", runbook)
        self.assertIn("makes no network call", runbook)
        self.assertIn("never plans or applies Terraform", runbook)
        self.assertIn("DBT_DEV", runbook)
        self.assertIn("--account", runbook)
        self.assertIn("ACCOUNT/DATABASE.SCHEMA", runbook)
        nav = (REPOSITORY_ROOT / "website" / "mkdocs.yml").read_text(encoding="utf-8")
        self.assertIn("docs/cloud-acceptance-runbook.md", nav)


if __name__ == "__main__":
    unittest.main()
