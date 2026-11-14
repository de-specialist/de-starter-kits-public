select
    orders.order_date,
    products.product_id,
    products.product_name,
    products.category,
    count(distinct case when orders.order_status in ('complete', 'completed') then orders.order_id end) as completed_order_count,
    sum(case when orders.order_status in ('complete', 'completed') then items.quantity else 0 end) as completed_item_count,
    sum(case when orders.order_status in ('complete', 'completed') then items.quantity * items.unit_price else 0 end) as completed_revenue,
    sum(case when orders.order_status = 'returned' then items.quantity * items.unit_price else 0 end) as returned_value
from {{ ref('silver_orders') }} as orders
inner join {{ ref('silver_order_items') }} as items using (order_id)
inner join {{ ref('silver_products') }} as products using (product_id)
group by orders.order_date, products.product_id, products.product_name, products.category
