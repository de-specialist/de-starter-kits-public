# Glossary

Terms used across these docs, in the sense this kit uses them. Links point to the page that documents each one in detail.

## Layers and models

Edition
:   One of the two independent dbt projects: BigQuery (`editions/Bigquery/`) or Snowflake (`editions/Snowflake/kit/`). Editions share a layout but not data, profiles, dependencies, or generated artifacts. See [edition workspaces](edition-workspaces.md).

Source
:   A table dbt reads but does not build, declared in `sources.yml`. The BigQuery edition reads the public TheLook eCommerce tables in place; the migration models read the Bronze namespace.

Seed
:   A small CSV file that `dbt seed` loads as a table. The Snowflake edition's `raw_customers` and `raw_orders` seeds are synthetic.

Staging
:   Models that clean one source or seed each: rename columns, cast types, and standardize status values. No business logic. Staging models are views.

Mart
:   A model that applies business logic and aggregates staged rows for reporting, such as `mart_daily_product_sales` or `fct_customer_orders`. Marts are tables. See the [mart contract](mart-contract.md).

Grain
:   What one row of a model represents, for example "one row per order-item date and product". Tests check that the grain's key is unique. Comparing two marts only makes sense when their grains match.

Medallion layers
:   The Bronze, Silver, and Gold layout used by the optional migration extension. See [medallion migration](medallion-migration-extension.md).

Bronze
:   Source-shaped rows that the migration loader copies into a dedicated namespace, plus load metadata such as `_migration_run_id` and `_ingested_at_utc`.

Silver
:   Views that cast types, normalize retail fields, and deduplicate the four canonical retail tables from Bronze.

Gold
:   Tables that implement the documented retail metrics from Silver, such as `mart_migration_customer_value`.

Materialization
:   How dbt builds a model: as a view, a table, or an incremental table.

Completed revenue
:   Gross value from completed activity only. It is not profit, margin, or net revenue. See the [mart contract](mart-contract.md).

Direct identifier
:   A field that identifies a person on its own, such as name, email, or street address. Curated example marts leave these out unless a use case requires them. The Snowflake synthetic `customer_name` is the documented exception; see the [mart contract](mart-contract.md).

Synthetic data
:   Invented example rows that describe no real person or business, such as the Snowflake seeds and the migration fixtures. Always labelled as synthetic.

## Contracts

Contract
:   In this kit, a written agreement that a model or tool must keep. These are Markdown documents, not dbt's `contract: enforced` model setting, which neither edition uses.

Mart contract
:   The grain, inputs, and metric definitions of every mart, and which metrics match across editions. See [mart contract](mart-contract.md).

Source change contract
:   What must be known about how a source's records arrive and change before building an incremental model on it. See [source change contract](source-change-contract.md).

Source snapshot contract
:   What the migration loader does and does not enforce about source consistency between inventory and extraction. See the [source snapshot contract](source-snapshot-contract.md).

## Incremental loading

Incremental model
:   A model that processes only part of its data on each run instead of rebuilding everything. Both editions have an optional incremental example, disabled by default: a BigQuery daily sales mart and the Snowflake `fct_orders_incremental` order model.

Lookback window
:   The recent period an incremental run reprocesses to pick up late changes. The BigQuery example rebuilds today plus the previous `incremental_lookback_days` days (default 90). The Snowflake example reprocesses rows within `incremental_lookback_days` (default 3) of its watermark, the latest stored `source_updated_at`. Changes older than the window need a full refresh.

`insert_overwrite`
:   A BigQuery incremental strategy that replaces whole date partitions. Because each listed partition is rebuilt from source, deleted or moved rows inside the window leave no stale groups.

Full refresh
:   `dbt build --full-refresh`: rebuild an incremental model from scratch. Use it after logic changes or to repair data older than the lookback window.

Partitioning and clustering
:   BigQuery table layout. Partitioning by date lets queries and incremental runs touch only the dates they need; clustering by product orders data within each partition.

CDC
:   Change data capture: recording every insert, update, and delete at the source. The kit does not implement CDC; its incremental example and the migration loader are not CDC.

Watermark
:   The timestamp or position that marks how far a source has been processed. See the [source change contract](source-change-contract.md).

## Running safely

Profile
:   dbt's connection settings in a local `profiles.yml`. Never commit it. Each edition ships `profiles.example.yml` as a template.

Target
:   A named connection inside a profile, such as `dev`, `ci`, or `migration`. The migration models build only for the `migration` target.

Placeholder `.ci` profile
:   The committed profile in each edition's `.ci/` folder that lets CI run `dbt parse` and generate docs with no warehouse credentials.

Credential-free parse
:   `dbt parse` with the placeholder profile. It checks project configuration and SQL references but does not prove the models run in a warehouse.

Disposable target
:   A dedicated BigQuery dataset (prefixed `starterkit_`) or Snowflake database and schema (prefixed `DBT_KIT_DEV` or `DBT_KIT_CI`) created for one run and torn down afterwards. See the [cloud acceptance runbook](cloud-acceptance-runbook.md).

Preflight
:   A local check that runs before anything contacts a warehouse. The first-run preflight (`scripts/first_run_preflight.py`) checks packages, lock files, and profile paths. The cloud acceptance preflight (`scripts/cloud_acceptance_preflight.py`) checks that the profile resolves to the confirmed disposable target. Neither makes a network call, and a pass does not prove access or cost.

Cloud acceptance
:   An authenticated `dbt build` against a disposable target, recorded as release evidence. See the [runbook](cloud-acceptance-runbook.md) and the record.

Human gate
:   A step that a named person must perform and sign off, such as provisioning, spending money, or destroying resources. The runbook records when the owner has delegated a gated step.

Bytes-billed guard
:   BigQuery's `maximum_bytes_billed` profile setting. It fails any single query that would bill more than the limit (1 GB in the examples). It is not a total spending cap.

Auto-suspend
:   A Snowflake warehouse setting that stops compute after a period of inactivity. It stops idle compute charges but not storage charges.

Hash lock
:   An edition's `requirements-lock.txt`, which pins every Python dependency with hashes. CI installs with `--require-hashes`.

Workload identity federation
:   Keyless authentication that lets a GitHub Actions run act as a Google Cloud service account without a stored key. The BigQuery CI warehouse build uses it.

## Migration

Migration loader
:   `migration/run_migration.py`, which copies selected local CSV files or SQL tables into a Bronze namespace with dlt, using full replacement. See the [migration loader](../migration/README.md).

Dry run
:   `--dry-run`: inventory the source and resolve the plan without writing data. Its result is never a cloud-write go-ahead on its own.

Schema confirmation fingerprint
:   A hash of the selected tables, columns, type families, and mapping configuration, printed by the dry run. A real load must repeat it exactly, so a changed source stops the run.

Reconciliation
:   Comparing counts or totals between layers, for example source to Bronze or Bronze to Gold, to show that a move did not lose or duplicate rows.

## Delivery

Scaffolder
:   `scripts/scaffold_edition.py`, which copies one edition into a new workspace outside the repository and refuses to copy credential-like files. See [edition workspaces](edition-workspaces.md).

Release archive
:   A distributable bundle of one edition, built by `scripts/package_edition.py` and checked with the archive audit.

Empty catalog
:   dbt docs generated with `--empty-catalog`, which skips warehouse metadata. The published dbt docs show models, descriptions, tests, and lineage but no warehouse column types.
