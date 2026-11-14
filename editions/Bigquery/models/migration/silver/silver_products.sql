with ranked as (
    select
        cast(product_id as {{ dbt.type_string() }}) as product_id,
        nullif(trim(cast(product_name as {{ dbt.type_string() }}), ' '), '') as product_name,
        nullif(trim(cast(category as {{ dbt.type_string() }}), ' '), '') as category,
        row_number() over (
            partition by cast(product_id as {{ dbt.type_string() }})
            order by
                _ingested_at_utc desc nulls last,
                nullif(trim(cast(product_name as {{ dbt.type_string() }}), ' '), '') desc nulls last,
                nullif(trim(cast(category as {{ dbt.type_string() }}), ' '), '') desc nulls last
        ) as _row_number
    from {{ source('migration_bronze', 'products') }}
    where cast(product_id as {{ dbt.type_string() }}) is not null
      and replace(cast(product_id as {{ dbt.type_string() }}), ' ', '') <> ''
)
select product_id, product_name, category
from ranked
where _row_number = 1
