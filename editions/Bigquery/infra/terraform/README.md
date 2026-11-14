# BigQuery non-production CI infrastructure

This small Terraform root provisions one isolated BigQuery dataset, a keyless CI service account, a GitHub Actions Workload Identity Federation (WIF) pool/provider, and four repository Actions variables. The provider trusts only the configured repository name plus its immutable numeric repository and owner IDs, and `.github/workflows/dbt-bigquery-ci.yml` on `main` for `workflow_dispatch` events. The service-account binding also uses the immutable repository ID. It does not create a project, enable APIs, create a billing budget, or grant organization-wide access.

## Prerequisites

- An existing, dedicated non-production Google Cloud project with billing configured as required by your organization. Do not use a production or customer-data project.
- Terraform 1.9 or later, the Google provider 7.46.2, the GitHub provider 6.13.0, and the Google Cloud CLI. The module uses cross-variable validation to ensure `github_repository` matches `github_owner`.
- Application Default Credentials for the Terraform operator (`gcloud auth application-default login`) and permission to create/manage the listed resources and IAM grants in this project. Project IAM grants require an administrator-approved role; dataset IAM is scoped to the new dataset.
- The IAM, Resource Manager, IAM Credentials, Security Token Service, and BigQuery APIs enabled by an administrator. This module deliberately does not enable services or broaden API administration permissions.
- The exact GitHub repository containing `.github/workflows/dbt-bigquery-ci.yml`.
- The numeric repository ID and owner ID from GitHub repository/owner metadata. These bind WIF to non-reusable IDs rather than relying on names alone.
- A unique 4-8 character lowercase alphanumeric `wif_deployment_suffix` for the CI service account (`starterkit-bq-ci-<suffix>`) and the WIF pool and provider IDs, so a second deployment in the same project does not collide. Use a fresh suffix when recreating them during Google's soft-delete window; old IDs may not be immediately reusable.
- A caller-supplied `GITHUB_TOKEN` environment variable with repository Actions Variables write permission. Prefer a fine-grained token or GitHub App installation token restricted to this repository; repository metadata read may also be needed. Do not pass the token as a Terraform variable or place it in `.tfvars`.

The CI service account receives `roles/bigquery.jobUser` on the supplied project so it can submit jobs, and `roles/bigquery.dataEditor` only on the new `starterkit_ci` dataset. WIF impersonation is granted only to the matching immutable repository-ID principal set; the provider condition requires the configured repository and owner IDs, expected repository name, trusted workflow on `main`, and a manual dispatch. No service-account key is created.

## Create the resources

Run the following from the repository root. Confirm the project is dedicated to non-production sample/CI work before setting the acknowledgment. The dataset defaults to `starterkit_ci`, uses the `US` BigQuery multi-region for the TheLook example, and applies a 30-day default lifetime to tables. Its location is pinned to `US`: changing it would replace a dataset configured for destructive teardown, so location migration is unsupported. BigQuery dataset location is configured on the dataset resource; the Google provider has no `region` setting here because these managed resources are project/global scoped. The project ID, GitHub owner, full `OWNER/REPO`, immutable repository and owner IDs, and deployment suffix are required inputs. Choose a new suffix after teardown if recreating the pool/provider during Google's soft-delete window. GitHub provider authentication reads `GITHUB_TOKEN` from the environment; no token variable or provider token argument is defined. Require this environment variable before applying so the provider does not fall back to a local GitHub CLI login.

```sh
cp editions/Bigquery/infra/terraform/terraform.tfvars.example editions/Bigquery/infra/terraform/terraform.tfvars
# Fill in identifiers, then set confirm_nonproduction_project = true only after verifying the project.
./scripts/terraform_lifecycle.sh bigquery apply
```

In the edition ZIP, run from the extracted `de-starter-kit-bigquery/` directory:

```sh
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
./scripts/terraform_lifecycle.sh bigquery apply
```

Terraform displays a plan and asks for interactive approval. Review every resource and IAM grant before confirming; do not use `-auto-approve`. The configuration intentionally refuses to proceed unless the non-production acknowledgment is true and keeps the dataset name in the `starterkit_` namespace. Choose a different dataset only if it remains a new, isolated starter-kit dataset. The provider cannot independently determine whether an existing Google Cloud project contains production data. No billing budget is created because a billing account was not an input.

### Daily query cost cap (optional)

The dbt profile's `maximum_bytes_billed` is a client-side, per-query limit that applies only to dbt. The CI service account holds project-wide `roles/bigquery.jobUser`, so anything running as it could submit queries without that limit. For a server-side guard, set `bigquery_daily_query_quota_mib` in `terraform.tfvars` (for example `1048576` for 1 TiB per day). Terraform then creates a Cloud Quotas preference that lowers BigQuery's project-wide `QueryUsagePerDay` quota. It is off by default because it needs the Cloud Quotas API (`cloudquotas.googleapis.com`) enabled and the `cloudquotas.quotas.update` permission, and it caps every query in the project, not just CI. Cloud Quotas cannot delete a preference: `destroy` only removes it from Terraform state and the cap stays in place. Raise or clear it on the console's **IAM & Admin > Quotas & System Limits** page. If you do not use this variable, set a custom query quota on that page before running CI, as described in [Create custom query quotas](https://cloud.google.com/bigquery/docs/custom-quotas).

On apply, Terraform creates or updates the following GitHub repository Actions variables:

- `BQ_CI_PROJECT` = configured Google Cloud project ID.
- `BQ_CI_DATASET` = created isolated dataset ID.
- `GCP_WORKLOAD_IDENTITY_PROVIDER` = created WIF provider resource name.
- `GCP_SERVICE_ACCOUNT` = created keyless service account email.

These values are identifiers, not credentials, so the workflow reads them from `vars.*`, not GitHub secrets. Terraform publishes the service-account and provider variables only after the WIF impersonation binding and BigQuery job/dataset grants exist; on destroy, it removes those variables before removing the grants. The only GitHub authentication credential is the caller-supplied `GITHUB_TOKEN` environment variable; Terraform neither accepts it as an input variable nor outputs it. Do not create or upload a service-account key. Keep Terraform state private; it contains infrastructure identifiers and IAM configuration. Local state, tfvars, and Terraform working files are excluded by this directory's `.gitignore`. Do not place credentials in Terraform variables, state, or examples. Leave `GITHUB_OWNER` and `GITHUB_ORGANIZATION` unset so the explicit provider owner is used.

Google IAM changes usually propagate within about two minutes, but can take seven minutes or more. Allow time before the first manual workflow dispatch; if authentication or authorization is denied immediately after apply, retry after propagation rather than changing the trust policy. See Google's [access change propagation guidance](https://cloud.google.com/iam/docs/access-change-propagation).

### Upgrading an existing deployment

Earlier versions of this module named the CI service account `starterkit-bq-ci`. It is now `starterkit-bq-ci-<suffix>`, and Google cannot rename a service account, so the next apply plans to destroy and recreate it along with its IAM grants and the WIF binding. Terraform updates `GCP_SERVICE_ACCOUNT` to the new email. Expect CI authentication to fail until the new grants propagate; dispatch the workflow again after a few minutes.

## Teardown

From the repository root, run `./scripts/terraform_lifecycle.sh bigquery destroy` and review the proposed deletions before accepting the interactive confirmation. The dataset is configured with `delete_contents_on_destroy = true`; destroying it removes the dataset and all tables and data stored in it. Terraform also removes the dedicated CI identity, its grants, WIF pool/provider, and repository variables. Use only a disposable sample/CI target. Never use `-auto-approve`; retain a private copy of state until teardown is complete.

## References

- Google provider: [`google_bigquery_dataset`](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/bigquery_dataset), [`google_bigquery_dataset_iam_member`](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/google_bigquery_dataset_iam), [`google_iam_workload_identity_pool`](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/iam_workload_identity_pool), and [`google_iam_workload_identity_pool_provider`](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/iam_workload_identity_pool_provider).
- GitHub provider: [`github_actions_variable`](https://registry.terraform.io/providers/integrations/github/6.13.0/docs/resources/actions_variable) and [provider authentication](https://registry.terraform.io/providers/integrations/github/6.13.0/docs).
- Google Cloud: [Workload Identity Federation for deployment pipelines](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines) and [BigQuery dataset deletion behavior](https://docs.cloud.google.com/bigquery/docs/managing-datasets#delete_datasets).
