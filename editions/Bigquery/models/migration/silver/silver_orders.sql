with ranked as (
    select
        cast(order_id as {{ dbt.type_string() }}) as order_id,
        case
            when replace(cast(customer_id as {{ dbt.type_string() }}), ' ', '') = '' then cast(null as {{ dbt.type_string() }})
            else cast(customer_id as {{ dbt.type_string() }})
        end as customer_id,
        lower(trim(cast(order_status as {{ dbt.type_string() }}), ' ')) as order_status,
        cast(order_date as date) as order_date,
        cast(updated_at as {{ dbt.type_timestamp() }}) as source_updated_at,
        row_number() over (
            partition by cast(order_id as {{ dbt.type_string() }})
            order by
                cast(updated_at as {{ dbt.type_timestamp() }}) desc nulls last,
                _ingested_at_utc desc nulls last,
                case
                    when replace(cast(customer_id as {{ dbt.type_string() }}), ' ', '') = '' then cast(null as {{ dbt.type_string() }})
                    else cast(customer_id as {{ dbt.type_string() }})
                end desc nulls last,
                lower(trim(cast(order_status as {{ dbt.type_string() }}), ' ')) desc nulls last,
                cast(order_date as date) desc nulls last
        ) as _row_number
    from {{ source('migration_bronze', 'orders') }}
    where cast(order_id as {{ dbt.type_string() }}) is not null
      and replace(cast(order_id as {{ dbt.type_string() }}), ' ', '') <> ''
)
select order_id, customer_id, order_status, order_date, source_updated_at
from ranked
where _row_number = 1
