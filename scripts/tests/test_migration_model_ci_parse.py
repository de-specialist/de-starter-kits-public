"""Verify CI parses optional migration models with isolated placeholder profiles."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMMAND = "dbt parse --profiles-dir .ci --target migration --vars '{enable_migration_models: true}'"


class MigrationModelCIParseTests(unittest.TestCase):
    def test_both_ci_workflows_parse_migration_target_with_models_enabled(self) -> None:
        workflows = (
            ROOT / ".github/workflows/dbt-bigquery-ci.yml",
            ROOT / ".github/workflows/dbt-snowflake-ci.yml",
        )
        for workflow in workflows:
            with self.subTest(workflow=workflow.name):
                content = workflow.read_text(encoding="utf-8")
                parse_job = re.search(r"(?ms)^  parse:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", content)
                self.assertIsNotNone(parse_job, f"{workflow.name} is missing a parse job")
                self.assertIn(COMMAND, parse_job.group(1))
                self.assertIn("Parse optional migration models without warehouse access", parse_job.group(1))
                self.assertNotIn("secrets.", parse_job.group(1))
                self.assertNotIn("id-token: write", parse_job.group(1))

    def test_bigquery_parse_job_has_noncredentialed_project_for_source_rendering(self) -> None:
        content = (ROOT / ".github/workflows/dbt-bigquery-ci.yml").read_text(encoding="utf-8")
        parse_job = re.search(r"(?ms)^  parse:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", content)
        self.assertIsNotNone(parse_job)
        self.assertRegex(parse_job.group(1), r"(?m)^      BQ_PROJECT: ci-placeholder$")

    def test_bigquery_migration_profile_uses_placeholder_project_and_distinct_dataset(self) -> None:
        content = (ROOT / "editions/Bigquery/.ci/profiles.yml").read_text(encoding="utf-8")
        migration = re.search(r"(?ms)^    migration:\n(.*?)(?=^    [A-Za-z0-9_-]+:|\Z)", content)
        self.assertIsNotNone(migration)
        block = migration.group(1)
        self.assertIn("type: bigquery", block)
        self.assertIn("project: ci-placeholder", block)
        self.assertIn("dataset: migration_ci_placeholder", block)
        self.assertNotIn("BQ_PROJECT", block)

    def test_snowflake_migration_profile_uses_placeholder_and_distinct_namespace(self) -> None:
        content = (ROOT / "editions/Snowflake/kit/.ci/profiles.yml").read_text(encoding="utf-8")
        migration = re.search(r"(?ms)^    migration:\n(.*?)(?=^    [A-Za-z0-9_-]+:|\Z)", content)
        self.assertIsNotNone(migration)
        block = migration.group(1)
        self.assertIn("type: snowflake", block)
        self.assertIn("account: ci-placeholder", block)
        self.assertIn("password: ci-placeholder", block)
        self.assertIn("database: MIGRATION_CI_DATABASE", block)
        self.assertIn("schema: MIGRATION_CI", block)
        self.assertNotIn("SNOWFLAKE_", block)


if __name__ == "__main__":
    unittest.main()
