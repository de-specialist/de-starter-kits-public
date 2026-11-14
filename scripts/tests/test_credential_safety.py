"""Offline tests for credential path and payload checks."""

from __future__ import annotations

import sys
import subprocess
import stat
import tempfile
import unittest
import warnings
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from credential_safety import credential_findings, is_sensitive_path  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
# Release archive tooling lands in a separate change; these checks run once it is present.
RELEASE_ARCHIVE_TOOLING = all(
    (REPO / path).is_file()
    for path in (
        "scripts/check_release_archive_paths.py",
        "scripts/validate_release_archives.sh",
        ".github/workflows/release-archive-validation.yml",
    )
)
requires_release_archive_tooling = unittest.skipUnless(
    RELEASE_ARCHIVE_TOOLING, "release archive tooling is not in this checkout yet"
)


class SensitivePathTests(unittest.TestCase):
    def test_common_local_credential_paths_are_sensitive(self) -> None:
        paths = (
            ".dbt/profiles.yml",
            ".ssh/id_ed25519",
            ".config/gcloud/application_default_credentials.json",
            "terraform/terraform.tfstate",
            "terraform/proposed.tfplan",
            "terraform/plan.plan",
            "terraform/terraform.tfvars.json",
            "gha-creds-project-123.json",
            "keys/client.pem",
            ".env.local",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue(is_sensitive_path(Path(path)))

    def test_terraform_generated_files_are_ignored_in_all_roots(self) -> None:
        paths = (
            Path(".gitignore"),
            Path("editions/Bigquery/infra/terraform/.gitignore"),
            Path("editions/Snowflake/infra/terraform/.gitignore"),
        )
        repo = Path(__file__).resolve().parents[2]
        for path in paths:
            content = (repo / path).read_text(encoding="utf-8")
            with self.subTest(path=str(path)):
                self.assertIn("*.tfplan", content)
                self.assertIn("*.plan", content)
                self.assertIn("*.tfvars.json", content)

    @requires_release_archive_tooling
    def test_release_archive_validator_rejects_sensitive_and_generated_paths(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        script = repo / "scripts" / "check_release_archive_paths.py"
        forbidden = (
            "private/gha-creds-project-123.json",
            "infra/service-account.json",
            "infra/terraform.tfstate.backup",
            "infra/proposed.tfplan",
            "infra/plan.plan",
            "infra/terraform.tfvars.json",
            "private/client.p12",
            "private/client.pfx",
            "infra/.terraform/plugins/provider",
            "private/signing-key.p8",
            ".ci/profiles.yml",
            ".dbt/profiles.yml",
            ".venv/bin/python",
            "target/compiled/model.sql",
            "logs/dbt.log",
            "dbt_packages/package/file.sql",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "archive"
            root.mkdir()
            allowed = root / "README.md"
            allowed.write_text("safe documentation\n", encoding="utf-8")
            safe = subprocess.run(
                [sys.executable, str(script), str(root)], capture_output=True, text=True, check=False,
            )
            self.assertEqual(safe.returncode, 0, safe.stderr)
            allowed.unlink()
            for relative in forbidden:
                with self.subTest(relative=relative):
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("synthetic fixture\n", encoding="utf-8")
                    result = subprocess.run(
                        [sys.executable, str(script), str(root)], capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertNotIn("synthetic fixture", result.stdout + result.stderr)
                    path.unlink()
                    for parent in reversed(path.parents):
                        if parent == root:
                            break
                        try:
                            parent.rmdir()
                        except OSError:
                            break
            outside = Path(temporary) / "outside.txt"
            outside.write_text("private content\n", encoding="utf-8")
            (root / "linked.txt").symlink_to(outside)
            symlink_result = subprocess.run(
                [sys.executable, str(script), str(root)], capture_output=True, text=True, check=False,
            )
            self.assertEqual(symlink_result.returncode, 1, symlink_result.stdout + symlink_result.stderr)
            outside_directory = Path(temporary) / "outside-directory"
            outside_directory.mkdir()
            (root / "linked-directory").symlink_to(outside_directory, target_is_directory=True)
            symlink_directory_result = subprocess.run(
                [sys.executable, str(script), str(root)], capture_output=True, text=True, check=False,
            )
            self.assertEqual(
                symlink_directory_result.returncode, 1,
                symlink_directory_result.stdout + symlink_directory_result.stderr,
            )

    @requires_release_archive_tooling
    def test_release_archive_workflow_tracks_every_packaged_security_script(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        workflow = (repo / ".github" / "workflows" / "release-archive-validation.yml").read_text(encoding="utf-8")
        for path in (
            "scripts/package_edition.py",
            "scripts/credential_safety.py",
            "scripts/check_release_archive_paths.py",
            "scripts/first_run_preflight.py",
            "scripts/terraform_lifecycle.sh",
            "scripts/validate_release_archives.sh",
            "scripts/release_archive_gitleaks.toml",
            "scripts/release_archive_gitleaks_allowlist.txt",
        ):
            with self.subTest(path=path):
                self.assertEqual(workflow.count(f"'{path}'"), 2)

    @requires_release_archive_tooling
    def test_release_archive_zip_members_must_stay_under_exact_root(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        script = repo / "scripts" / "check_release_archive_paths.py"
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "fixture.zip"
            fixtures = (
                (("de-starter-kit-bigquery/README.md",), 0),
                (("de-starter-kit-bigquery/README.md", "outside/credentials.json"), 1),
                (("de-starter-kit-bigquery/../outside.txt",), 1),
                (("/de-starter-kit-bigquery/README.md",), 1),
                (("de-starter-kit-bigquery\\..\\outside.txt",), 1),
                (("de-starter-kit-bigquery/repeat.txt", "de-starter-kit-bigquery/repeat.txt"), 1),
            )
            for members, expected in fixtures:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
                        for member in members:
                            bundle.writestr(member, "fixture-only")
                result = subprocess.run(
                    [sys.executable, str(script), "--archive", str(archive), "--expected-root", "de-starter-kit-bigquery"],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

            with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
                link = ZipInfo("de-starter-kit-bigquery/linked.txt")
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                bundle.writestr(link, "../outside.txt")
            symlink_result = subprocess.run(
                [sys.executable, str(script), "--archive", str(archive), "--expected-root", "de-starter-kit-bigquery"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(symlink_result.returncode, 1, symlink_result.stdout + symlink_result.stderr)

    @requires_release_archive_tooling
    def test_release_archive_scan_is_network_isolated(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        script = (repo / "scripts" / "validate_release_archives.sh").read_text(encoding="utf-8")
        docker_command = next(line for line in script.splitlines() if "docker run" in line)
        self.assertIn("--network none", docker_command)

    def test_regular_project_files_are_not_sensitive_by_path(self) -> None:
        self.assertFalse(is_sensitive_path(Path("models/marts/fct_orders.sql")))
        self.assertFalse(is_sensitive_path(Path("profiles.example.yml")))

    def test_obvious_secret_marker_is_reported_without_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = Path(temp_dir) / "config.txt"
            payload.write_text("api_key = actual-secret-value-123\n", encoding="utf-8")

            findings = credential_findings(payload)

        self.assertIn("credential-like key/value assignment", findings)
        self.assertNotIn("actual-secret-value-123", " ".join(findings))

    def test_documented_placeholder_is_not_reported_as_secret_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = Path(temp_dir) / "example.txt"
            payload.write_text("api_key = ${API_KEY}\n", encoding="utf-8")

            self.assertEqual(credential_findings(payload), ())


if __name__ == "__main__":
    unittest.main()
