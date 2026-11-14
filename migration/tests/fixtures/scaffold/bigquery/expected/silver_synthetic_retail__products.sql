-- scaffolded: editable Silver starter model (bigquery-silver-v1, source alias synthetic_retail).
-- Ownership: operator-owned after publication. The scaffolder never reads, rewrites,
-- renames, or deletes this file; a new scaffold needs a fresh client workspace.
-- Opt-in: enabled only with --target migration and --vars '{enable_migration_models: true}'.
-- No deduplication: Bronze rows pass through one to one. Deduplication or latest-row
-- logic needs a source row-version field; the unique test on the primary key exposes
-- duplicate keys. Key policy (item 136): string primary keys drop NULL, empty, and
-- ASCII-space-only values; string foreign keys map those values to NULL.

{{
    config(
        materialized='view',
        schema='silver',
        tags=['scaffolded'],
        enabled=(target.name == 'migration' and (var('enable_migration_models', false) | as_bool))
    )
}}

select
    cast(`sku` as string) as product_id,
    cast(`product_title` as string) as product_name,
    cast(`product_group` as string) as category,
    _migration_run_id,
    _ingested_at_utc
from {{ source('migration_bronze_synthetic_retail', 'products') }}
where cast(`sku` as string) is not null
  and replace(cast(`sku` as string), ' ', '') <> ''
