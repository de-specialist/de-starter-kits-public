# BigQuery cost checks

Use Cloud Billing Reports to inspect dollars billed and BigQuery job metadata to inspect query bytes. These views answer different questions: bytes billed per query are not a complete account of project costs.

## Check billed costs in Cloud Billing Reports

In Google Cloud Console, open **Billing → Reports**. Select the linked billing account or a project-scoped view, set the date range, filter the relevant project, and group by **Service** to review BigQuery costs. Use the project that actually pays for the jobs; it can differ from the project that stores the dataset or from `BQ_PROJECT` when those are configured separately. Cost reports require the billing/project permissions described in Google's [Billing Reports guide](https://docs.cloud.google.com/billing/docs/how-to/reports).

Review the selected period in dollars, including discounts, credits, and other adjustments shown in the report. This is the appropriate place to understand total billed spend. BigQuery storage and other billed services are included in project costs but are not represented by query-job bytes alone.

## Inspect query bytes in `INFORMATION_SCHEMA.JOBS_BY_PROJECT`

This query summarizes completed query jobs for the last 30 days, in UTC. Replace `YOUR_PROJECT_ID` with the project where the jobs ran and replace `REGION` with the matching BigQuery location suffix. For this edition's default `BQ_LOCATION=US`, use `us` so the full qualifier is `region-us`. The query execution location must match the region-qualified metadata view.

```sql
SELECT
  DATE(creation_time) AS job_date_utc,
  COUNT(*) AS query_jobs,
  SUM(total_bytes_processed) AS total_bytes_processed,
  SUM(total_bytes_billed) AS total_bytes_billed
FROM `YOUR_PROJECT_ID.region-REGION.INFORMATION_SCHEMA.JOBS_BY_PROJECT`
WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  AND job_type = 'QUERY'
  AND statement_type != 'SCRIPT'
  AND state = 'DONE'
GROUP BY job_date_utc
ORDER BY job_date_utc DESC;
```

`total_bytes_processed` describes data processed; `total_bytes_billed` records bytes billed for on-demand pricing. With capacity pricing, the bytes field is informational rather than a dollar charge. This project-level view includes all query jobs in that project, not only dbt jobs. Filter the `creation_time` partition, as the example does, to reduce metadata scanning. See Google's [`JOBS` view reference](https://docs.cloud.google.com/bigquery/docs/information-schema-jobs) for columns, permissions, scope, and location requirements.

To query the project-level `JOBS_BY_PROJECT` view, your identity needs `bigquery.jobs.listAll` and `bigquery.jobs.create` on the project; Google's guide lists BigQuery User and BigQuery Resource Viewer as predefined roles that include these permissions. Ask an administrator for the narrowest access appropriate to your organization.

## Attribute dbt jobs with labels

This edition configures dbt query comments as BigQuery job labels. The labels identify the application, edition, dbt target, and invocation, so you can filter the edition's jobs separately from unrelated project activity. They are metadata for attribution; they do not change billing or enforce a spend limit. See the dbt [BigQuery job-label configuration](https://docs.getdbt.com/reference/resource-configs/bigquery-configs#applying-labels-to-jobs).

For example, filter the region-qualified jobs view to this edition's query jobs:

```sql
SELECT
  creation_time,
  job_id,
  total_bytes_billed,
  labels
FROM `YOUR_PROJECT_ID.region-us.INFORMATION_SCHEMA.JOBS_BY_PROJECT`
WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  AND job_type = 'QUERY'
  AND EXISTS (
    SELECT 1
    FROM UNNEST(labels) AS label
    WHERE label.key = 'edition'
      AND label.value = 'bigquery'
  )
ORDER BY creation_time DESC;
```

Use the location suffix for the job region (for example, `region-us` for the default `US` location). Job labels can be absent on jobs from other tools or older runs, so retain the unfiltered project-level view for reconciliation.

## Interpret the numbers

- A `maximum_bytes_billed` profile setting is a per-query guardrail for dbt jobs. It is not a total project budget and it does not cap Looker Studio connector jobs or other tools.
- Query-job bytes do not measure table storage. Persistent dbt tables in `BQ_DATASET`, including incremental-model partitions, can incur storage charges independently of query processing.
- Byte totals are not a direct dollar estimate across pricing models, free allowances, cached results, credits, or capacity reservations. Use Billing Reports for billed dollars.
- If Looker Studio uses a separate billing project, inspect that project's costs in Billing Reports. The jobs query only shows jobs in the project named by `YOUR_PROJECT_ID`.
- Avoid exposing job metadata such as user email or SQL text in shared reports. Query results can reveal project activity.

## Billing export is optional

This guide uses Cloud Billing Reports and `INFORMATION_SCHEMA` only. It does not enable or configure Cloud Billing export. Exporting billing data to BigQuery is a separate administrator action that requires billing-account permissions, a BigQuery dataset, and additional setup; querying or storing exported data can itself incur BigQuery charges. See Google's [Cloud Billing export setup guide](https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-setup) before choosing to enable it.
