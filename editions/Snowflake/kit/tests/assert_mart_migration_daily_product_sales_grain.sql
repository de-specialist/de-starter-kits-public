-- Migration Gold has one row per order date and product.
select order_date, product_id
from {{ ref('mart_migration_daily_product_sales') }}
group by 1, 2
having count(*) > 1
