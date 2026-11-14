with ranked as (
    select
        cast(customer_id as {{ dbt.type_string() }}) as customer_id,
        cast(created_at as {{ dbt.type_timestamp() }}) as customer_created_at,
        nullif(trim(cast(country as {{ dbt.type_string() }}), ' '), '') as country,
        row_number() over (
            partition by cast(customer_id as {{ dbt.type_string() }})
            order by
                _ingested_at_utc desc nulls last,
                cast(created_at as {{ dbt.type_timestamp() }}) desc nulls last,
                nullif(trim(cast(country as {{ dbt.type_string() }}), ' '), '') desc nulls last
        ) as _row_number
    from {{ source('migration_bronze', 'customers') }}
    where cast(customer_id as {{ dbt.type_string() }}) is not null
      and replace(cast(customer_id as {{ dbt.type_string() }}), ' ', '') <> ''
)
select customer_id, customer_created_at, country
from ranked
where _row_number = 1
