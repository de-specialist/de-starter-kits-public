# Terraform lifecycle and safety

The BigQuery and Snowflake examples are separate, optional infrastructure modules. Each must have its own working directory, state file, credentials, variables, and review. Never copy state or credential files between editions or use these examples against production or customer-data environments.

## Static validation

The `Terraform validation` workflow runs Terraform 1.17.1 `fmt -check`, `init -backend=false`, and `validate` separately for each module. It has read-only repository permissions and does not plan, apply, destroy, or request cloud credentials. A successful validation checks configuration syntax/provider schemas only; it does not prove permissions, costs, or successful provisioning.

## BigQuery module

Directory: `editions/Bigquery/infra/terraform/`. Use a dedicated non-production GCP project, locally authenticated Application Default Credentials, a GitHub `GITHUB_TOKEN` supplied through a secure environment mechanism, and the least privilege approved for the module. The GitHub provider reads only the `GITHUB_TOKEN` environment variable; do not pass it as Terraform input or place it in state. Keep local state private; it records project/resource identifiers and IAM configuration.

Authenticate with `gcloud auth application-default login`; set `GITHUB_TOKEN` securely, and copy/edit the ignored `terraform.tfvars` from its example with the project, GitHub owner/repository, immutable GitHub IDs, and `confirm_nonproduction_project=true` only after verifying the target. Use a fine-grained token or GitHub App installation token with repository Actions Variables write access. From the repository root, initialize and apply interactively with:

```sh
./scripts/terraform_lifecycle.sh bigquery apply
```

Before running, confirm `GITHUB_TOKEN` is set without printing its value; the GitHub provider can otherwise fall back to the local GitHub CLI. Review the full proposed GCP and GitHub resource changes and approve interactively. The module creates a BigQuery dataset, service account, scoped grants, GitHub Workload Identity Federation resources, and the repository Actions variables `BQ_CI_PROJECT`, `BQ_CI_DATASET`, `GCP_WORKLOAD_IDENTITY_PROVIDER`, and `GCP_SERVICE_ACCOUNT`. All four values are identifiers, so the workflow reads them through `vars.*`; no manual post-apply variable or secret setup is needed. GCP charges may apply for stored data, query processing, and other enabled services; this module does not create a billing budget. Dataset teardown deletes its contents.

## Snowflake module

Directory: `editions/Snowflake/infra/terraform/`. Use a dedicated disposable Snowflake database/schema and warehouse. Authenticate using the module's documented Snowflake provider method; keep credentials and `.tfvars` local and use a separate state file from BigQuery. Snowflake charges may accrue from warehouse compute and storage; use auto-suspend and inspect the plan for warehouse size, grants, and resource scope.

Authenticate using the protected local Snowflake CLI profile or secret-injected environment described in the module README. Copy and edit the ignored variables file, then from the repository root apply interactively:

```sh
cp editions/Snowflake/infra/terraform/terraform.tfvars.example editions/Snowflake/infra/terraform/terraform.tfvars
./scripts/terraform_lifecycle.sh snowflake apply
```

## Before the first acceptance run

After a module is applied, and before any dbt command contacts the new target, run the offline cloud acceptance preflight described in `docs/cloud-acceptance-runbook.md` (added with the preflight/credentials work). It requires the explicit disposable project/dataset or database/schema identity, rejects `DBT_DEV` and production-like names, makes no network call, and never plans or applies Terraform.

## Destroying resources

Destroy only disposable resources created by the selected module, with its matching state and credentials. Both commands run from the repository root and require Terraform's interactive confirmation:

```sh
./scripts/terraform_lifecycle.sh bigquery destroy
./scripts/terraform_lifecycle.sh snowflake destroy
```

Read the complete destroy plan before confirming. BigQuery dataset deletion removes tables and data in that dataset. Snowflake database/schema deletion can remove tables and data, and warehouse deletion stops that compute resource. Terraform may also revoke IAM grants and delete identities or federation resources. Preserve private state until teardown is verified; never use `-auto-approve` or run commands against a shared or production target.

After `terraform destroy` succeeds, the operator verifies that each managed resource is absent in the cloud provider console or with the provider CLI. An empty `terraform state list` is useful state evidence, but does not by itself prove the remote resources were deleted. Record the edition, non-secret target identity, commit, Terraform workspace/backend, UTC completion time, command exit status, and the managed resources confirmed absent. Keep this record and any state files private, and do not include credentials or resource contents.
