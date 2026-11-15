-- Portable (BigQuery and Snowflake): turn periodic full snapshots into the change-record
-- contract used by scd2_history.sql. MOCK names; replace them with yours.
--
-- Grain warning: effective_at is the snapshot date on which a value was first OBSERVED.
-- The real change happened at some unknown time after the previous snapshot. Do not relabel
-- these rows as source_effective and do not interpolate dates between snapshots.
--
-- A key missing from a later snapshot becomes a tombstone. Only load complete snapshots:
-- a partial snapshot would create false tombstones. A snapshot date with no rows at all is
-- not in the calendar, so it creates none.

with snapshots as (
    select * from {{ ref('stg_mock_customer_snapshots') }}
),

calendar as (
    select distinct snapshot_date from snapshots
),

first_seen as (
    select customer_key, min(snapshot_date) as first_snapshot_date
    from snapshots
    group by customer_key
),

expected as (
    select f.customer_key, c.snapshot_date
    from first_seen f
    inner join calendar c on c.snapshot_date >= f.first_snapshot_date
)

select
    e.customer_key,
    s.tier,
    s.region,
    e.snapshot_date as effective_at,
    -- Use the snapshot's load timestamp here if you keep one.
    e.snapshot_date as recorded_at,
    s.customer_key is null as is_deleted,
    'snapshot_observed' as effective_basis,
    -- Set to the real snapshot cadence: 'month', 'week' or 'day'.
    'month' as observation_grain
from expected e
left join snapshots s
    on s.customer_key = e.customer_key
    and s.snapshot_date = e.snapshot_date
