# Snowflake development operations

## Warehouse cost controls

Use a dedicated, small development warehouse with auto-resume and a short auto-suspend interval. The setup example uses an X-Small warehouse that suspends after 60 seconds idle. Auto-suspend stops idle compute use; it does not remove stored tables or other account-level charges.

The examples below use the repository Terraform naming pattern from the [setup guide](setup.md#target-naming). Replace `<suffix>` with the value in your dbt profile (for the Terraform default prefix, `TF`).

Check that the warehouse is suspended after a run:

```sql
SHOW WAREHOUSES LIKE 'DBT_KIT_DEV_<suffix>_WH';
```

Suspend it manually when finished:

```sql
ALTER WAREHOUSE DBT_KIT_DEV_<suffix>_WH SUSPEND;
```

Keep GitHub Actions on its own disposable `DBT_KIT_CI_<suffix>_DB` database, matching `DBT_KIT_CI_<suffix>_SCHEMA` schema, and `DBT_KIT_CI_<suffix>_WH` warehouse with a non-production role.

For reviewing billed credits and hourly warehouse usage, see the [Snowflake cost checks guide](cost-checks.md). Credits are not currency; reconcile account-specific pricing against the invoice or usage statement.

## CI execution boundary

Pull requests run `dbt parse` against the placeholder profile in `kit/.ci/profiles.yml` and do not receive Snowflake secrets. The credentialed build runs only after a push to `main`, where it executes `dbt build` against the configured `DBT_KIT_CI_<suffix>_SCHEMA` (or `DBT_KIT_DEV_<suffix>_SCHEMA`) schema; its pre-build guard rejects any other database or schema name. Protect `main` with required review before enabling repository secrets for this workflow. `dbt build` includes the seed nodes, so the workflow does not need a separate `dbt seed` step.

## Remove development outputs

The dbt target schema contains generated views and tables, including the incremental example. If you provisioned the target with Terraform, remove it with the interactive lifecycle command described in the [Terraform README](../infra/terraform/README.md#tear-down) rather than with manual SQL.

For a manually provisioned target, inspect it before removing anything:

```sql
SHOW TABLES IN SCHEMA DBT_KIT_DEV_<suffix>_DB.DBT_KIT_DEV_<suffix>_SCHEMA;
SHOW VIEWS IN SCHEMA DBT_KIT_DEV_<suffix>_DB.DBT_KIT_DEV_<suffix>_SCHEMA;
```

Only when the schema name begins with `DBT_KIT_DEV_`, it was created solely for this kit, and you have confirmed its contents, remove it with:

```sql
DROP SCHEMA IF EXISTS DBT_KIT_DEV_<suffix>_DB.DBT_KIT_DEV_<suffix>_SCHEMA CASCADE;
```

This permanently deletes every object in that schema. Never run a drop against a database or schema without the `DBT_KIT_DEV_` or `DBT_KIT_CI_` prefix, or against any shared or production object. It leaves the database, role, and warehouse in place; ask your Snowflake administrator to review those resources separately if you no longer need them.

## Incremental order example

`fct_orders_incremental` is disabled in the default build and is a teaching example, not a production-ready CDC pipeline. Enable and build it and its dependencies with `dbt build --select +fct_orders_incremental --vars '{enable_incremental_example: true}'`. It merges by `order_id` and reprocesses a three-day window (`incremental_lookback_days`, default `3`) based on `source_updated_at`. This window must be at least as long as the source's expected update delay; change it when adapting the model. Rows updated outside that window can be missed unless you perform a full refresh. Two more knobs are explicit: rows with a null `source_updated_at` are always re-selected, and `incremental_future_skew_days` (default `1`) bounds how far ahead of the build date a timestamp may be before it is treated as future-dated.

The first build creates the table. To simulate a correction, update an existing row's `amount`, `status`, or `source_updated_at` in `seeds/raw_orders.csv`, then reload the seed and rebuild the model:

```sh
dbt seed --select raw_orders --full-refresh
dbt build --select +fct_orders_incremental --vars '{enable_incremental_example: true}'
```

For a complete rebuild of the incremental target:

```sh
dbt build --full-refresh --select +fct_orders_incremental --vars '{enable_incremental_example: true}'
```

### Timestamp edge contract

`source_updated_at` is treated as a UTC timestamp without a time zone. Each row receives a `watermark_status` when it is written:

| `watermark_status` | Condition | Incremental behavior |
|---|---|---|
| `missing` | `source_updated_at` is null | Re-selected on every incremental run (the incremental filter includes `watermark_status in ('missing', 'future')`), so the merged row matches a full refresh; never sets the watermark. |
| `future` | At or after the future cutoff: `current_date()` in the Snowflake session plus `incremental_future_skew_days` days (default `1`, the start of tomorrow) | Re-selected on every incremental run; never sets the watermark, so a mistyped or skewed clock cannot freeze it. Once the build date passes it, the row is rewritten as `valid`. |
| `valid` | Any other timestamp | Sets the watermark. Reprocessed while it is at or after the window start. |

The watermark is the target's maximum `source_updated_at` among rows stored as `valid` (and earlier than the future cutoff); if there are none, every row is selected. The window start is the watermark minus the lookback and is inclusive: a row exactly on it is reprocessed, and every row sharing that timestamp is selected. A late arrival whose timestamp is at or after the window start is recovered by the next run; a valid correction whose timestamp is earlier than the window start (a late arrival with a stale timestamp) is skipped until a full refresh. Widen `incremental_lookback_days` to recover older late arrivals at the cost of a larger scan. The merge does not remove rows for source hard deletes. Two source rows with the same `order_id` in one run fail the merge when that order is already in the target; otherwise both are inserted and the `unique` test fails.

Because null and future-dated rows are reprocessed on every run, a large number of them increases scan and merge cost; resolve them at the source. The cutoff follows the session time zone used by `current_date()`, so a session set ahead of UTC treats up to one extra day as valid. Set `incremental_future_skew_days` to a whole number of at least `1`: `0` would classify today's rows as future-dated, and a large value lets a mistyped timestamp set the watermark and hide later updates until a full refresh. Pass the same value to the build and the singular test (for example `--vars '{enable_incremental_example: true, incremental_future_skew_days: 2}'`).

Re-selecting null timestamps on every run is a deliberate choice: the alternative, skipping them after the first load, keeps runs cheaper but lets the incremental target silently diverge from a full refresh whenever a null-timestamp row changes. To reverse the choice, remove only `'missing'` from the `watermark_status in ('missing', 'future')` predicate in the incremental filter (keep `'future'`, or future-dated rows stop being re-selected), and document that null-timestamp updates need a full refresh. If you also change the `not_null` test on `stg_orders.source_updated_at` to fail, remove or fix the null-timestamp order `5005` in the synthetic seed first, or `dbt build` fails.

The synthetic seed exercises each case: order `5003` sits exactly on the window start (`2026-01-09 10:00:00`, three days before the `2026-01-12 10:00:00` watermark), `5005` has a null timestamp, and `5006` is dated `2099-01-01`. Because of `5005`, the `not_null` test on `stg_orders.source_updated_at` is set to warn rather than fail. After a full refresh or an incremental run, the singular test `assert_fct_orders_incremental_timestamp_contract` checks that every missing, future, and in-window staging row matches the target, including its `watermark_status`, and that `watermark_status` agrees with timestamp nullness for every row. It does not check rows older than the window start, which may legitimately differ until a full refresh. One known false positive: if the latest valid row's `source_updated_at` is later set to null, the watermark moves earlier and the test reports unchanged-timestamp edits between the new and old window starts; a full refresh clears it. Upgrading an existing target to add `watermark_status` requires a `--full-refresh`.

This contract has been checked only with `dbt parse` and the offline regression `scripts/tests/test_snowflake_incremental_timestamp_contract.py`, which renders the model, transpiles it to DuckDB with `sqlglot`, and compares incremental runs with full refreshes for null, future-dated, boundary, and late-arrival rows. Validate execution, session time zones, late corrections and incremental equivalence against your own isolated Snowflake target before relying on warehouse-specific behavior.
