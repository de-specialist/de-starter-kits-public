# Optional Snowflake migration models

The Snowflake edition includes an optional Silver/Gold dbt layer for data loaded into Snowflake by the shared dlt migration command. These models are disabled for a normal `dbt build`; no change to the regular seed-based example is required.

## Bronze source contract

The dbt source `migration_bronze` reads the database from `SNOWFLAKE_DATABASE` and the schema from `MIGRATION_BRONZE_NAMESPACE` (default `STARTERKIT_MIGRATION_BRONZE`). The dlt loader must target that same database and Bronze schema. The SQL source URL is used only by the migration loader; dbt uses its own local Snowflake profile.

The included retail transformations expect these Bronze table names and fields:

| Table | Expected columns |
|---|---|
| `customers` | `customer_id`, `created_at`, `country` |
| `products` | `product_id`, `product_name`, `category` |
| `orders` | `order_id`, `customer_id`, `order_status`, `order_date`, `updated_at` |
| `order_items` | `order_item_id`, `order_id`, `product_id`, `quantity`, `unit_price` |

The migration loader adds `_ingested_at_utc` once per run. Since this is a full replacement, that timestamp is equal across rows in the same run and does not identify the latest row for customers, products, or order items; canonical field-value tie-breakers choose a stable winner only when typed input values and session-timezone settings are identical. Orders sort first by source `updated_at`. These deterministic choices do not prove business-correct latest-row semantics when conflicting source rows lack a row-level version timestamp. **Cross-warehouse timestamp/date parity is not verified.** As a review inference, Snowflake timestamp casts and timestamp-to-date conversion may depend on timestamp type and session timezone. This could change the ordering used for deduplication or the date assigned to daily sales compared with BigQuery. Explicitly align UTC conversion/session behavior and test timezone-boundary fixtures in both warehouses before claiming parity; no warehouse verification has been performed. CSV filenames become table names. PostgreSQL or another relational source must expose the same canonical names and columns, or its Silver models must be adapted. Loading arbitrary tables to Bronze does not make them compatible with these retail marts.

## Local profile and role

Copy `kit/profiles.example.yml` to the ignored `kit/.dbt/profiles.yml` and set `DBT_PROFILES_DIR` to that directory as described in the [setup guide](setup.md). The profile includes a separate `migration` target. It uses key-pair authentication and defaults to the custom, non-admin `DBT_MIGRATION_ROLE`; it does not inherit the regular development target's role. Set `MIGRATION_SNOWFLAKE_ROLE` only if your administrator has provisioned a different non-admin role. Set `SNOWFLAKE_PRIVATE_KEY_PATH` to a local key file registered for that user and role. If an existing key is restricted to another role, use a separate authorized key or user instead of weakening the restriction. Keep all private keys outside the repository.

Set the connection and namespace variables in the same local shell before invoking dbt. These are placeholders, not account credentials:

```sh
export SNOWFLAKE_ACCOUNT="YOUR_ACCOUNT_IDENTIFIER"
export SNOWFLAKE_USER="YOUR_NONADMIN_DBT_USER"
export SNOWFLAKE_DATABASE="YOUR_NONPROD_DATABASE"
export SNOWFLAKE_WAREHOUSE="YOUR_SMALL_AUTO_SUSPENDING_WAREHOUSE"
export SNOWFLAKE_PRIVATE_KEY_PATH="/absolute/path/to/local/dbt_migration_key.p8"
export MIGRATION_SNOWFLAKE_ROLE="DBT_MIGRATION_ROLE"
export MIGRATION_BRONZE_NAMESPACE="STARTERKIT_MIGRATION_BRONZE"
export MIGRATION_DBT_SCHEMA="STARTERKIT_MIGRATION"
```

Ask a Snowflake administrator to create or approve the custom role and grant only the access it needs. The role needs `USAGE` on the target database and warehouse, `USAGE` plus `SELECT` on the Bronze schema/tables, and permission to create its dbt output schemas. If dbt is allowed to create them, grant `CREATE SCHEMA` on this dedicated database. Otherwise pre-create the exact output schemas and grant the role `USAGE`, `CREATE TABLE`, `CREATE VIEW`, and `CREATE STAGE` there. Grant `SELECT` on future Bronze tables if the dlt loader will replace or add tables. Do not use `ACCOUNTADMIN`, `SYSADMIN`, or a production transformation role for routine runs.

An administrator or the Bronze schema owner can grant these privileges to the existing custom role, after replacing the database and warehouse placeholders:

```sql
GRANT USAGE ON DATABASE <nonprod_database> TO ROLE DBT_MIGRATION_ROLE;
GRANT USAGE ON WAREHOUSE <small_auto_suspending_warehouse> TO ROLE DBT_MIGRATION_ROLE;
GRANT CREATE SCHEMA ON DATABASE <nonprod_database> TO ROLE DBT_MIGRATION_ROLE;
GRANT USAGE ON SCHEMA <nonprod_database>.STARTERKIT_MIGRATION_BRONZE TO ROLE DBT_MIGRATION_ROLE;
GRANT SELECT ON ALL TABLES IN SCHEMA <nonprod_database>.STARTERKIT_MIGRATION_BRONZE TO ROLE DBT_MIGRATION_ROLE;
GRANT SELECT ON FUTURE TABLES IN SCHEMA <nonprod_database>.STARTERKIT_MIGRATION_BRONZE TO ROLE DBT_MIGRATION_ROLE;
```

With the default target schema `STARTERKIT_MIGRATION`, dbt's default custom-schema naming generates `STARTERKIT_MIGRATION_silver` and `STARTERKIT_MIGRATION_gold`. Snowflake may display unquoted identifiers in uppercase. The Bronze schema remains separate. Change the target base schema only to a unique prefix reserved for this migration.

## Run the opt-in build

After the dlt load has completed and you have reconciled the selected Bronze tables, run from `editions/Snowflake/kit/` with the local profile configured:

```sh
dbt debug --target migration
dbt build --target migration --select tag:medallion --vars '{enable_migration_models: true}'
```

The explicit variable enables only the models under `models/migration/` when the selected target is also named `migration`. Without both conditions, the optional layer stays disabled and the regular Snowflake example remains the default. Review model tests and compare source counts and agreed Gold metrics before cutover. The loader currently performs full replacement for selected tables; these models do not add CDC, delete propagation, or automatic schema mapping. See the shared [migration guide](../../../docs/medallion-migration-extension.md) for the loader workflow and its limits.

Gold treats `complete` and `completed` as completed orders. `returned_value` reports the original extended item value for orders whose status is exactly `returned`; returned orders are excluded from completed counts, quantities, and revenue. This is not a refund amount, partial-return calculation, or net-sales metric. Other statuses, including cancelled or unrecognized values, are excluded from both completed and returned measures.
