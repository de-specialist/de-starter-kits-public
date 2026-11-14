# BigQuery edition: online retail analytics

This project transforms Google's public [TheLook eCommerce](https://console.cloud.google.com/marketplace/product/bigquery-public-data/thelook-ecommerce) dataset into reusable product and customer sales marts. The curated models avoid direct identifiers such as names, email addresses, and street addresses.

## What it demonstrates

- BigQuery sources, dbt staging models, a product-level daily sales mart, and a customer value mart.
- Model tests for unique/non-null keys and relationships.
- BigQuery partitioning, a per-query bytes-billed ceiling, and credential-free CI parsing with an optional trusted warehouse build.
- An optional incremental daily sales mart that reprocesses a configurable 90-day lookback window.

## Quick start

For IAM roles, dataset setup, and locations, see the [detailed setup guide](docs/setup.md). For actual billed spend versus query-byte checks, see the [BigQuery cost-check guide](docs/cost-check.md). To provision an isolated dataset and keyless CI identity with Terraform, see [Terraform infrastructure](infra/terraform/README.md).

Use Python 3.11, the Google Cloud CLI, and the edition's hash-verified dependencies from this directory:

```sh
mkdir -p .dbt
python -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-lock.txt
gcloud auth application-default login
cp -n profiles.example.yml .dbt/profiles.yml
export DBT_PROFILES_DIR="$PWD/.dbt"
```

Set `BQ_PROJECT` to a Google Cloud project where you can run BigQuery jobs. Create the target dataset with the location and permissions described in the [setup guide](docs/setup.md). Optionally set `BQ_DATASET` (default `dbt_retail`) and `BQ_LOCATION` (default `US`; it must match the public source location). After creating the local profile and setting the project, run the credential-free preflight from the repository root:

```sh
python scripts/first_run_preflight.py bigquery --workspace editions/Bigquery
```

In a scaffolded workspace or downloaded archive, run `python scripts/first_run_preflight.py bigquery --workspace .` from the project directory. The preflight checks local Python/dbt packages, lock/profile files, profile isolation, and that `BQ_PROJECT` (and `BQ_DATASET`, if set) are not production-like. It does not read profile contents, print variable values, or contact Google Cloud. When it exits `0`, run from this directory:

```sh
dbt debug
dbt build
```

The build reads the public source tables and creates models in your project's target dataset. Public table storage is hosted by Google, but queries run in your project and count toward its query processing. Check bytes processed before running, keep the configured `maximum_bytes_billed` guard, and follow the [BigQuery Sandbox limits](https://cloud.google.com/bigquery/docs/sandbox) if using the no-billing sandbox.

## Business metric definitions

- `completed_revenue` includes order items with `status = 'complete'`, using item `sale_price` and item creation date.
- `returned_value` sums `sale_price` for items with `status = 'returned'` by item creation date. It is a diagnostic value, not a refund accounting ledger.
- `completed_order_count` counts distinct completed orders associated with a customer.
- `completed_revenue` in the customer mart is gross completed-item value, not margin or net revenue.

## Dashboard idea

Use Looker Studio's BigQuery connector to build a simple report from the two marts. Follow the [Looker Studio walkthrough](docs/looker-studio.md) for field mappings, chart setup, SQL checks, and cost guardrails. The connector bills query processing to the selected Google Cloud project, so dashboards should use the pre-aggregated marts and avoid repeatedly querying raw sources. See Google's [Looker Studio overview](https://docs.cloud.google.com/data-studio/welcome) and [BigQuery connector guide](https://docs.cloud.google.com/data-studio/connect-to-google-bigquery).

TheLook is a fictional retailer dataset. Review the dataset's terms and suitability before redistributing data or using the project with real customer information.

The optional `mart_daily_product_sales_incremental` model is disabled in the default build. Run it and its dependencies with `dbt build --select +mart_daily_product_sales_incremental --vars '{enable_incremental_example: true}'`.

Each incremental run rebuilds a bounded lookback window: today and the previous `incremental_lookback_days` days (default 90, by `current_date()` in UTC). It uses dbt-bigquery's static `insert_overwrite` strategy, so every daily partition in that window is replaced wholesale from the source, including partitions that end up with no rows. The source scan is filtered to the same dates.

What this guarantees, for dates inside the window, once the run completes:

- Late status, price, or product-attribute changes are recomputed from all current source items.
- A date/product group whose last item was hard-deleted, or moved to a different date or product inside the window, is removed instead of keeping a stale aggregate.
- Rerunning without source changes is idempotent.

Limits:

- Dates before the window are never rewritten. Changes there, including deletes or moves out of an old group, still need `dbt build --full-refresh --select +mart_daily_product_sales_incremental --vars '{enable_incremental_example: true}'`. The model does not detect older changes.
- Items dated after `current_date()` are skipped by incremental runs until their date enters the window; a full refresh includes them immediately.
- `incremental_lookback_days` must be a whole number from 0 to 3650 (BigQuery limits one job to 4,000 modified partitions); other values fail at compile time.
- This is not CDC. On 2026-11-01 one warehouse run on a disposable dataset matched a full refresh row for row on the current source snapshot. Late-change and stale-group behavior is checked only offline, by `scripts/tests/test_bigquery_incremental_stale_groups.py`, which emulates the adapter merge in DuckDB.

To replay older dates without a full refresh, widen the window to include the affected date. Each run scans and rewrites every day in the window, so review the expected query bytes first:

```sh
dbt build --select +mart_daily_product_sales_incremental \
  --vars '{enable_incremental_example: true, incremental_lookback_days: 365}'
```

Because every partition in the widened window is rebuilt, this also removes stale groups on those dates.

When enabled, the build also runs a singular test that checks the incremental mart has no duplicate `order_item_date`/`product_id` keys. The test stays disabled with the model in the default build.

## Dependency updates

Edit `requirements.txt`, then regenerate the universal Python 3.11 lock from this directory with `uv pip compile requirements.txt --python-version 3.11 --universal --generate-hashes --output-file requirements-lock.txt` (requires `uv` on `PATH`). Commit both files together. GitHub Actions installs the hash-verified lock file.
