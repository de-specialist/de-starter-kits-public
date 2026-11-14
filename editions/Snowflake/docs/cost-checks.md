# Check Snowflake project costs

Use this guide to review account-level billed credits and attribute hourly warehouse compute to a warehouse. Snowflake credits are a unit of consumption, **not a currency amount**. The currency value depends on your account's contract and pricing. Reconcile credits with your Snowflake invoice or usage statement; do not apply a generic dollars-per-credit estimate.

## Snowsight cost dashboard

1. Sign in to Snowsight with a role that can view cost and usage data.
2. Open **Admin » Cost management » Consumption**.
3. Choose a warehouse for the dashboard query (Snowflake recommends X-Small), then select **Compute** in Usage Type.
4. Filter by time range, service, warehouse/resource, or tags as needed.

The dashboard is a quick overview. For billed daily credits, use `METERING_DAILY_HISTORY` below. Snowflake notes that many cost views show consumed cloud services credits before the daily cloud services adjustment.

## Daily billed credits by service

Run with a role that can query the account's `SNOWFLAKE.ACCOUNT_USAGE` views. This returns billed credit totals per day and service type for the last 30 days:

```sql
SELECT
    usage_date,
    service_type,
    SUM(credits_used_compute) AS billed_compute_credits,
    SUM(credits_used_cloud_services) AS billed_cloud_services_credits,
    SUM(credits_adjustment_cloud_services) AS cloud_services_adjustment_credits,
    SUM(credits_billed) AS total_billed_credits
FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
WHERE usage_date >= DATEADD(day, -30, CURRENT_DATE())
GROUP BY usage_date, service_type
ORDER BY usage_date DESC, total_billed_credits DESC;
```

`CREDITS_BILLED` includes the cloud services adjustment. Keep the adjustment column visible when reviewing cloud services; adding it back to raw consumption would overstate billed usage.

## Hourly compute credits by warehouse

This query helps identify which warehouses consumed compute credits over the last seven days. It reports hourly compute consumption, including idle warehouse time, and is useful for attribution; it is not a currency calculation or an account-wide bill total.

```sql
SELECT
    start_time AS usage_hour,
    warehouse_name,
    credits_used_compute AS hourly_compute_credits
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE start_time >= DATEADD(day, -7, CURRENT_TIMESTAMP())
  AND warehouse_id > 0
ORDER BY usage_hour DESC, warehouse_name;
```

`WAREHOUSE_METERING_HISTORY.CREDITS_USED_COMPUTE` includes warehouse compute usage. `CREDITS_ATTRIBUTED_COMPUTE_QUERIES`, if used for query-level analysis, excludes idle time. The hourly warehouse view's `CREDITS_USED` can include cloud services credits before the daily adjustment, so use the daily view's `CREDITS_BILLED` when asking what was billed.

## Attribute dbt statements with query tags

The Snowflake example profile sets a default `query_tag` of `de_starter_snowflake`. Override it with the non-secret `SNOWFLAKE_QUERY_TAG` environment variable when you need a distinct tag for a workspace or run. Query tags appear in Snowflake query history and help distinguish starter-kit activity from other workloads; they are metadata and do not cap spend. dbt also supports a model-level `query_tag` when finer attribution is useful. See dbt's [Snowflake query-tag guidance](https://docs.getdbt.com/reference/resource-configs/snowflake-configs#query-tags).

## Access and data delay

Access is separate from the dbt development role. By default, `ACCOUNT_USAGE` views are restricted. Ask your Snowflake administrator to grant the least-privilege `SNOWFLAKE.USAGE_VIEWER` database role to an account role for usage views, or another approved access path. Snowsight cost dashboards may require account administrator access depending on the account's current configuration. Do not add broad account privileges to the kit's dbt role just to run these checks.

These views are delayed: daily metering can lag by up to 180 minutes; hourly warehouse metering can lag up to 180 minutes, and its cloud-services column can lag up to six hours. A recent run may not appear immediately. For finance reconciliation, allow the data to settle and compare with the invoice/usage statement. Set the session timezone to UTC when reconciling Account Usage data with Organization Usage.

## Official Snowflake references

- [Explore compute cost in Snowsight and SQL](https://docs.snowflake.com/en/user-guide/cost-exploring-compute)
- [`METERING_DAILY_HISTORY` columns and latency](https://docs.snowflake.com/en/sql-reference/account-usage/metering_daily_history)
- [`WAREHOUSE_METERING_HISTORY` columns and latency](https://docs.snowflake.com/en/sql-reference/account-usage/warehouse_metering_history)
- [Account Usage access and database roles](https://docs.snowflake.com/en/sql-reference/account-usage)
