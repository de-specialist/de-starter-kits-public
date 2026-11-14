{{ config(enabled=var('enable_incremental_example', false)) }}

-- Post-build invariants of the fct_orders_incremental timestamp edge contract.
-- Run after a full refresh or an incremental run, with no source change in between.
-- Returns one row per violation.
--   missing/future rows: always reprocessed, so they must equal staging exactly.
--   window rows: every staging row at or after the inclusive window start (derived from the
--     target's valid watermark) must equal staging.
--   status: watermark_status must equal the status recomputed from staging for those rows, and
--     must agree with source_updated_at nullness for every row.
-- Known false positive: if the latest valid row's source_updated_at is later set to null, the
-- watermark moves earlier, and unchanged-timestamp edits between the new and old window starts
-- are reported even though the run behaved as documented. A full refresh clears it.
-- Rows older than the window start ("late" corrections) are intentionally not checked; they can
-- differ from staging until a full refresh.

with bounds as (
    select dateadd(
        day,
        {{ var('incremental_future_skew_days', 1) }},
        current_date()
    )::timestamp_ntz as future_cutoff
),

target as (
    select * from {{ ref('fct_orders_incremental') }}
),

window_start as (
    select dateadd(
        day,
        -{{ var('incremental_lookback_days', 3) }},
        coalesce(max(t.source_updated_at), '1900-01-01'::timestamp_ntz)
    ) as starts_at
    from target as t
    where t.watermark_status = 'valid'
        and t.source_updated_at < (select future_cutoff from bounds)
),

expected as (
    select
        s.*,
        case
            when s.source_updated_at is null then 'missing'
            when s.source_updated_at >= b.future_cutoff then 'future'
            else 'valid'
        end as expected_status
    from {{ ref('stg_orders') }} as s
    cross join bounds as b
    cross join window_start as w
    where s.source_updated_at is null
        or s.source_updated_at >= b.future_cutoff
        or s.source_updated_at >= w.starts_at
),

violations as (
    select e.order_id, e.expected_status || '_row_not_current' as issue
    from expected as e
    left join target as t on e.order_id = t.order_id
    where t.order_id is null
        or not equal_null(t.customer_id, e.customer_id)
        or not equal_null(t.order_date, e.order_date)
        or not equal_null(t.status, e.status)
        or not equal_null(t.amount, e.amount)
        or not equal_null(t.source_updated_at, e.source_updated_at)
        or not equal_null(t.watermark_status, e.expected_status)

    union all

    select t.order_id, 'watermark_status_mismatch' as issue
    from target as t
    where (t.source_updated_at is null) <> (t.watermark_status = 'missing')
)

select order_id, issue
from violations
