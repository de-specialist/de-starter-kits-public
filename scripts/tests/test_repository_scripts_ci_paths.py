"""Keep CI trigger filters aligned with files consumed by tests and the docs site."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "repository-scripts-ci.yml"
DOCS_WORKFLOW = ROOT / ".github" / "workflows" / "docs-site.yml"
MIGRATION_WORKFLOW = ROOT / ".github" / "workflows" / "migration-offline-ci.yml"
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
SCRIPTS_SUITE = "unittest discover -s scripts/tests"
MIGRATION_SUITE = "unittest discover -s migration/tests"
REQUIRED_FIXTURE_PATHS = {
    "migration/examples/**",
    "migration/mappings/**",
    "editions/Bigquery/models/**",
    "editions/Bigquery/models/migration/**",
    "editions/Snowflake/kit/models/**",
    "editions/Snowflake/kit/seeds/**",
    "editions/Snowflake/kit/models/migration/**",
    "editions/Bigquery/.ci/profiles.yml",
    "editions/Snowflake/kit/.ci/profiles.yml",
    "editions/Bigquery/infra/terraform/**",
    "editions/Snowflake/infra/terraform/**",
    ".github/workflows/dbt-bigquery-ci.yml",
    ".github/workflows/dbt-snowflake-ci.yml",
    ".github/dependabot.yml",
}
REQUIRED_DOCS_PATHS = {
    "CONTRIBUTING.md",
    "migration/README.md",
    "migration/mappings/**",
}


def workflow_paths(workflow: Path, event: str) -> set[str]:
    content = workflow.read_text(encoding="utf-8")
    match = re.search(
        rf"(?ms)^  {re.escape(event)}:\n(.*?)(?=^  [A-Za-z_][A-Za-z0-9_-]*:\n|\Z)",
        content,
    )
    if match is None:
        raise AssertionError(f"Workflow is missing the {event} event block")
    paths_block = re.search(r"(?ms)^    paths:\n(.*?)(?=^    [A-Za-z_][A-Za-z0-9_-]*:|\Z)", match.group(1))
    if paths_block is None:
        raise AssertionError(f"Workflow {event} event is missing path filters")
    return set(re.findall(r"(?m)^      - '([^']+)'$", paths_block.group(1)))


class RepositoryScriptsCIPathTests(unittest.TestCase):
    def test_fixture_and_migration_model_paths_trigger_pull_request_ci(self):
        # Script tests run on pull requests only; main is validated by the PR
        # run before merge (push runs were removed to avoid duplicate runs).
        missing = REQUIRED_FIXTURE_PATHS - workflow_paths(WORKFLOW, "pull_request")
        self.assertFalse(missing, f"pull_request CI omits test input paths: {sorted(missing)}")

    def test_published_markdown_inputs_trigger_docs_site_build(self):
        for event in ("pull_request", "push"):
            with self.subTest(event=event):
                missing = REQUIRED_DOCS_PATHS - workflow_paths(DOCS_WORKFLOW, event)
                self.assertFalse(missing, f"{event} docs workflow omits published inputs: {sorted(missing)}")

    def test_workflow_installs_locked_test_dependencies_and_disables_optional_skips(self):
        content = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "python -m pip install --disable-pip-version-check --require-hashes -r migration/tests-requirements-lock.txt",
            content,
        )
        self.assertIn("run: CI=true python -m unittest discover -s scripts/tests -v", content)

    def test_migration_tests_run_on_pull_requests_for_any_migration_change(self):
        paths = workflow_paths(MIGRATION_WORKFLOW, "pull_request")
        self.assertIn("migration/**", paths)
        self.assertIn(".github/workflows/migration-offline-ci.yml", paths)
        content = MIGRATION_WORKFLOW.read_text(encoding="utf-8")
        trigger = re.search(r"(?ms)^  pull_request:\n(.*?)(?=^  [A-Za-z_][A-Za-z0-9_-]*:\n|\Z)", content)
        self.assertIsNotNone(trigger)
        for narrowing_key in ("branches:", "branches-ignore:", "paths-ignore:"):
            self.assertNotIn(narrowing_key, trigger.group(1))
        # Types may only add ready_for_review (draft PRs skip the job) to the defaults.
        types = re.search(r"(?m)^    types: \[([^\]]*)\]$", trigger.group(1))
        if types is not None:
            self.assertLessEqual(
                {"opened", "synchronize", "reopened"}, {t.strip() for t in types.group(1).split(",")}
            )
        self.assertIn(
            "python -m pip install --disable-pip-version-check --require-hashes -r migration/tests-requirements-lock.txt",
            content,
        )
        self.assertIn("run: CI=true python -m unittest discover -s migration/tests -v", content)

    def test_each_offline_suite_runs_in_exactly_one_workflow(self):
        # Overlapping path filters may start both workflows on one PR, so each
        # suite must belong to a single workflow to avoid duplicate runs. This
        # matches the canonical "unittest discover -s <dir>" form used here.
        workflows = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml"))
        for suite, owner in ((SCRIPTS_SUITE, WORKFLOW), (MIGRATION_SUITE, MIGRATION_WORKFLOW)):
            with self.subTest(suite=suite):
                runners = [w.name for w in workflows if suite in w.read_text(encoding="utf-8")]
                self.assertEqual(runners, [owner.name])
                self.assertEqual(owner.read_text(encoding="utf-8").count(suite), 1)


if __name__ == "__main__":
    unittest.main()
