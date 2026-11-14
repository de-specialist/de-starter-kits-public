"""Offline failure-path tests for release archive validation orchestration."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGED_FILES = (
    "package_edition.py",
    "credential_safety.py",
    "check_release_archive_paths.py",
    "first_run_preflight.py",
    "terraform_lifecycle.sh",
    "validate_release_archives.sh",
    "release_archive_gitleaks.toml",
    "release_archive_gitleaks_allowlist.txt",
)
TOOL_NAMES = ("python3", "unzip", "sha256sum", "git", "dirname", "mktemp", "basename", "mkdir", "rm", "grep", "cp", "cat")
PINNED_IMAGE = "ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:b109bc5f8f76a38196a3e413704fc5b9e3c32360bce4e4b603bd6f45b3721dbb"


class ValidateReleaseArchivesFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        missing_tools = [name for name in TOOL_NAMES if shutil.which(name) is None]
        if missing_tools:
            self.skipTest(f"required local archive-test tools are unavailable: {', '.join(missing_tools)}")
        self.root = Path(self.temporary.name)
        self.repo = self.root / "fixture-repository"
        scripts = self.repo / "scripts"
        scripts.mkdir(parents=True)
        for name in PACKAGED_FILES:
            shutil.copyfile(REPOSITORY_ROOT / "scripts" / name, scripts / name)
        for edition in ("Bigquery", "Snowflake"):
            edition_root = self.repo / "editions" / edition
            edition_root.mkdir(parents=True)
            (edition_root / "README.md").write_text("synthetic release fixture\n", encoding="utf-8")

        git = shutil.which("git")
        if not git:
            self.skipTest("git is unavailable for the synthetic clean-checkout fixture")
        subprocess.run([git, "init", "-q", str(self.repo)], check=True)
        subprocess.run([git, "-C", str(self.repo), "config", "user.email", "fixture@example.invalid"], check=True)
        subprocess.run([git, "-C", str(self.repo), "config", "user.name", "Fixture"], check=True)
        subprocess.run([git, "-C", str(self.repo), "add", "scripts", "editions"], check=True)
        subprocess.run([git, "-C", str(self.repo), "commit", "-qm", "fixture"], check=True)

        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in TOOL_NAMES:
            executable = shutil.which(name)
            if executable:
                (self.bin / name).symlink_to(executable)

    def run_validation(self, *, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PATH"] = str(self.bin)
        env["PYTHON"] = "python3"
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["/bin/bash", str(self.repo / "scripts" / "validate_release_archives.sh")],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def write_tool(self, name: str, content: str) -> Path:
        tool = self.bin / name
        tool.unlink(missing_ok=True)
        tool.write_text(content, encoding="utf-8")
        tool.chmod(0o755)
        return tool

    def test_missing_docker_refuses_with_actionable_message(self) -> None:
        result = self.run_validation()

        self.assertEqual(result.returncode, 2)
        self.assertIn("install Docker", result.stderr)
        self.assertIn("CLI is on PATH", result.stderr)

    def test_dirty_checkout_refuses_without_disclosing_paths(self) -> None:
        (self.repo / "editions" / "Bigquery" / "private-fixture-name.txt").write_text(
            "synthetic dirty sentinel\n", encoding="utf-8"
        )

        result = self.run_validation()

        self.assertEqual(result.returncode, 2)
        self.assertIn("clean Git checkout", result.stderr)
        self.assertNotIn("private-fixture-name", result.stdout + result.stderr)
        self.assertNotIn("synthetic dirty sentinel", result.stdout + result.stderr)

    def test_pinned_container_failure_is_actionable_and_keeps_isolation(self) -> None:
        args_file = self.root / "docker-args.txt"
        self.write_tool(
            "docker",
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$DOCKER_ARGS_FILE\"\necho 'fixture container failure' >&2\nexit 17\n",
        )

        result = self.run_validation(extra_env={"DOCKER_ARGS_FILE": str(args_file)})

        self.assertEqual(result.returncode, 1)
        self.assertIn("fixture container failure", result.stderr)
        self.assertIn("confirm the pinned Gitleaks image can run", result.stderr)
        args = args_file.read_text(encoding="utf-8")
        self.assertIn("--network\nnone", args)
        self.assertIn(":ro", args)
        self.assertIn("ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:b109bc5f8f76a38196a3e413704fc5b9e3c32360bce4e4b603bd6f45b3721dbb", args)

    def commit_fixture(self, message: str) -> None:
        git = shutil.which("git") or "git"
        subprocess.run([git, "-C", str(self.repo), "add", "-A", "-f", "scripts", "editions"], check=True)
        subprocess.run([git, "-C", str(self.repo), "commit", "-qm", message], check=True)

    def add_edition_file(self, relative: str, content: str) -> None:
        path = self.repo / "editions" / "Bigquery" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.commit_fixture("add edition gitleaks file")

    def recording_docker(self) -> tuple[Path, Path]:
        """Install a fake docker that records arguments and the mounted scan config."""
        args_file = self.root / "docker-args.txt"
        config_copy = self.root / "docker-config-copy.toml"
        self.write_tool(
            "docker",
            "#!/bin/sh\n"
            "printf '%s\\n' \"$@\" >> \"$DOCKER_ARGS_FILE\"\n"
            "printf '%s\\n' '===' >> \"$DOCKER_ARGS_FILE\"\n"
            "for arg in \"$@\"; do\n"
            "  case \"$arg\" in\n"
            "    *:/scan-config:ro)\n"
            "      src=${arg%:/scan-config:ro}\n"
            "      cat \"$src/gitleaks.toml\" > \"$DOCKER_CONFIG_COPY\"\n"
            "      if [ -s \"$src/gitleaksignore\" ]; then echo 'ignore file not empty' >&2; exit 41; fi\n"
            "      ;;\n"
            "  esac\n"
            "done\n"
            "exit 0\n",
        )
        self.docker_env = {"DOCKER_ARGS_FILE": str(args_file), "DOCKER_CONFIG_COPY": str(config_copy)}
        return args_file, config_copy

    def test_scan_always_uses_repository_pinned_config_and_empty_ignore_file(self) -> None:
        args_file, config_copy = self.recording_docker()

        result = self.run_validation(extra_env=self.docker_env)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        invocations = [chunk for chunk in args_file.read_text(encoding="utf-8").split("===\n") if chunk.strip()]
        self.assertEqual(len(invocations), 2)
        for invocation in invocations:
            args = invocation.splitlines()
            self.assertIn(PINNED_IMAGE, args)
            self.assertEqual(args[args.index("--config") + 1], "/scan-config/gitleaks.toml")
            self.assertEqual(args[args.index("--gitleaks-ignore-path") + 1], "/scan-config/gitleaksignore")
            self.assertIn("--ignore-gitleaks-allow", args)
            mounts = [args[index + 1] for index, arg in enumerate(args) if arg == "--volume"]
            self.assertEqual(len(mounts), 2, mounts)
            self.assertTrue(all(mount.endswith(":ro") for mount in mounts), mounts)
            release_host = Path(next(m for m in mounts if m.endswith(":/release:ro")).rsplit(":/release", 1)[0])
            config_host = Path(next(m for m in mounts if m.endswith(":/scan-config:ro")).rsplit(":/scan-config", 1)[0])
            self.assertFalse(config_host.is_relative_to(release_host), "scan config must live outside the scanned tree")
        self.assertEqual(
            config_copy.read_text(encoding="utf-8"),
            (REPOSITORY_ROOT / "scripts" / "release_archive_gitleaks.toml").read_text(encoding="utf-8"),
        )

    def test_missing_repository_config_refuses(self) -> None:
        self.write_tool("docker", "#!/bin/sh\nexit 0\n")
        (self.repo / "scripts" / "release_archive_gitleaks.toml").unlink()
        self.commit_fixture("drop scan config")

        result = self.run_validation()

        self.assertEqual(result.returncode, 2)
        self.assertIn("release_archive_gitleaks.toml is missing", result.stderr)

    def assert_edition_file_refused_before_scan(self, relative: str) -> None:
        args_file, _ = self.recording_docker()
        self.add_edition_file(relative, "[[allowlists]]\npaths = ['.*']\n")

        result = self.run_validation(extra_env=self.docker_env)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("release_archive_gitleaks_allowlist.txt", result.stderr)
        self.assertFalse(args_file.exists(), "the secret scan must not run on a refused archive")

    def test_edition_root_gitleaks_config_fails_validation(self) -> None:
        self.assert_edition_file_refused_before_scan(".gitleaks.toml")

    def test_edition_root_gitleaks_ignore_fails_validation(self) -> None:
        self.assert_edition_file_refused_before_scan(".gitleaksignore")

    def test_nested_mixed_case_gitleaks_ignore_fails_validation(self) -> None:
        self.assert_edition_file_refused_before_scan("models/nested/.GitLeaksIgnore")

    def test_allowlisted_edition_gitleaks_file_passes_without_changing_scan(self) -> None:
        args_file, config_copy = self.recording_docker()
        (self.repo / "scripts" / "release_archive_gitleaks_allowlist.txt").write_text(
            "# approved for a documented example\nde-starter-kit-bigquery/examples/.gitleaksignore\n",
            encoding="utf-8",
        )
        self.add_edition_file("examples/.gitleaksignore", "fixture-fingerprint\n")

        result = self.run_validation(extra_env=self.docker_env)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--gitleaks-ignore-path\n/scan-config/gitleaksignore", args_file.read_text(encoding="utf-8"))
        self.assertEqual(
            config_copy.read_text(encoding="utf-8"),
            (REPOSITORY_ROOT / "scripts" / "release_archive_gitleaks.toml").read_text(encoding="utf-8"),
        )

    def test_allowlist_entry_for_other_path_does_not_approve_file(self) -> None:
        (self.repo / "scripts" / "release_archive_gitleaks_allowlist.txt").write_text(
            "de-starter-kit-snowflake/.gitleaksignore\n", encoding="utf-8",
        )
        self.commit_fixture("approve a different path")
        self.assert_edition_file_refused_before_scan(".gitleaksignore")

    def test_partial_package_failure_cleans_temporary_release_files(self) -> None:
        temp_release = self.root / "controlled-release-temp"
        self.write_tool("docker", "#!/bin/sh\nexit 0\n")
        self.write_tool(
            "mktemp",
            "#!/bin/sh\nmkdir -p \"$CONTROLLED_RELEASE_TEMP\"\nprintf '%s\\n' \"$CONTROLLED_RELEASE_TEMP\"\n",
        )
        self.write_tool(
            "python3",
            "#!/bin/sh\nif [ \"$1\" = \"$FIXTURE_PACKAGE_SCRIPT\" ]; then\n  : > \"$3\"\n  : > \"$3.sha256\"\n  echo 'fixture package failure after partial output' >&2\n  exit 23\nfi\nexec \"$REAL_PYTHON\" \"$@\"\n",
        )

        result = self.run_validation(extra_env={
            "CONTROLLED_RELEASE_TEMP": str(temp_release),
            "FIXTURE_PACKAGE_SCRIPT": str(self.repo / "scripts" / "package_edition.py"),
            "REAL_PYTHON": shutil.which("python3") or "python3",
        })

        self.assertEqual(result.returncode, 23)
        self.assertIn("fixture package failure after partial output", result.stderr)
        self.assertFalse(temp_release.exists())


class ExtractedTreeGitleaksFileTests(unittest.TestCase):
    """Exercise the extracted-tree check directly; the orchestration tests stop at the ZIP check."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "de-starter-kit-bigquery"
        (self.root / "models" / "nested").mkdir(parents=True)
        (self.root / "README.md").write_text("synthetic\n", encoding="utf-8")
        self.allowlist = Path(temporary.name) / "allowlist.txt"
        self.allowlist.write_text("# none approved\n", encoding="utf-8")

    def check(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(REPOSITORY_ROOT / "scripts" / "check_release_archive_paths.py"),
                "--gitleaks-allowlist",
                str(self.allowlist),
                str(self.root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_clean_tree_passes(self) -> None:
        self.assertEqual(self.check().returncode, 0)

    def test_nested_mixed_case_ignore_file_is_refused(self) -> None:
        (self.root / "models" / "nested" / ".GitLeaksIgnore").write_text("fingerprint\n", encoding="utf-8")

        result = self.check()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("release_archive_gitleaks_allowlist.txt", result.stderr)

    def test_directory_named_like_config_is_refused(self) -> None:
        (self.root / ".gitleaks.toml").mkdir()

        self.assertEqual(self.check().returncode, 1)

    def test_allowlisted_path_passes_and_other_paths_still_fail(self) -> None:
        (self.root / "models" / "nested" / ".gitleaksignore").write_text("fingerprint\n", encoding="utf-8")
        self.allowlist.write_text("de-starter-kit-bigquery/models/nested/.gitleaksignore\n", encoding="utf-8")
        self.assertEqual(self.check().returncode, 0)

        (self.root / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
        self.assertEqual(self.check().returncode, 1)

    def test_unreadable_allowlist_refuses(self) -> None:
        self.allowlist.unlink()

        result = self.check()

        self.assertEqual(result.returncode, 2)
        self.assertIn("allowlist could not be read", result.stderr)


if __name__ == "__main__":
    unittest.main()
