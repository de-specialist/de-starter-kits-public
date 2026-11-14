{{ config(enabled=var('enable_incremental_example', false)) }}

-- A row represents one product on one order-item creation date.
select
    order_item_date,
    product_id
from {{ ref('mart_daily_product_sales_incremental') }}
group by 1, 2
having count(*) > 1
