-- scaffolded: editable Silver starter model (snowflake-silver-v1, source alias synthetic_retail).
-- Ownership: operator-owned after publication. The scaffolder never reads, rewrites,
-- renames, or deletes this file; a new scaffold needs a fresh client workspace.
-- Opt-in: enabled only with --target migration and --vars '{enable_migration_models: true}'.
-- REVIEW: cast placeholders pending items 138 (temporal contract) and 139 (destination type profiles):
--   order_date (date -> date), updated_at (timestamp -> timestamp_ntz).
-- timestamp_ntz keeps wall-clock values without a time zone and its conversion
-- depends on the session time zone; confirm UTC handling before relying on it.
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
    order_number::varchar as order_id,
    iff(replace(buyer_id::varchar, ' ', '') = '', null::varchar, buyer_id::varchar) as customer_id,
    status::varchar as order_status,
    purchase_date::date as order_date,
    modified_at::timestamp_ntz as updated_at,
    _migration_run_id,
    _ingested_at_utc
from {{ source('migration_bronze_synthetic_retail', 'orders') }}
where order_number::varchar is not null
  and replace(order_number::varchar, ' ', '') <> ''
