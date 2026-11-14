select
    cast(id as int64) as user_id,
    country,
    state,
    city,
    cast(age as int64) as age,
    gender,
    traffic_source,
    cast(created_at as timestamp) as customer_created_at
from {{ source('thelook', 'users') }}
