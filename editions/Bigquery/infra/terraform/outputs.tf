output "dataset_id" {
  description = "The isolated dataset configured as BQ_DATASET for the CI build."
  value       = google_bigquery_dataset.ci.dataset_id
}

output "service_account_email" {
  description = "Keyless service account email also written to the GCP_SERVICE_ACCOUNT repository variable."
  value       = google_service_account.ci.email
}

output "workload_identity_provider" {
  description = "Provider resource name also written to the GCP_WORKLOAD_IDENTITY_PROVIDER repository variable."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "project_id" {
  description = "Project containing the isolated dataset and CI identity."
  value       = var.project_id
}
