# Looker Studio walkthrough

Build a small report from the BigQuery marts after running `dbt build`. This guide describes the setup and chart configuration; it does not create or publish a report.

## Before you start

Confirm that the target dataset contains these tables:

- `BQ_PROJECT.BQ_DATASET.mart_daily_product_sales`
- `BQ_PROJECT.BQ_DATASET.mart_customer_value`

Use the project and dataset from your BigQuery profile. The sales mart has one row per `order_item_date` and `product_id`; the customer mart has one row per `user_id`. The revenue fields are gross item sale value, not profit. `returned_value` is diagnostic sale price for returned items by item creation date, not a refund ledger.

Looker Studio's BigQuery connector requires a billing project associated with a billing account, so this walkthrough may not work with BigQuery Sandbox alone. The connector can bill a different project from the one holding the data. Choose the project that should pay for report queries, and grant the data source identity BigQuery Data Viewer on the target dataset and BigQuery Job User on the billing project. With **Owner's credentials**, the data source owner runs queries for report viewers; with **Viewer's credentials**, each viewer needs BigQuery access. Restrict report sharing appropriately, especially because `mart_customer_value` includes user level location, age, gender, and traffic source fields. See Google's [BigQuery connector guide](https://docs.cloud.google.com/data-studio/connect-to-google-bigquery).

## Connect the marts

1. In Looker Studio, create a report and add a BigQuery data source.
2. Select the billing project, then the dataset configured by `BQ_PROJECT` and `BQ_DATASET`.
3. Select `mart_daily_product_sales`; repeat to add `mart_customer_value` as a second data source.
4. Choose the credential mode and sharing access deliberately. Do not publish the report publicly for this sample.
5. Confirm that `order_item_date` and `customer_created_date` are treated as dates, and that revenue and value fields are numeric. Set currency formatting only if the sample's currency is known for your use case; the model does not define a currency code.

Looker Studio connects each data source to one table. Keep each chart on the mart that provides its dimensions and measures; do not blend the two marts for these examples.

## Build four charts

| Chart | Data source | Dimensions | Metrics | Notes |
|---|---|---|---|---|
| Scorecards | `mart_daily_product_sales` | None | `SUM(completed_revenue)`, `SUM(completed_item_count)`, `SUM(returned_value)` | Set a date range. Label returned value as diagnostic. |
| Daily revenue time series | `mart_daily_product_sales` | `order_item_date` | `SUM(completed_revenue)`; optionally `SUM(returned_value)` | Use `order_item_date` as the chart's date range dimension. Separate returned value if its scale obscures the revenue trend. |
| Category performance bar chart | `mart_daily_product_sales` | `category` or `department` | `SUM(completed_revenue)`, `SUM(returned_value)` | Sort by completed revenue and limit to the top categories for readability. |
| Customer summary table | `mart_customer_value` | `traffic_source`, `country` | `COUNT_DISTINCT(user_id)`, `SUM(completed_order_count)`, `SUM(completed_revenue)` | These are lifetime customer measures. Optionally filter by `customer_created_date` to analyze acquisition cohorts; that does not turn lifetime revenue into revenue earned during the selected period. |

Add a date range control for the sales charts and set its date range dimension to `order_item_date`. Add category and department filters for product charts, and country and traffic source filters for the customer table. Scope controls to the relevant charts: the customer mart's `customer_created_date` means cohort creation date, not order date. Do not sum `completed_order_count` from `mart_daily_product_sales` across products; one order can contain items from multiple products, so that metric is not additive across the mart grain. Google's [chart and control guide](https://docs.cloud.google.com/data-studio/add-charts-and-controls-to-your-report) describes control behavior and scoping.

## Sanity-check the tables

Replace the project and dataset IDs. Choose a date interval that contains data in `mart_daily_product_sales`; the explicit date predicate allows partition pruning. Before running, check the BigQuery console's estimated bytes processed.

```sql
DECLARE start_date DATE DEFAULT DATE '2024-01-01'; -- change to the interval you want to inspect
DECLARE end_date DATE DEFAULT DATE '2024-01-31';   -- change to the interval you want to inspect

SELECT
  COUNT(*) AS rows_in_period,
  COUNTIF(order_item_date IS NULL OR product_id IS NULL) AS null_grain_rows,
  SUM(completed_revenue) AS completed_revenue,
  SUM(completed_item_count) AS completed_items,
  SUM(returned_value) AS diagnostic_returned_value
FROM `your-gcp-project.dbt_retail.mart_daily_product_sales`
WHERE order_item_date BETWEEN start_date AND end_date;
```

This second query should return no rows; if it returns any, the date/product grain is duplicated in the chosen period:

```sql
DECLARE start_date DATE DEFAULT DATE '2024-01-01'; -- use the same interval as above
DECLARE end_date DATE DEFAULT DATE '2024-01-31';

SELECT order_item_date, product_id, COUNT(*) AS duplicate_rows
FROM `your-gcp-project.dbt_retail.mart_daily_product_sales`
WHERE order_item_date BETWEEN start_date AND end_date
GROUP BY order_item_date, product_id
HAVING COUNT(*) > 1;
```

On the customer mart, confirm one row per customer and inspect aggregate totals:

```sql
SELECT
  COUNT(*) AS customer_rows,
  COUNT(DISTINCT user_id) AS distinct_customers,
  COUNTIF(user_id IS NULL) AS null_customer_ids,
  SUM(completed_order_count) AS completed_customer_orders,
  SUM(completed_revenue) AS completed_revenue
FROM `your-gcp-project.dbt_retail.mart_customer_value`;
```

The customer query reads the full unpartitioned mart. Run it only when needed and review estimated bytes before execution. These queries check the built aggregates; dbt's unit test separately checks the complete/returned rules using inline fixtures. If the selected date interval returns no sales rows, choose dates present in the mart.

## Cost and refresh guardrails

- The profile's `maximum_bytes_billed` setting applies to dbt BigQuery jobs; it is not a total budget and does not automatically cap Looker Studio connector queries.
- Use `order_item_date` date controls for sales charts so BigQuery can prune partitions. Choose a period that contains data, and avoid unnecessarily wide ranges.
- Prefer the marts over the raw TheLook tables. Do not use custom SQL against raw tables for dashboard charts.
- Avoid frequent auto-refresh and repeated reloads while developing. Use the longest freshness interval that meets the report's needs, and monitor query usage in the billing project.
- A billing budget and alerts help monitor spend but do not prevent all charges. Connector usage can incur query-processing and storage costs; see the current [BigQuery connector documentation](https://docs.cloud.google.com/data-studio/connect-to-google-bigquery).
- Looker Studio connector queries include the `requestor:looker_studio` job label; use it to separate report query volume and cost in BigQuery monitoring. See Google's [Data Studio BigQuery monitoring guide](https://docs.cloud.google.com/data-studio/bigquery-monitor).
