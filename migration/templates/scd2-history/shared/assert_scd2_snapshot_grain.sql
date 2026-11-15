-- Singular dbt test (portable). For snapshot-observed history, every interval boundary must
-- be a real snapshot date. Returns rows when a boundary was invented between snapshots,
-- for example by expanding monthly snapshots into daily rows.
with calendar as (
    select distinct snapshot_date from {{ ref('stg_mock_customer_snapshots') }}
)

select h.customer_key, h.valid_from, h.valid_to
from {{ ref('scd2_customer_history') }} h
left join calendar f on f.snapshot_date = h.valid_from
left join calendar t on t.snapshot_date = h.valid_to
where h.effective_basis = 'snapshot_observed'
    and (
        f.snapshot_date is null
        or (h.valid_to is not null and t.snapshot_date is null)
    )
