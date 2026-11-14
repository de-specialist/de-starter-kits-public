"""Offline tests for direct-requirement and hash-lock consistency checking."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CHECKER = Path(__file__).resolve().parents[1] / "check_dependency_lock.py"


class CheckDependencyLockTests(unittest.TestCase):
    def run_checker(self, requirements: str, lock: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            requirements_path = temp_path / "requirements.txt"
            lock_path = temp_path / "requirements-lock.txt"
            requirements_path.write_text(requirements)
            lock_path.write_text(lock)
            return subprocess.run(
                [sys.executable, str(CHECKER), str(requirements_path), str(lock_path)],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_compatible_exact_pin_passes(self) -> None:
        result = self.run_checker(
            "dbt-bigquery>=1.10.5,<2.0\n",
            "dbt_bigquery==1.12.1 \\\n    --hash=sha256:" + "a" * 64 + "\n",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("compatible exact pins", result.stdout)

    def test_missing_pin_fails(self) -> None:
        result = self.run_checker("dbt-bigquery>=1.10.5,<2.0\n", "dbt-core==1.12.6\n")

        self.assertEqual(result.returncode, 1)
        self.assertIn("no exact version pin", result.stderr)

    def test_pin_outside_direct_constraint_fails(self) -> None:
        result = self.run_checker("dbt-bigquery>=1.10.5,<2.0\n", "dbt-bigquery==2.0.0\n")

        self.assertEqual(result.returncode, 1)
        self.assertIn("do not satisfy", result.stderr)

    def test_direct_url_is_rejected(self) -> None:
        result = self.run_checker(
            "dbt-bigquery @ https://example.invalid/dbt-bigquery.whl\n",
            "dbt-bigquery==1.12.1\n",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("direct URL requirements are not supported", result.stderr)

    def test_include_directive_is_rejected(self) -> None:
        result = self.run_checker("-r extra-requirements.txt\n", "dbt-bigquery==1.12.1\n")

        self.assertEqual(result.returncode, 2)
        self.assertIn("include, editable, and path directives are unsupported", result.stderr)


if __name__ == "__main__":
    unittest.main()
