# Cross-edition mart contract

The BigQuery and Snowflake projects share a dbt layering pattern, but they use different source grains and are independent examples. Compare equivalent business concepts only where their definitions below match.

## Shared modeling conventions

Both editions follow `source or seed → staging → marts`. Staging models standardize names, types, and status values; marts aggregate data for reporting. Both define `completed_revenue` as gross value from completed activity, not profit, margin, or net revenue. Their customer-level models retain customers with no completed activity and return zero completed revenue and order count.

## Mart inventory

| Edition / model | Grain | Inputs | Metrics and dimensions |
|---|---|---|---|
| BigQuery `mart_daily_product_sales` | One row per order-item date and product | Public TheLook `order_items` joined to `products` through staging | Completed item and order counts, completed item sale value, returned item count and returned value; product name, category, department |
| BigQuery `mart_daily_product_sales_incremental` | One row per order-item date and product | Same public TheLook inputs as the daily sales mart | Same metrics as the daily sales mart; optional rebuild of the last 90 days of date partitions, disabled by default |
| BigQuery `mart_customer_value` | One row per user | Public TheLook `users` left-joined to staged order items | Completed order count, completed item sale value, most recent completed item date; country, state, city, age, gender, traffic source, customer-created date |
| Snowflake `fct_customer_orders` | One row per synthetic customer | `raw_customers` and `raw_orders` seeds through staging | Completed order count, completed order amount, most recent completed order date; synthetic customer name and country |
| Snowflake `fct_orders_incremental` | One row per order | Synthetic `raw_orders` seed through `stg_orders` | Order status, amount, order date, source update timestamp; demonstrates merge-based incremental loading, not a customer or daily sales mart |

## Metric semantics and differences

- BigQuery completed revenue sums item `sale_price` only where status is `complete`; its daily mart groups by item `created_at` date. Its `returned_value` sums the sale price of returned items by item creation date. It is diagnostic, not a refund ledger.
- Snowflake completed revenue sums order `amount` only where status is `completed`. The synthetic seed is one row per order, so counting completed order rows is equivalent to counting distinct completed orders in this example.
- The datasets do not support like-for-like product or return reporting: the Snowflake fixture has order headers and no product or item rows, while BigQuery's source is item-grain. Snowflake's `cancelled` status is not the same as BigQuery's `returned` status.
- BigQuery curated models omit direct identifiers such as name, email, and street address. Snowflake's `customer_name` is synthetic fixture data; do not carry it into real customer marts without an explicit privacy and access policy.
- BigQuery's daily product mart is partitioned by date and clustered by product. Its incremental variant rebuilds every date partition in a configurable 90-day lookback with static `insert_overwrite`, so deleted or moved items inside the window leave no stale groups; older dates need a full refresh. Snowflake's optional incremental order model uses an order merge key and a three-day source-update lookback; both are separate technical examples, not equivalent marts.

When adding models, preserve these grains and definitions or document the new metric as edition-specific. Warehouse-specific SQL and configuration stay in the edition folder.
