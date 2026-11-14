select
    u.user_id,
    u.country,
    u.state,
    u.city,
    u.age,
    u.gender,
    u.traffic_source,
    date(u.customer_created_at) as customer_created_date,
    count(distinct if(oi.status = 'complete', oi.order_id, null)) as completed_order_count,
    sum(if(oi.status = 'complete', oi.sale_price, 0)) as completed_revenue,
    max(if(oi.status = 'complete', oi.order_item_date, null)) as most_recent_completed_order_date
from {{ ref('stg_users') }} as u
left join {{ ref('stg_order_items') }} as oi using (user_id)
group by 1, 2, 3, 4, 5, 6, 7, 8
