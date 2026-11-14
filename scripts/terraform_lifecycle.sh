#!/usr/bin/env bash

set -euo pipefail

usage() {
  printf 'Usage: %s {bigquery|snowflake} {apply|destroy}\n' "${0##*/}" >&2
}

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

if [[ $# -ne 2 ]]; then
  usage
  exit 2
fi

edition="$1"
action="$2"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"

case "$edition" in
  bigquery)
    if [[ -d "$repo_root/editions/Bigquery/infra/terraform" ]]; then
      module_dir="$repo_root/editions/Bigquery/infra/terraform"
    else
      [[ -f "$repo_root/dbt_project.yml" && ! -f "$repo_root/kit/dbt_project.yml" ]] || fail "archive contents do not identify the bigquery edition"
      module_dir="$repo_root/infra/terraform"
    fi
    ;;
  snowflake)
    if [[ -d "$repo_root/editions/Snowflake/infra/terraform" ]]; then
      module_dir="$repo_root/editions/Snowflake/infra/terraform"
    else
      [[ -f "$repo_root/kit/dbt_project.yml" && ! -f "$repo_root/dbt_project.yml" ]] || fail "archive contents do not identify the snowflake edition"
      module_dir="$repo_root/infra/terraform"
    fi
    ;;
  *)
    usage
    fail "edition must be bigquery or snowflake"
    ;;
esac

case "$action" in
  apply|destroy) ;;
  *)
    usage
    fail "action must be apply or destroy"
    ;;
esac

# Terraform reads these from the environment. They could add flags such as -auto-approve,
# switch the workspace or data directory, swap provider binaries or CLI configuration, or
# override variables that terraform.tfvars leaves at their defaults. Refuse to run instead
# of letting the ambient shell silently change what apply or destroy does. Only names are
# printed because values (for example TF_VAR_*) may be sensitive.
blocked_env=()
while IFS= read -r name; do
  case "$name" in
    TF_CLI_ARGS | TF_CLI_ARGS_* | TF_VAR_* | TF_WORKSPACE | TF_DATA_DIR | TF_CLI_CONFIG_FILE | TERRAFORM_CONFIG | TF_REATTACH_PROVIDERS)
      blocked_env+=("$name")
      ;;
  esac
done < <(compgen -e)
if [[ ${#blocked_env[@]} -gt 0 ]]; then
  fail "unset these environment variables before running; they can silently change Terraform behaviour: ${blocked_env[*]}"
fi

command -v terraform >/dev/null 2>&1 || fail "Terraform CLI is not installed or not on PATH"
[[ -d "$module_dir" ]] || fail "Terraform module directory is missing"
[[ -f "$module_dir/main.tf" && -f "$module_dir/versions.tf" ]] || fail "Terraform configuration is incomplete"
[[ -f "$module_dir/terraform.tfvars" ]] || fail "copy and edit this edition's local terraform.tfvars before running"

# The BigQuery module uses GitHub provider resources; require an explicit token instead of ambient CLI auth.
if [[ "$edition" == "bigquery" && -z "${GITHUB_TOKEN:-}" ]]; then
  fail "GITHUB_TOKEN must be set for the BigQuery Terraform action"
fi

terraform -chdir="$module_dir" init -input=false -lockfile=readonly

case "$action" in
  apply)
    terraform -chdir="$module_dir" apply -var-file=terraform.tfvars
    ;;
  destroy)
    terraform -chdir="$module_dir" destroy -var-file=terraform.tfvars
    ;;
esac
