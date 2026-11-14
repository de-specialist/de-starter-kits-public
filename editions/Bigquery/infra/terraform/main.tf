locals {
  table_expiration_ms = var.default_table_expiration_days == 0 ? null : var.default_table_expiration_days * 24 * 60 * 60 * 1000
  labels = {
    environment = "nonprod"
    managed_by  = "terraform"
    purpose     = "starter-kit-ci"
  }
  workflow_ref    = "${var.github_repository}/.github/workflows/dbt-bigquery-ci.yml@refs/heads/main"
  repository_name = split("/", var.github_repository)[1]
}

resource "github_actions_variable" "bq_ci_project" {
  repository    = local.repository_name
  variable_name = "BQ_CI_PROJECT"
  value         = var.project_id
}

resource "github_actions_variable" "bq_ci_dataset" {
  repository    = local.repository_name
  variable_name = "BQ_CI_DATASET"
  value         = google_bigquery_dataset.ci.dataset_id
}

resource "github_actions_variable" "wif_provider" {
  repository    = local.repository_name
  variable_name = "GCP_WORKLOAD_IDENTITY_PROVIDER"
  value         = google_iam_workload_identity_pool_provider.github.name

  depends_on = [
    google_service_account_iam_member.github_workload_identity_user,
    google_project_iam_member.ci_job_user,
    google_bigquery_dataset_iam_member.ci_data_editor,
  ]
}

resource "github_actions_variable" "service_account" {
  repository    = local.repository_name
  variable_name = "GCP_SERVICE_ACCOUNT"
  value         = google_service_account.ci.email

  depends_on = [
    google_service_account_iam_member.github_workload_identity_user,
    google_project_iam_member.ci_job_user,
    google_bigquery_dataset_iam_member.ci_data_editor,
  ]
}

resource "google_bigquery_dataset" "ci" {
  project                     = var.project_id
  dataset_id                  = var.dataset_id
  friendly_name               = "Starter kit CI (non-production)"
  description                 = "Disposable, isolated BigQuery dataset for the data engineering starter kit. Do not store production or customer data."
  location                    = var.location
  default_table_expiration_ms = local.table_expiration_ms
  delete_contents_on_destroy  = true
  deletion_policy             = "DELETE"
  labels                      = local.labels
}

resource "google_service_account" "ci" {
  project      = var.project_id
  account_id   = "starterkit-bq-ci-${var.wif_deployment_suffix}"
  display_name = "Starter kit BigQuery CI"
  description  = "Keyless GitHub Actions identity for the starter-kit BigQuery build."
}

resource "google_project_iam_member" "ci_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.ci.email}"
}

resource "google_bigquery_dataset_iam_member" "ci_data_editor" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.ci.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.ci.email}"
}

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "starterkit-github-ci-${var.wif_deployment_suffix}"
  display_name              = "Starter kit GitHub Actions"
  description               = "Federation pool restricted to the configured repository and trusted BigQuery workflow."
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-actions-${var.wif_deployment_suffix}"
  display_name                       = "GitHub Actions BigQuery CI"
  description                        = "OIDC provider restricted to the main-branch manual BigQuery CI workflow."

  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.repository"          = "assertion.repository"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
    "attribute.ref"                 = "assertion.ref"
    "attribute.event_name"          = "assertion.event_name"
    "attribute.workflow_ref"        = "assertion.workflow_ref"
  }

  attribute_condition = "assertion.repository.lowerAscii() == '${lower(var.github_repository)}' && assertion.repository_id == '${var.github_repository_id}' && assertion.repository_owner_id == '${var.github_repository_owner_id}' && assertion.ref == 'refs/heads/main' && assertion.event_name == 'workflow_dispatch' && assertion.workflow_ref.lowerAscii() == '${lower(local.workflow_ref)}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "github_workload_identity_user" {
  service_account_id = google_service_account.ci.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/${var.github_repository_id}"
}

# Optional, default-off server-side cap on BigQuery bytes processed per project per day.
# dbt's maximum_bytes_billed is client-side and covers only dbt jobs; this quota covers every
# query in the project. Cloud Quotas cannot delete a preference, so destroy only removes it
# from state and the cap stays in place until changed in the console or set to -1.
resource "google_cloud_quotas_quota_preference" "bigquery_query_usage_per_day" {
  count = var.bigquery_daily_query_quota_mib == 0 ? 0 : 1

  parent        = "projects/${var.project_id}"
  name          = "starterkit-bq-query-usage-${var.wif_deployment_suffix}"
  service       = "bigquery.googleapis.com"
  quota_id      = "QueryUsagePerDay"
  justification = "Starter-kit non-production cost guard managed by Terraform."

  quota_config {
    preferred_value = tostring(var.bigquery_daily_query_quota_mib)
  }

  # Lowering an unlimited quota to a small value is a large decrease by design.
  ignore_safety_checks = "QUOTA_DECREASE_PERCENTAGE_TOO_HIGH"
}
