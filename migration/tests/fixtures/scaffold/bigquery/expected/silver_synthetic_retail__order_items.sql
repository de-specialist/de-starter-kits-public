-- scaffolded: editable Silver starter model (bigquery-silver-v1, source alias synthetic_retail).
-- Ownership: operator-owned after publication. The scaffolder never reads, rewrites,
-- renames, or deletes this file; a new scaffold needs a fresh client workspace.
-- Opt-in: enabled only with --target migration and --vars '{enable_migration_models: true}'.
-- REVIEW: cast placeholders pending items 138 (temporal contract) and 139 (destination type profiles):
--   unit_price (numeric).
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
    cast(`line_id` as string) as order_item_id,
    case
        when replace(cast(`order_number` as string), ' ', '') = '' then cast(null as string)
        else cast(`order_number` as string)
    end as order_id,
    case
        when replace(cast(`sku` as string), ' ', '') = '' then cast(null as string)
        else cast(`sku` as string)
    end as product_id,
    cast(`units` as int64) as quantity,
    cast(`item_price` as numeric) as unit_price,
    _migration_run_id,
    _ingested_at_utc
from {{ source('migration_bronze_synthetic_retail', 'order_items') }}
where cast(`line_id` as string) is not null
  and replace(cast(`line_id` as string), ' ', '') <> ''
