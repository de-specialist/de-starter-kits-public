with completed_orders as (
    select
        customer_id,
        count(distinct order_id) as completed_order_count,
        max(order_date) as last_completed_order_date
    from {{ ref('silver_orders') }}
    where order_status in ('complete', 'completed')
    group by customer_id
), completed_revenue as (
    select
        orders.customer_id,
        sum(items.quantity * items.unit_price) as completed_revenue
    from {{ ref('silver_orders') }} as orders
    inner join {{ ref('silver_order_items') }} as items using (order_id)
    where orders.order_status in ('complete', 'completed')
    group by orders.customer_id
)
select
    customers.customer_id,
    customers.country,
    coalesce(completed_orders.completed_order_count, 0) as completed_order_count,
    coalesce(completed_revenue.completed_revenue, 0) as completed_revenue,
    completed_orders.last_completed_order_date
from {{ ref('silver_customers') }} as customers
left join completed_orders using (customer_id)
left join completed_revenue using (customer_id)
