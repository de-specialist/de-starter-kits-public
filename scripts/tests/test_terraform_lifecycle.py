"""Offline routing tests for the Terraform lifecycle wrapper."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPOSITORY_ROOT / "scripts" / "terraform_lifecycle.sh"
EDITIONS = {
    "bigquery": "Bigquery",
    "snowflake": "Snowflake",
}
BLOCKED_ENVIRONMENT_PREFIXES = ("TF_CLI_ARGS", "TF_VAR_")
BLOCKED_ENVIRONMENT_NAMES = (
    "TF_WORKSPACE",
    "TF_DATA_DIR",
    "TF_CLI_CONFIG_FILE",
    "TERRAFORM_CONFIG",
    "TF_REATTACH_PROVIDERS",
)


class TerraformLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.repo = self.root / "fixture-repository"
        self.scripts = self.repo / "scripts"
        self.scripts.mkdir(parents=True)
        self.wrapper = self.scripts / "terraform_lifecycle.sh"
        shutil.copyfile(WRAPPER, self.wrapper)
        self.wrapper.chmod(0o755)

        for edition in EDITIONS.values():
            module = self.repo / "editions" / edition / "infra" / "terraform"
            module.mkdir(parents=True)
            (module / "main.tf").write_text("# synthetic fixture\n", encoding="utf-8")
            (module / "versions.tf").write_text("# synthetic fixture\n", encoding="utf-8")
            (module / "terraform.tfvars").write_text("fixture = true\n", encoding="utf-8")

        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.call_log = self.root / "terraform-calls.jsonl"
        self.fake_terraform = self.bin_dir / "terraform"
        self.fake_terraform.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['TERRAFORM_CALL_LOG'], 'a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n",
            encoding="utf-8",
        )
        self.fake_terraform.chmod(0o755)

    def run_wrapper(
        self,
        *arguments: str,
        include_github_token: bool = True,
        extra_environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(BLOCKED_ENVIRONMENT_PREFIXES)
            and name not in BLOCKED_ENVIRONMENT_NAMES
        }
        environment["PATH"] = f"{self.bin_dir}{os.pathsep}{environment.get('PATH', '')}"
        environment["TERRAFORM_CALL_LOG"] = str(self.call_log)
        if include_github_token:
            # Synthetic sentinel only; Terraform is always replaced with the fake executable.
            environment["GITHUB_TOKEN"] = "synthetic-test-token"
        else:
            environment.pop("GITHUB_TOKEN", None)
        environment.update(extra_environment or {})
        return subprocess.run(
            [str(self.wrapper), *arguments],
            cwd=self.repo,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def read_calls(self) -> list[list[str]]:
        if not self.call_log.exists():
            return []
        return [json.loads(line) for line in self.call_log.read_text(encoding="utf-8").splitlines()]

    def test_allowed_edition_and_action_route_only_to_matching_module(self) -> None:
        for edition, directory in EDITIONS.items():
            for action in ("apply", "destroy"):
                with self.subTest(edition=edition, action=action):
                    self.call_log.unlink(missing_ok=True)
                    result = self.run_wrapper(edition, action)

                    self.assertEqual(result.returncode, 0, result.stderr)
                    calls = self.read_calls()
                    module_path = str(
                        self.repo / "editions" / directory / "infra" / "terraform"
                    )
                    self.assertEqual(
                        calls,
                        [
                            [f"-chdir={module_path}", "init", "-input=false", "-lockfile=readonly"],
                            [f"-chdir={module_path}", action, "-var-file=terraform.tfvars"],
                        ],
                    )
                    self.assertFalse(any("-auto-approve" in arg for call in calls for arg in call))

    def test_packaged_edition_layout_routes_to_local_infra_directory(self) -> None:
        shutil.rmtree(self.repo / "editions")
        (self.repo / "kit").mkdir()
        (self.repo / "kit" / "dbt_project.yml").write_text("name: snowflake_edition\n", encoding="utf-8")
        module = self.repo / "infra" / "terraform"
        module.mkdir(parents=True)
        (module / "main.tf").write_text("# synthetic fixture\n", encoding="utf-8")
        (module / "versions.tf").write_text("# synthetic fixture\n", encoding="utf-8")
        (module / "terraform.tfvars").write_text("fixture = true\n", encoding="utf-8")

        result = self.run_wrapper("snowflake", "apply")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.read_calls(),
            [
                [f"-chdir={module}", "init", "-input=false", "-lockfile=readonly"],
                [f"-chdir={module}", "apply", "-var-file=terraform.tfvars"],
            ],
        )

    def test_packaged_edition_rejects_wrong_edition_argument(self) -> None:
        shutil.rmtree(self.repo / "editions")
        (self.repo / "kit").mkdir()
        (self.repo / "kit" / "dbt_project.yml").write_text("name: snowflake_edition\n", encoding="utf-8")
        module = self.repo / "infra" / "terraform"
        module.mkdir(parents=True)
        (module / "main.tf").write_text("# synthetic fixture\n", encoding="utf-8")
        (module / "versions.tf").write_text("# synthetic fixture\n", encoding="utf-8")
        (module / "terraform.tfvars").write_text("fixture = true\n", encoding="utf-8")

        result = self.run_wrapper("bigquery", "apply")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("archive contents do not identify the bigquery edition", result.stderr)
        self.assertEqual(self.read_calls(), [])

    def test_invalid_edition_is_rejected_without_invoking_terraform(self) -> None:
        result = self.run_wrapper("redshift", "apply")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("edition must be bigquery or snowflake", result.stderr)
        self.assertEqual(self.read_calls(), [])

    def test_invalid_action_is_rejected_without_invoking_terraform(self) -> None:
        result = self.run_wrapper("snowflake", "plan")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("action must be apply or destroy", result.stderr)
        self.assertEqual(self.read_calls(), [])

    def test_missing_edition_tfvars_is_rejected_before_terraform(self) -> None:
        (self.repo / "editions" / "Bigquery" / "infra" / "terraform" / "terraform.tfvars").unlink()

        result = self.run_wrapper("bigquery", "apply")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("copy and edit this edition's local terraform.tfvars", result.stderr)
        self.assertEqual(self.read_calls(), [])

    def test_bigquery_requires_token_before_terraform(self) -> None:
        result = self.run_wrapper("bigquery", "apply", include_github_token=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GITHUB_TOKEN must be set", result.stderr)
        self.assertEqual(self.read_calls(), [])

    def test_ambient_terraform_environment_is_rejected_before_terraform(self) -> None:
        sentinel = "synthetic-sentinel-value"
        cases = {
            "TF_CLI_ARGS": "-auto-approve",
            "TF_CLI_ARGS_destroy": "-auto-approve",
            "TF_CLI_ARGS_apply": "-auto-approve",
            "TF_CLI_ARGS_init": "-upgrade",
            "TF_WORKSPACE": "other",
            "TF_DATA_DIR": str(self.root / "other-data-dir"),
            "TF_CLI_CONFIG_FILE": str(self.root / "terraformrc"),
            "TERRAFORM_CONFIG": str(self.root / "terraformrc"),
            "TF_REATTACH_PROVIDERS": "{}",
            "TF_VAR_dataset_id": sentinel,
        }
        for name, value in cases.items():
            for action in ("apply", "destroy"):
                with self.subTest(name=name, action=action):
                    self.call_log.unlink(missing_ok=True)
                    result = self.run_wrapper(
                        "snowflake", action, extra_environment={name: value}
                    )

                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("unset these environment variables", result.stderr)
                    self.assertIn(name, result.stderr)
                    self.assertNotIn(sentinel, result.stderr)
                    self.assertEqual(self.read_calls(), [])

    def test_empty_ambient_terraform_variable_is_still_rejected(self) -> None:
        result = self.run_wrapper(
            "bigquery", "destroy", extra_environment={"TF_CLI_ARGS_destroy": ""}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TF_CLI_ARGS_destroy", result.stderr)
        self.assertEqual(self.read_calls(), [])

    def test_unrelated_tf_prefixed_variable_is_allowed(self) -> None:
        result = self.run_wrapper(
            "snowflake", "apply", extra_environment={"TF_LOG": "WARN"}
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.read_calls()), 2)


if __name__ == "__main__":
    unittest.main()
