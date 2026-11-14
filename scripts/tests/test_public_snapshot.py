"""Keep local deployment state and privileged jobs out of the public toolkit."""

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]


class PublicSnapshotTests(unittest.TestCase):
    def test_workflows_are_hosted_and_credential_free(self):
        forbidden = (
            "self-hosted", "secrets.", "id-token:", "contents: write",
            "pull_request_target", "CI_RUNS_ON", "cloudflare-pages:",
        )
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            text = path.read_text()
            with self.subTest(workflow=path.name):
                self.assertIn("runs-on: ubuntu-latest", text)
                for token in forbidden:
                    self.assertNotIn(token, text)

    def test_private_pilot_operations_are_absent(self):
        for name in (
            ".github/privacy-guard.toml", ".github/workflows/privacy-guard.yml",
            ".github/workflows/auto-approve-owner-prs.yml", "website/deploy",
            "docs/product-backlog.md", "docs/customer-validation.md",
            "docs/cloud-acceptance-record.md", "docs/release-validation-record.md",
        ):
            with self.subTest(path=name):
                self.assertFalse((ROOT / name).exists())

    def test_local_configuration_is_ignored_without_hiding_examples(self):
        private = (
            ".env", ".local/target.json", ".agents/task-queue.json",
            ".claude/session.json", ".codex/session.json", "archives/hr.zip",
            "private/mapping.json", "local-config/warehouse.toml",
            "editions/Bigquery/.dbt/profiles.yml", "example.tfvars",
            "example.tfstate", "credentials.pem", "warehouse.duckdb",
        )
        for name in private:
            with self.subTest(path=name):
                result = subprocess.run(
                    ["git", "check-ignore", "--no-index", "-q", name], cwd=ROOT,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0)
        self.assertTrue((ROOT / ".env.example").is_file())
        self.assertTrue((ROOT / "editions/Bigquery/profiles.example.yml").is_file())


if __name__ == "__main__":
    unittest.main()
