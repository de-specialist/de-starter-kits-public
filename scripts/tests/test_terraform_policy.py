"""Offline regression checks for narrowly scoped Terraform safety policies."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BQ = ROOT / "editions" / "Bigquery" / "infra" / "terraform"
SNOW = ROOT / "editions" / "Snowflake" / "infra" / "terraform"


def _resource_block(source: str, resource_type: str, name: str) -> str:
    match = re.search(
        rf'resource\s+"{re.escape(resource_type)}"\s+"{re.escape(name)}"\s*\{{',
        source,
    )
    if not match:
        raise AssertionError(f"Missing Terraform resource {resource_type}.{name}")
    depth = 1
    index = match.end()
    start = index
    while index < len(source) and depth:
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
        index += 1
    if depth:
        raise AssertionError(f"Unclosed Terraform resource {resource_type}.{name}")
    return source[start:index - 1]


class TerraformPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bq_main = (BQ / "main.tf").read_text(encoding="utf-8")
        cls.bq_vars = (BQ / "variables.tf").read_text(encoding="utf-8")
        cls.bq_readme = (BQ / "README.md").read_text(encoding="utf-8")
        cls.bq_versions = (BQ / "versions.tf").read_text(encoding="utf-8")
        cls.snow_main = (SNOW / "main.tf").read_text(encoding="utf-8")
        cls.snow_vars = (SNOW / "variables.tf").read_text(encoding="utf-8")

    def test_bigquery_job_and_data_grants_have_distinct_narrow_scopes(self) -> None:
        job = _resource_block(self.bq_main, "google_project_iam_member", "ci_job_user")
        dataset = _resource_block(self.bq_main, "google_bigquery_dataset_iam_member", "ci_data_editor")
        self.assertRegex(job, r'project\s*=\s*var\.project_id')
        self.assertRegex(job, r'role\s*=\s*"roles/bigquery\.jobUser"')
        self.assertRegex(dataset, r'dataset_id\s*=\s*google_bigquery_dataset\.ci\.dataset_id')
        self.assertRegex(dataset, r'role\s*=\s*"roles/bigquery\.dataEditor"')
        self.assertNotRegex(self.bq_main, r'google_project_iam_member"\s+"[^"\n]+"\s*\{[^}]*roles/bigquery\.dataEditor')

    def test_bigquery_wif_is_bound_to_repo_ids_and_manual_main_workflow(self) -> None:
        provider = _resource_block(self.bq_main, "google_iam_workload_identity_pool_provider", "github")
        binding = _resource_block(self.bq_main, "google_service_account_iam_member", "github_workload_identity_user")
        for condition in (
            "assertion.repository.lowerAscii() == '${lower(var.github_repository)}'",
            "assertion.repository_id == '${var.github_repository_id}'",
            "assertion.repository_owner_id == '${var.github_repository_owner_id}'",
            "assertion.ref == 'refs/heads/main'",
            "assertion.event_name == 'workflow_dispatch'",
            "assertion.workflow_ref.lowerAscii() == '${lower(local.workflow_ref)}'",
        ):
            self.assertIn(condition, provider)
        self.assertIn("attribute.repository_id/${var.github_repository_id}", binding)
        self.assertIn('"roles/iam.workloadIdentityUser"', binding)
        self.assertIn("GITHUB_TOKEN", (BQ / "versions.tf").read_text(encoding="utf-8"))

    def test_bigquery_wif_ids_use_a_required_validated_recreation_suffix(self) -> None:
        pool = _resource_block(self.bq_main, "google_iam_workload_identity_pool", "github")
        provider = _resource_block(self.bq_main, "google_iam_workload_identity_pool_provider", "github")
        self.assertIn('"starterkit-github-ci-${var.wif_deployment_suffix}"', pool)
        self.assertIn('"github-actions-${var.wif_deployment_suffix}"', provider)
        suffix_match = re.search(
            r'(?ms)^variable "wif_deployment_suffix" \{(.*?)(?=^variable |\Z)',
            self.bq_vars,
        )
        self.assertIsNotNone(suffix_match)
        assert suffix_match is not None
        suffix = suffix_match.group(1)
        self.assertIn('regex("^[a-z0-9]{4,8}$", var.wif_deployment_suffix)', suffix)
        self.assertNotRegex(suffix, r"(?m)^\s*default\s*=")
        self.assertIn("fresh suffix", self.bq_readme)

    def test_bigquery_identity_variables_wait_for_access_grants(self) -> None:
        required_dependencies = (
            "google_service_account_iam_member.github_workload_identity_user",
            "google_project_iam_member.ci_job_user",
            "google_bigquery_dataset_iam_member.ci_data_editor",
        )
        for variable in ("wif_provider", "service_account"):
            block = _resource_block(self.bq_main, "github_actions_variable", variable)
            for dependency in required_dependencies:
                self.assertIn(dependency, block)

    def test_bigquery_disposable_dataset_deletion_is_explicit(self) -> None:
        dataset = _resource_block(self.bq_main, "google_bigquery_dataset", "ci")
        self.assertRegex(dataset, r'delete_contents_on_destroy\s*=\s*true')
        self.assertRegex(dataset, r'deletion_policy\s*=\s*"DELETE"')
        self.assertIn("removes the dataset and all tables and data", self.bq_readme)
        self.assertIn("Never use `-auto-approve`", self.bq_readme)
        self.assertIn("starterkit_", self.bq_vars)
        self.assertIn("confirm_nonproduction_project", self.bq_vars)

    def test_bigquery_minimum_terraform_version_supports_cross_variable_validation(self) -> None:
        self.assertRegex(self.bq_versions, r'required_version\s*=\s*">=\s*1\.9\.0"')
        self.assertIn("Terraform 1.9 or later", self.bq_readme)

    def test_bigquery_location_is_pinned_to_us_and_not_a_provider_region(self) -> None:
        location = re.search(r'(?ms)^variable "location"\s*\{(.*?)^\}', self.bq_vars)
        self.assertIsNotNone(location)
        self.assertRegex(location.group(1), r'default\s*=\s*"US"')
        self.assertRegex(location.group(1), r'condition\s*=\s*var\.location\s*==\s*"US"')
        google_provider = re.search(r'(?ms)^provider "google"\s*\{(.*?)^\}', self.bq_versions)
        self.assertIsNotNone(google_provider)
        self.assertNotRegex(google_provider.group(1), r'(?m)^\s*region\s*=')
        self.assertIn("location is pinned to `US`", self.bq_readme)

    def test_snowflake_grants_are_scoped_to_created_resources(self) -> None:
        db = _resource_block(self.snow_main, "snowflake_grant_privileges_to_account_role", "dbt_database_usage")
        schema = _resource_block(self.snow_main, "snowflake_grant_privileges_to_account_role", "dbt_schema_access")
        warehouse = _resource_block(self.snow_main, "snowflake_grant_privileges_to_account_role", "dbt_warehouse_usage")
        self.assertIn("snowflake_database.dev.name", db)
        self.assertIn("snowflake_schema.dev.fully_qualified_name", schema)
        self.assertIn("snowflake_warehouse.dev.name", warehouse)
        self.assertNotIn("on_account", schema)
        self.assertIn('privileges        = ["USAGE", "CREATE TABLE", "CREATE VIEW"]', schema)
        self.assertNotIn("CREATE STAGE", self.snow_main)
        self.assertNotIn("CREATE FILE FORMAT", self.snow_main)
        self.assertIn("DBT_KIT_(DEV|CI)", self.snow_vars)
        self.assertIn("ACCOUNTADMIN", self.snow_vars)
        self.assertRegex(self.snow_vars, r'default\s*=\s*"XSMALL"')
        self.assertIn("warehouse_auto_suspend_seconds", self.snow_vars)
        self.assertIn("data_retention_time_in_days", self.snow_vars)

    def test_snowflake_warehouse_bounds_statement_runtime(self) -> None:
        warehouse = _resource_block(self.snow_main, "snowflake_warehouse", "dev")
        self.assertRegex(warehouse, r'statement_timeout_in_seconds\s*=\s*var\.warehouse_statement_timeout_seconds')
        self.assertRegex(warehouse, r'warehouse_size\s*=\s*upper\(var\.warehouse_size\)')
        self.assertRegex(warehouse, r'initially_suspended\s*=\s*true')
        self.assertIn("var.warehouse_statement_timeout_seconds <= 3600", self.snow_vars)

    def test_snowflake_disposable_database_is_transient(self) -> None:
        database = _resource_block(self.snow_main, "snowflake_database", "dev")
        self.assertRegex(database, r'is_transient\s*=\s*true')

    def test_bigquery_service_account_id_uses_deployment_suffix(self) -> None:
        account = _resource_block(self.bq_main, "google_service_account", "ci")
        self.assertIn('account_id   = "starterkit-bq-ci-${var.wif_deployment_suffix}"', account)
        # Service account IDs allow at most 30 characters; the suffix is capped at 8.
        self.assertLessEqual(len("starterkit-bq-ci-") + 8, 30)

    def test_bigquery_daily_query_quota_is_optional_and_off_by_default(self) -> None:
        quota = _resource_block(
            self.bq_main, "google_cloud_quotas_quota_preference", "bigquery_query_usage_per_day"
        )
        self.assertIn("count = var.bigquery_daily_query_quota_mib == 0 ? 0 : 1", quota)
        self.assertIn('quota_id      = "QueryUsagePerDay"', quota)
        variable = re.search(
            r'(?ms)^variable "bigquery_daily_query_quota_mib" \{(.*?)^\}', self.bq_vars
        )
        self.assertIsNotNone(variable)
        assert variable is not None
        self.assertRegex(variable.group(1), r'default\s*=\s*0\b')
        self.assertIn("maximum_bytes_billed", self.bq_readme)

    def test_all_providers_use_exact_version_pins(self) -> None:
        for versions in (self.bq_versions, (SNOW / "versions.tf").read_text(encoding="utf-8")):
            constraints = re.findall(r'(?m)^\s*version\s*=\s*"([^"]+)"', versions)
            self.assertTrue(constraints)
            for constraint in constraints:
                # A bare version is also an exact pin in Terraform; Dependabot writes
                # bumps in that form ("= 2.22.0" becomes "2.22.1").
                self.assertRegex(constraint, r"^(= )?\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
