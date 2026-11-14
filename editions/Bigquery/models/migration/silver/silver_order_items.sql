with ranked as (
    select
        cast(order_item_id as {{ dbt.type_string() }}) as order_item_id,
        case
            when replace(cast(order_id as {{ dbt.type_string() }}), ' ', '') = '' then cast(null as {{ dbt.type_string() }})
            else cast(order_id as {{ dbt.type_string() }})
        end as order_id,
        case
            when replace(cast(product_id as {{ dbt.type_string() }}), ' ', '') = '' then cast(null as {{ dbt.type_string() }})
            else cast(product_id as {{ dbt.type_string() }})
        end as product_id,
        cast(quantity as integer) as quantity,
        cast(unit_price as {{ dbt.type_numeric() }}) as unit_price,
        row_number() over (
            partition by cast(order_item_id as {{ dbt.type_string() }})
            order by
                _ingested_at_utc desc nulls last,
                case
                    when replace(cast(order_id as {{ dbt.type_string() }}), ' ', '') = '' then cast(null as {{ dbt.type_string() }})
                    else cast(order_id as {{ dbt.type_string() }})
                end desc nulls last,
                case
                    when replace(cast(product_id as {{ dbt.type_string() }}), ' ', '') = '' then cast(null as {{ dbt.type_string() }})
                    else cast(product_id as {{ dbt.type_string() }})
                end desc nulls last,
                cast(quantity as integer) desc nulls last,
                cast(unit_price as {{ dbt.type_numeric() }}) desc nulls last
        ) as _row_number
    from {{ source('migration_bronze', 'order_items') }}
    where cast(order_item_id as {{ dbt.type_string() }}) is not null
      and replace(cast(order_item_id as {{ dbt.type_string() }}), ' ', '') <> ''
)
select order_item_id, order_id, product_id, quantity, unit_price
from ranked
where _row_number = 1
