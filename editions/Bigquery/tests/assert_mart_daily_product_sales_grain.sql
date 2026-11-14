-- A row represents one product on one order-item creation date.
select
    order_item_date,
    product_id
from {{ ref('mart_daily_product_sales') }}
group by 1, 2
having count(*) > 1
