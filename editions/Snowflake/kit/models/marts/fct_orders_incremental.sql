{{
    config(
        materialized='incremental',
        enabled=var('enable_incremental_example', false),
        unique_key='order_id',
        incremental_strategy='merge'
    )
}}

-- Timestamp edge contract (see docs/operations.md, "Incremental order example"):
--   missing: a null source_updated_at is re-selected on every incremental run (the filter includes
--            `watermark_status = 'missing'`), so its merged row matches a full refresh; it never sets the watermark.
--   future:  a timestamp at or after the future cutoff (session current_date() plus
--            incremental_future_skew_days, default 1 = start of tomorrow) is re-selected on every run and
--            never sets the watermark, so a skewed or mistyped clock cannot freeze it.
--   valid:   only rows stored as valid set the watermark; the lookback window start is inclusive (>=).
--            A row stored as future whose date has since passed is still below the watermark's
--            window, so it is re-selected and rewritten as valid.
--   late:    a valid row at or after watermark - incremental_lookback_days (default 3) is recovered;
--            one earlier than that window start is skipped until a full refresh.

with bounds as (
    select dateadd(
        day,
        {{ var('incremental_future_skew_days', 1) }},
        current_date()
    )::timestamp_ntz as future_cutoff
),

orders as (
    select
        o.order_id,
        o.customer_id,
        o.order_date,
        o.status,
        o.amount,
        o.source_updated_at,
        case
            when o.source_updated_at is null then 'missing'
            when o.source_updated_at >= b.future_cutoff then 'future'
            else 'valid'
        end::varchar as watermark_status
    from {{ ref('stg_orders') }} as o
    cross join bounds as b
)

select
    order_id,
    customer_id,
    order_date,
    status,
    amount,
    source_updated_at,
    watermark_status
from orders
{% if is_incremental() %}
where watermark_status in ('missing', 'future')
    or source_updated_at >= (
        select dateadd(
            day,
            -{{ var('incremental_lookback_days', 3) }},
            coalesce(max(t.source_updated_at), '1900-01-01'::timestamp_ntz)
        )
        from {{ this }} as t
        where t.watermark_status = 'valid'
            and t.source_updated_at < (select future_cutoff from bounds)
    )
{% endif %}
