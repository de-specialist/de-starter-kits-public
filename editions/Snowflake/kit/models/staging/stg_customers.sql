select
    customer_id::number as customer_id,
    trim(customer_name)::varchar as customer_name,
    upper(trim(country))::varchar(2) as country
from {{ ref('raw_customers') }}
