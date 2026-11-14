select
    c.customer_id,
    c.customer_name,
    c.country,
    coalesce(count_if(o.status = 'completed'), 0) as completed_order_count,
    coalesce(sum(iff(o.status = 'completed', o.amount, 0)), 0)::number(12, 2) as completed_revenue,
    max(iff(o.status = 'completed', o.order_date, null)) as most_recent_completed_order
from {{ ref('stg_customers') }} as c
left join {{ ref('stg_orders') }} as o
    on c.customer_id = o.customer_id
group by c.customer_id, c.customer_name, c.country
