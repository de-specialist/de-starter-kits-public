-- scaffolded: editable Silver starter model (bigquery-silver-v1, source alias synthetic_retail).
-- Ownership: operator-owned after publication. The scaffolder never reads, rewrites,
-- renames, or deletes this file; a new scaffold needs a fresh client workspace.
-- Opt-in: enabled only with --target migration and --vars '{enable_migration_models: true}'.
-- REVIEW: cast placeholders pending items 138 (temporal contract) and 139 (destination type profiles):
--   order_date (date), updated_at (timestamp).
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
    cast(`order_number` as string) as order_id,
    case
        when replace(cast(`buyer_id` as string), ' ', '') = '' then cast(null as string)
        else cast(`buyer_id` as string)
    end as customer_id,
    cast(`status` as string) as order_status,
    cast(`purchase_date` as date) as order_date,
    cast(`modified_at` as timestamp) as updated_at,
    _migration_run_id,
    _ingested_at_utc
from {{ source('migration_bronze_synthetic_retail', 'orders') }}
where cast(`order_number` as string) is not null
  and replace(cast(`order_number` as string), ' ', '') <> ''
