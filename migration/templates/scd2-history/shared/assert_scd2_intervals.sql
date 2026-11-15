-- Singular dbt test (portable). Returns rows when a key's intervals are not a clean
-- half-open chain: each valid_to equals the next valid_from, and only the last row is open.
with history as (
    select * from {{ ref('scd2_customer_history') }}
),

ordered as (
    select
        customer_key,
        valid_from,
        valid_to,
        lead(valid_from) over (partition by customer_key order by valid_from) as next_valid_from
    from history
)

select customer_key, valid_from, 'empty_or_inverted_interval' as failure
from history
where valid_to is not null and valid_to <= valid_from

union all

select customer_key, valid_from, 'gap_or_overlap' as failure
from ordered
where next_valid_from is not null
    and (valid_to is null or valid_to <> next_valid_from)

union all

select customer_key, valid_from, 'current_flag_mismatch' as failure
from history
where is_current <> (valid_to is null)

union all

select customer_key, max(valid_from) as valid_from, 'not_exactly_one_current' as failure
from history
group by customer_key
having sum(case when is_current then 1 else 0 end) <> 1
