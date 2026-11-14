-- scaffolded: editable Silver starter model (snowflake-silver-v1, source alias synthetic_retail).
-- Ownership: operator-owned after publication. The scaffolder never reads, rewrites,
-- renames, or deletes this file; a new scaffold needs a fresh client workspace.
-- Opt-in: enabled only with --target migration and --vars '{enable_migration_models: true}'.
-- REVIEW: cast placeholders pending items 138 (temporal contract) and 139 (destination type profiles):
--   unit_price (numeric -> number(28, 6)).
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
    line_id::varchar as order_item_id,
    iff(replace(order_number::varchar, ' ', '') = '', null::varchar, order_number::varchar) as order_id,
    iff(replace(sku::varchar, ' ', '') = '', null::varchar, sku::varchar) as product_id,
    units::number(38, 0) as quantity,
    item_price::number(28, 6) as unit_price,
    _migration_run_id,
    _ingested_at_utc
from {{ source('migration_bronze_synthetic_retail', 'order_items') }}
where line_id::varchar is not null
  and replace(line_id::varchar, ' ', '') <> ''
