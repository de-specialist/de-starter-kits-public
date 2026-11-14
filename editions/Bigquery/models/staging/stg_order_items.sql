select
    cast(id as int64) as order_item_id,
    cast(order_id as int64) as order_id,
    cast(user_id as int64) as user_id,
    cast(product_id as int64) as product_id,
    lower(trim(status)) as status,
    cast(created_at as timestamp) as created_at,
    date(created_at) as order_item_date,
    cast(sale_price as numeric) as sale_price
from {{ source('thelook', 'order_items') }}
