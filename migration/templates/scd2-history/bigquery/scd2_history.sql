{{ config(materialized='table') }}

-- SCD2 history template (BigQuery edition). MOCK column names; replace them with yours:
--   natural key: customer_key        tracked attributes: tier, region
-- Input contract and guidance: ../README.md. Intervals are half-open: [valid_from, valid_to).
-- A full rebuild restates history, so backdated and corrected records reshape earlier intervals.
-- customer_key is a string here; cast other key types inside concat().

with changes as (
    select * from {{ ref('stg_mock_customer_changes') }}
),

-- Late correction: one row per (natural key, effective_at); the latest recorded_at wins.
-- Staging must reject ambiguous ties. If they slip through, prefer deletion, then
-- descending retained values (nulls last). Exact output duplicates are interchangeable.
deduped as (
    select *
    from (
        select
            c.*,
            row_number() over (
                partition by c.customer_key, c.effective_at
                order by c.recorded_at desc,
                    coalesce(c.is_deleted, false) desc,
                    c.tier desc nulls last,
                    c.region desc nulls last,
                    c.effective_basis desc nulls last,
                    c.observation_grain desc nulls last
            ) as correction_rank
        from changes c
    )
    where correction_rank = 1
),

flagged as (
    select
        customer_key,
        tier,
        region,
        coalesce(is_deleted, false) as is_deleted,
        effective_at,
        recorded_at,
        effective_basis,
        observation_grain,
        row_number() over (partition by customer_key order by effective_at) as version_seq,
        lag(tier) over (partition by customer_key order by effective_at) as prev_tier,
        lag(region) over (partition by customer_key order by effective_at) as prev_region,
        lag(coalesce(is_deleted, false)) over (
            partition by customer_key order by effective_at
        ) as prev_is_deleted
    from deduped
),

-- Keep a row only when it starts a new version. Repeated tombstones are one version.
versions as (
    select *
    from flagged
    where version_seq = 1
        or is_deleted is distinct from prev_is_deleted
        or (
            not is_deleted
            and (
                tier is distinct from prev_tier
                or region is distinct from prev_region
            )
        )
)

select
    to_hex(md5(concat(customer_key, '|', cast(effective_at as string)))) as scd2_key,
    customer_key,
    tier,
    region,
    is_deleted,
    effective_at as valid_from,
    lead(effective_at) over (partition by customer_key order by effective_at) as valid_to,
    lead(effective_at) over (partition by customer_key order by effective_at) is null as is_current,
    effective_basis,
    observation_grain,
    recorded_at as source_recorded_at
from versions
