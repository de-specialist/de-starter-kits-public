# BigQuery setup and deployment

## Prerequisites

- Python 3.11, Git, and the Google Cloud CLI.
- A Google Cloud project for dbt jobs and destination tables. BigQuery Sandbox can be used for limited exploration; review its current limits before relying on it.
- Access to the public `bigquery-public-data.thelook_ecommerce` source dataset.

The profile uses `BQ_PROJECT` as the project where query jobs run and destination tables are written. Set `BQ_DATASET` for the destination dataset (default `dbt_retail`) and `BQ_LOCATION` for its location (default `US`). The public TheLook dataset is in the `US` multi-region, so keep the target dataset in `US` for this example.

The `dbt_retail` default is for ordinary local development. The cloud acceptance preflight requires a dedicated `starterkit_`-prefixed dataset; use the [cloud acceptance runbook](../../../docs/cloud-acceptance-runbook.md) and set `BQ_DATASET` to that confirmed target before running it.

## Access and authentication

Enable the BigQuery API for the project. For a least-privilege setup, ask an administrator to grant:

- **BigQuery Job User** (`roles/bigquery.jobUser`) on the project, so your identity can run query jobs.
- **BigQuery Data Editor** (`roles/bigquery.dataEditor`) on the destination dataset, so dbt can create and update its models.

Create the destination dataset in the BigQuery console first, in the location configured by `BQ_LOCATION`. If you want dbt to create datasets itself, your identity also needs dataset creation permission on the project; avoid broad project-level editor roles for convenience. Google documents the [BigQuery roles and permissions](https://docs.cloud.google.com/bigquery/docs/access-control).

For local development, authenticate with Application Default Credentials:

```sh
gcloud auth application-default login
export BQ_PROJECT="your-gcp-project-id"
export BQ_DATASET="dbt_retail"
export BQ_LOCATION="US"
```

Keep the profile inside this edition so it cannot overwrite another project's dbt configuration:

```sh
mkdir -p .dbt
cp -n profiles.example.yml .dbt/profiles.yml
export DBT_PROFILES_DIR="$PWD/.dbt"
```

The `-n` option preserves an existing local profile. Set `BQ_PROJECT`, and optionally `BQ_DATASET` and `BQ_LOCATION`, in your shell before running dbt. Keep `.dbt/profiles.yml` and credentials out of Git.

## Install and run

From `editions/Bigquery/`, install dependencies and run dbt:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-lock.txt
dbt debug
dbt build
```

`dbt build` reads the public source tables and creates staging views and marts in your destination dataset. Unlike the Snowflake edition, this example uses public warehouse data rather than CSV seeds.

## Cost and CI

The profile sets `maximum_bytes_billed` to 1,000,000,000 bytes per query as a guardrail. This is not a total spend cap: multiple queries under the limit, storage, and other services can still incur charges. Review query estimates and your Google Cloud billing or Sandbox limits before running. Queries against public tables still use your project for job processing. The sales mart is partitioned by date and clustered by product; use these fields when querying it to reduce scanned data.

GitHub Actions runs `dbt parse` without cloud credentials. This checks project configuration but does not confirm IAM access, source availability, or successful warehouse execution.

## Optional migration datasets and access

The optional migration extension writes raw Bronze data with a separate migration loader, then reads Bronze and writes dbt Silver and Gold models. Pre-create all three datasets in the same `US` location as the source and target jobs:

- `starterkit_migration_bronze` for dlt-loaded raw tables (`MIGRATION_BRONZE_NAMESPACE`).
- `starterkit_migration_silver` for the migration dbt Silver models.
- `starterkit_migration_gold` for the migration dbt Gold marts.

The migration dbt target dataset defaults to `starterkit_migration` (`MIGRATION_DBT_DATASET`). The dbt project appends its custom schema names, `silver` and `gold`, to that target dataset. If you change the target dataset or location, pre-create the corresponding Silver and Gold datasets there before building.

Grant access by workload, preferably to separate identities:

| Identity | Project-level role | Dataset-level roles |
|---|---|---|
| Migration loader | BigQuery Job User (`roles/bigquery.jobUser`) on the project | BigQuery Data Editor (`roles/bigquery.dataEditor`) on `starterkit_migration_bronze` |
| dbt migration runner | BigQuery Job User (`roles/bigquery.jobUser`) on the project | BigQuery Data Viewer (`roles/bigquery.dataViewer`) on Bronze; BigQuery Data Editor (`roles/bigquery.dataEditor`) on Silver and Gold |

These grants let the loader write Bronze and let dbt read Bronze and create/update migration models. Pre-create the datasets so neither identity needs project-level dataset-creation privileges. If one identity runs both workloads, combine the listed grants. Set `BQ_PROJECT` and `DESTINATION__BIGQUERY__PROJECT_ID` to the same project; keep `BQ_LOCATION` and all three dataset locations aligned. The loader's target confirmation must use that project ID. See the [migration guide](../../../migration/README.md) for the dry-run and load sequence.

To provision the optional warehouse build target and configure CI in one Terraform apply, follow the [BigQuery Terraform guide](../infra/terraform/README.md). It creates a dedicated dataset, narrowly scoped CI identity, repository-scoped Workload Identity Federation, and the four identifier-only Actions variables consumed by the workflow. The job remains manually dispatched from `main`, uses the profile's 1 GB per-query bytes-billed limit, and does not delete the dataset after a run. The byte limit does not cap total workflow cost. Never use production or customer data for this target. See Google's [GitHub Actions authentication guide](https://github.com/google-github-actions/auth#workload-identity-federation-through-a-service-account) for the authentication flow.

Edit `requirements.txt`, then regenerate the universal Python 3.11 lock from this directory with `uv pip compile requirements.txt --python-version 3.11 --universal --generate-hashes --output-file requirements-lock.txt` (requires `uv` on `PATH`). Commit both files together.
