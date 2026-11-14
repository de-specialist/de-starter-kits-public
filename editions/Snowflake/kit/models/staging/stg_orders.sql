select
    order_id::number as order_id,
    customer_id::number as customer_id,
    order_date::date as order_date,
    lower(trim(status))::varchar as status,
    amount::number(12, 2) as amount,
    source_updated_at::timestamp_ntz as source_updated_at
from {{ ref('raw_orders') }}
