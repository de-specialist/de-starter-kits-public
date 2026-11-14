# Snowflake edition: data pipeline starter

This dbt + Snowflake example uses synthetic customer and order seeds to demonstrate staging models, a customer orders mart, schema tests, and GitHub Actions CI.

## Quick start

From this edition folder (or a copied edition folder), install its pinned dependencies:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-lock.txt
mkdir -p kit/.dbt
cp -n kit/profiles.example.yml kit/.dbt/profiles.yml
```

Set `DBT_PROFILES_DIR` to the local profile directory and configure key-pair or password authentication as described in [`docs/setup.md`](docs/setup.md). The copy command preserves an existing local profile. From the repository root, run the local preflight, then run the project:

```sh
export DBT_PROFILES_DIR="$PWD/editions/Snowflake/kit/.dbt"
python scripts/first_run_preflight.py snowflake --workspace editions/Snowflake/kit
cd editions/Snowflake
export DBT_PROFILES_DIR="$PWD/kit/.dbt"
(cd kit && dbt debug && dbt build)
```

In a scaffolded workspace or downloaded archive, run these commands from the workspace root before `cd kit`:

```sh
export DBT_PROFILES_DIR="$PWD/kit/.dbt"
python scripts/first_run_preflight.py snowflake --workspace .
```

The preflight checks local Python/dbt packages, lock/profile files, and profile isolation; it does not read profile contents or contact Snowflake.

## Models and metrics

- `fct_customer_orders` has one row per customer in `stg_customers`, including customers with no orders. `completed_order_count` counts orders with status `completed`; `completed_revenue` sums their `amount` (0 when none); `most_recent_completed_order` is the latest completed `order_date`. Cancelled orders are excluded from all three.
- `fct_orders_incremental` (opt-in, `enable_incremental_example: true`) has one row per `order_id`. It merges rows whose `source_updated_at` falls in a three-day lookback window, and always reprocesses rows with a null (`watermark_status = 'missing'`) or future-dated (`'future'`) timestamp. Only `valid` timestamps set the watermark. See the [timestamp edge contract](docs/operations.md#timestamp-edge-contract).

See the [setup guide](docs/setup.md), [development operations guide](docs/operations.md), [cost checks guide](docs/cost-checks.md), and [adaptation guide](docs/adapting-the-kit.md). The example rows are synthetic; replace them with governed source tables before using the models for a real business.

For the opt-in dlt Bronze-to-Silver/Gold migration path and its non-admin profile setup, see the [Snowflake migration guide](docs/migration.md).

For an isolated disposable Snowflake development database, schema, and auto-suspending warehouse, see the [Terraform infrastructure guide](infra/terraform/README.md). Review its role requirements and destructive teardown instructions before applying.
