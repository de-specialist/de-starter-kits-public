"""Offline integration coverage for edition archive packaging."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_NAMES = ("package_edition.py", "credential_safety.py", "first_run_preflight.py", "terraform_lifecycle.sh")


class PackageEditionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        temporary_root = Path(self.temporary_directory.name)
        self.repo = temporary_root / "fixture-repository"
        self.source = self.repo / "editions" / "Bigquery"
        self.source.mkdir(parents=True)
        self.snowflake_source = self.repo / "editions" / "Snowflake"
        self.snowflake_source.mkdir(parents=True)
        scripts = self.repo / "scripts"
        scripts.mkdir()
        for name in SCRIPT_NAMES:
            shutil.copyfile(REPOSITORY_ROOT / "scripts" / name, scripts / name)
        self.output_directory = temporary_root / "delivery"
        self.output_directory.mkdir()
        self.archive = self.output_directory / "edition.zip"

    def write_source(self, relative_path: str, content: str = "fixture content\n") -> None:
        path = self.source / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def package(
        self, edition: str = "bigquery", archive: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        output_archive = archive or self.archive
        return subprocess.run(
            [
                sys.executable,
                str(self.repo / "scripts" / "package_edition.py"),
                edition,
                str(output_archive),
            ],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_archive_contains_payload_and_valid_manifest_and_checksum(self) -> None:
        edition_payloads = {
            "README.md": "fixture edition readme\n",
            "profiles.example.yml": "fixture profile template\n",
            "models/marts/example.sql": "select 1 as fixture_value\n",
        }
        for relative_path, content in edition_payloads.items():
            self.write_source(relative_path, content)
        # Repository helper scripts are bundled by the packager, not the edition.
        payloads = {
            **edition_payloads,
            "scripts/first_run_preflight.py": (
                REPOSITORY_ROOT / "scripts" / "first_run_preflight.py"
            ).read_text(encoding="utf-8"),
            "scripts/terraform_lifecycle.sh": (
                REPOSITORY_ROOT / "scripts" / "terraform_lifecycle.sh"
            ).read_text(encoding="utf-8"),
        }

        private_ledger = self.repo / ".agents" / "task-queue.json"
        private_ledger.parent.mkdir(parents=True)
        private_ledger.write_text('{"private_fixture": "DO_NOT_PUBLISH_LEDGER_SENTINEL"}\n', encoding="utf-8")

        excluded_paths = (
            ".env",
            ".env.local",
            ".dbt/profiles.yml",
            ".aws/credentials",
            ".config/gcloud/application_default_credentials.json",
            "credentials/service-account.json",
            "infra/terraform/plan.tfplan",
            "infra/terraform/plan.plan",
            "infra/terraform/terraform.tfvars.json",
            "private/id_rsa",
            "private/signing-key.p8",
            "private/certificate.pem",
            ".ci/profiles.yml",
            "target/compiled/example.sql",
            "dbt_packages/dependency/macros/example.sql",
            "logs/dbt.log",
        )
        for relative_path in excluded_paths:
            self.write_source(relative_path, "synthetic excluded fixture\n")

        result = self.package()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.archive.is_file())

        archive_root = "de-starter-kit-bigquery/"
        with ZipFile(self.archive) as bundle:
            members = set(bundle.namelist())
            expected_payload_members = {
                f"{archive_root}{relative_path}" for relative_path in payloads
            }
            manifest_member = f"{archive_root}MANIFEST.sha256"
            self.assertEqual(
                members,
                expected_payload_members | {manifest_member},
            )
            archive_text = "\n".join(bundle.read(member).decode("utf-8") for member in members)
            self.assertNotIn("DO_NOT_PUBLISH_LEDGER_SENTINEL", archive_text)

            manifest = bundle.read(manifest_member).decode("utf-8")
            manifest_entries = {}
            for line in manifest.splitlines():
                digest, member = line.split("  ", maxsplit=1)
                manifest_entries[member] = digest

            self.assertEqual(set(manifest_entries), expected_payload_members)
            for relative_path, content in payloads.items():
                member = f"{archive_root}{relative_path}"
                self.assertEqual(
                    manifest_entries[member],
                    hashlib.sha256(content.encode("utf-8")).hexdigest(),
                )
                self.assertEqual(bundle.read(member).decode("utf-8"), content)

        checksum_file = Path(f"{self.archive}.sha256")
        self.assertTrue(checksum_file.is_file())
        expected_archive_digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.assertEqual(
            checksum_file.read_text(encoding="utf-8"),
            f"{expected_archive_digest}  {self.archive.name}\n",
        )

    def test_refuses_obvious_credential_payload_without_creating_outputs(self) -> None:
        self.write_source("README.md", "password=fixture-only-fake-value\n")

        result = self.package()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to package credential-like content", result.stderr)
        self.assertFalse(self.archive.exists())
        self.assertFalse(Path(f"{self.archive}.sha256").exists())

    def test_snowflake_archive_uses_snowflake_tree_and_namespace(self) -> None:
        snowflake_archive = self.output_directory / "snowflake-edition.zip"
        (self.snowflake_source / "README.md").write_text("synthetic Snowflake guide\n", encoding="utf-8")
        (self.snowflake_source / "kit").mkdir()
        (self.snowflake_source / "kit" / "dbt_project.yml").write_text("name: fixture\n", encoding="utf-8")
        (self.snowflake_source / "kit" / "target").mkdir()
        (self.snowflake_source / "kit" / "target" / "manifest.json").write_text("generated", encoding="utf-8")

        result = self.package("snowflake", snowflake_archive)

        self.assertEqual(result.returncode, 0, result.stderr)
        with ZipFile(snowflake_archive) as bundle:
            self.assertEqual(
                set(bundle.namelist()),
                {
                    "de-starter-kit-snowflake/README.md",
                    "de-starter-kit-snowflake/kit/dbt_project.yml",
                    "de-starter-kit-snowflake/scripts/first_run_preflight.py",
                    "de-starter-kit-snowflake/scripts/terraform_lifecycle.sh",
                    "de-starter-kit-snowflake/MANIFEST.sha256",
                },
            )
            self.assertEqual(
                bundle.read("de-starter-kit-snowflake/README.md").decode("utf-8"),
                "synthetic Snowflake guide\n",
            )
        self.assertTrue(Path(f"{snowflake_archive}.sha256").is_file())

    def test_repository_terms_files_are_bundled_at_root_when_present(self) -> None:
        self.write_source("README.md", "fixture edition readme\n")
        terms = {
            "LICENSE": "Synthetic licence fixture\n",
            "NOTICE": "Synthetic notice fixture\n",
            "SUPPORT.md": "Synthetic support fixture\n",
        }
        for name, content in terms.items():
            (self.repo / name).write_text(content, encoding="utf-8")

        for edition, archive in (
            ("bigquery", self.archive),
            ("snowflake", self.output_directory / "snowflake-terms.zip"),
        ):
            result = self.package(edition, archive)
            self.assertEqual(result.returncode, 0, result.stderr)
            root = f"de-starter-kit-{edition}/"
            with ZipFile(archive) as bundle:
                members = set(bundle.namelist())
                manifest = bundle.read(f"{root}MANIFEST.sha256").decode("utf-8")
                for name, content in terms.items():
                    member = f"{root}{name}"
                    self.assertIn(member, members)
                    self.assertEqual(bundle.read(member).decode("utf-8"), content)
                    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    self.assertIn(f"{digest}  {member}\n", manifest)

    def test_partial_repository_terms_files_are_tolerated(self) -> None:
        self.write_source("README.md", "fixture edition readme\n")
        (self.repo / "LICENSE").write_text("Synthetic licence fixture\n", encoding="utf-8")

        result = self.package()

        self.assertEqual(result.returncode, 0, result.stderr)
        with ZipFile(self.archive) as bundle:
            members = set(bundle.namelist())
        self.assertIn("de-starter-kit-bigquery/LICENSE", members)
        self.assertNotIn("de-starter-kit-bigquery/NOTICE", members)
        self.assertNotIn("de-starter-kit-bigquery/SUPPORT.md", members)

    def test_refuses_symlinked_repository_terms_file(self) -> None:
        self.write_source("README.md", "fixture edition readme\n")
        target = self.repo / "elsewhere.txt"
        target.write_text("Synthetic linked text\n", encoding="utf-8")
        (self.repo / "NOTICE").symlink_to(target)

        result = self.package()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlinked repository terms file", result.stderr)
        self.assertFalse(self.archive.exists())

    def test_refuses_edition_file_that_collides_with_repository_terms(self) -> None:
        self.write_source("LICENSE", "Edition-level licence fixture\n")
        (self.repo / "LICENSE").write_text("Repository licence fixture\n", encoding="utf-8")

        result = self.package()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("edition already contains LICENSE", result.stderr)
        self.assertFalse(self.archive.exists())

    def test_git_ignored_local_state_is_not_packaged(self) -> None:
        git = shutil.which("git")
        if not git:
            self.skipTest("git is unavailable for the synthetic checkout fixture")
        self.write_source("README.md", "fixture edition readme\n")
        self.write_source("infra/terraform/main.tf", "# synthetic module\n")
        self.write_source(
            "infra/terraform/.gitignore",
            "*.tfstate\n*.tfstate.*\ncrash.log\noverride.tf\nlocal-cache/\n",
        )
        ignored = (
            "infra/terraform/terraform.tfstate.1700000000.backup",
            "infra/terraform/crash.log",
            "infra/terraform/override.tf",
            "infra/terraform/local-cache/provider-notes.txt",
        )
        for relative_path in ignored:
            self.write_source(relative_path, "synthetic ignored local state\n")
        subprocess.run([git, "init", "-q", str(self.repo)], check=True)

        result = self.package()

        self.assertEqual(result.returncode, 0, result.stderr)
        with ZipFile(self.archive) as bundle:
            members = set(bundle.namelist())
        root = "de-starter-kit-bigquery/"
        self.assertIn(f"{root}infra/terraform/main.tf", members)
        self.assertIn(f"{root}infra/terraform/.gitignore", members)
        for relative_path in ignored:
            self.assertNotIn(f"{root}{relative_path}", members)

    def test_refuses_edition_file_that_collides_with_bundled_script(self) -> None:
        self.write_source("README.md", "fixture edition readme\n")
        self.write_source("scripts/first_run_preflight.py", "# synthetic edition copy\n")

        result = self.package()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("edition already contains scripts/first_run_preflight.py", result.stderr)
        self.assertFalse(self.archive.exists())

    def test_generated_directories_are_excluded_case_insensitively(self) -> None:
        self.write_source("README.md", "fixture edition readme\n")
        self.write_source("Target/compiled/example.sql", "select 1\n")
        self.write_source("LOGS/dbt.log", "synthetic log\n")

        result = self.package()

        self.assertEqual(result.returncode, 0, result.stderr)
        with ZipFile(self.archive) as bundle:
            names = bundle.namelist()
        self.assertEqual(len(names), len(set(names)))
        self.assertFalse(any("/Target/" in name or "/LOGS/" in name for name in names))

    def test_executable_bit_is_preserved_and_normalised(self) -> None:
        self.write_source("README.md", "fixture edition readme\n")
        (self.repo / "scripts" / "terraform_lifecycle.sh").chmod(0o750)
        (self.source / "README.md").chmod(0o600)

        result = self.package()

        self.assertEqual(result.returncode, 0, result.stderr)
        with ZipFile(self.archive) as bundle:
            modes = {info.filename: info.external_attr >> 16 for info in bundle.infolist()}
        root = "de-starter-kit-bigquery/"
        self.assertEqual(modes[f"{root}scripts/terraform_lifecycle.sh"], 0o100755)
        self.assertEqual(modes[f"{root}README.md"], 0o100644)
        self.assertEqual(modes[f"{root}MANIFEST.sha256"], 0o100644)

    def test_archive_checksum_is_stable_when_source_mtimes_change(self) -> None:
        self.write_source("README.md", "same synthetic content\n")
        self.write_source("models/example.sql", "select 1 as stable_value\n")

        first_result = self.package()
        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        first_bytes = self.archive.read_bytes()

        second_archive = self.output_directory / "edition-second.zip"
        for source_file in self.source.rglob("*"):
            if source_file.is_file():
                os.utime(source_file, (1_700_000_000, 1_700_000_000))

        second_result = self.package(archive=second_archive)
        self.assertEqual(second_result.returncode, 0, second_result.stderr)
        self.assertEqual(second_archive.read_bytes(), first_bytes)
        first_checksum = Path(f"{self.archive}.sha256").read_text(encoding="utf-8")
        second_checksum = Path(f"{second_archive}.sha256").read_text(encoding="utf-8")
        self.assertEqual(first_checksum.split()[0], second_checksum.split()[0])


if __name__ == "__main__":
    unittest.main()
