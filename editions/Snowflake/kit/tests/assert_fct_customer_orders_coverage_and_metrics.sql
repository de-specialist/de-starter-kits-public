with customers as (
    select customer_id
    from {{ ref('stg_customers') }}
),
facts as (
    select customer_id, completed_order_count, completed_revenue
    from {{ ref('fct_customer_orders') }}
),
violations as (
    select c.customer_id, 'missing_customer' as issue
    from customers as c
    left join facts as f on c.customer_id = f.customer_id
    where f.customer_id is null

    union all

    select f.customer_id, 'unexpected_customer' as issue
    from facts as f
    left join customers as c on f.customer_id = c.customer_id
    where c.customer_id is null

    union all

    select customer_id, 'invalid_completed_metrics' as issue
    from facts
    where completed_order_count is null
        or completed_order_count < 0
        or completed_revenue is null
        or completed_revenue < 0
)
select customer_id, issue
from violations
