select
    cast(id as int64) as product_id,
    name as product_name,
    category,
    department,
    brand,
    cast(cost as numeric) as unit_cost,
    cast(retail_price as numeric) as retail_price
from {{ source('thelook', 'products') }}
