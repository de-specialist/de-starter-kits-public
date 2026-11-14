{{ config(
    partition_by={"field": "order_item_date", "data_type": "date"},
    cluster_by=["product_id"]
) }}

select
    oi.order_item_date,
    oi.product_id,
    p.product_name,
    p.category,
    p.department,
    count(distinct if(oi.status = 'complete', oi.order_id, null)) as completed_order_count,
    countif(oi.status = 'complete') as completed_item_count,
    sum(if(oi.status = 'complete', oi.sale_price, 0)) as completed_revenue,
    countif(oi.status = 'returned') as returned_item_count,
    sum(if(oi.status = 'returned', oi.sale_price, 0)) as returned_value
from {{ ref('stg_order_items') }} as oi
left join {{ ref('stg_products') }} as p using (product_id)
group by 1, 2, 3, 4, 5
