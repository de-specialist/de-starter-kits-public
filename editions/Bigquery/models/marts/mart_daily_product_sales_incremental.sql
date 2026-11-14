{#-
  Incremental runs rebuild every daily partition in a bounded lookback window
  (today and the previous `incremental_lookback_days` days, by current_date()
  in UTC) from source, using a static insert_overwrite. Each listed partition is
  replaced wholesale even when the rebuild yields no rows for it, so date/product
  groups whose last item was deleted or moved to another date or product inside
  the window are dropped, not left stale. Groups dated before the window are
  never touched; changes there still need --full-refresh.
-#}
{%- set lookback_days = var('incremental_lookback_days', 90) -%}
{%- if lookback_days is not number or lookback_days is sameas true or lookback_days is sameas false or lookback_days != lookback_days | int or lookback_days < 0 or lookback_days > 3650 -%}
  {{ exceptions.raise_compiler_error(
      "incremental_lookback_days must be a whole number of days from 0 to 3650 (under BigQuery's 4,000-partitions-per-job limit), got "
      ~ lookback_days ~ ". Use --full-refresh to rebuild older history."
  ) }}
{%- endif -%}
{#- Normalize whole-number floats such as 7.0 so range() and the SQL interval get an integer. -#}
{%- set lookback_days = lookback_days | int -%}
{%- set partitions_to_replace = [] -%}
{%- for days_ago in range(lookback_days + 1) -%}
  {%- do partitions_to_replace.append('date_sub(current_date(), interval ' ~ days_ago ~ ' day)') -%}
{%- endfor -%}

{{ config(
    materialized='incremental',
    enabled=var('enable_incremental_example', false),
    incremental_strategy='insert_overwrite',
    partitions=partitions_to_replace,
    partition_by={"field": "order_item_date", "data_type": "date"},
    cluster_by=["product_id"]
) }}

select
    oi.order_item_date,
    oi.product_id,
    p.product_name,
    p.category,
    p.department,
    count(distinct if(oi.status = 'complete', oi.order_id, null)) as completed_order_count,
    countif(oi.status = 'complete') as completed_item_count,
    sum(if(oi.status = 'complete', oi.sale_price, 0)) as completed_revenue,
    countif(oi.status = 'returned') as returned_item_count,
    sum(if(oi.status = 'returned', oi.sale_price, 0)) as returned_value
from {{ ref('stg_order_items') }} as oi
left join {{ ref('stg_products') }} as p using (product_id)
{% if is_incremental() %}
-- Must select exactly the partitions listed in `partitions`: rows outside them
-- would be appended without replacing their partition.
where oi.order_item_date between date_sub(current_date(), interval {{ lookback_days }} day)
    and current_date()
{% endif %}
group by 1, 2, 3, 4, 5
