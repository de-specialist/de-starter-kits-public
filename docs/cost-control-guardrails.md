# Cost Control Guardrails

Use warehouse-native controls before running a build, then check billed usage after it completes. A query-level limit, a notification budget, and a warehouse credit monitor cover different things; none should be treated as a universal account spend cap.

## BigQuery

- Keep `maximum_bytes_billed` configured in the edition profile. It rejects an individual on-demand query above the configured bytes threshold; it does not cap total project spend, storage, or jobs submitted by other tools.
- Create a Cloud Billing budget scoped to the project and, where useful, the BigQuery service. Configure actual and forecast threshold alerts and assign recipients who can act on them.
- Treat a standard alerts-only budget as a notification. Google's spend-cap budgets are currently limited to eligible services; verify BigQuery eligibility in the [spend-cap documentation](https://cloud.google.com/billing/docs/how-to/budgets-spend-caps) before relying on that feature.
- For exact charges, use [Billing Reports](https://cloud.google.com/billing/docs/how-to/reports). Use the [BigQuery cost checks](../editions/Bigquery/docs/cost-check.md) guide for job bytes and label-based attribution.

## Snowflake

- Use a small dedicated development warehouse with auto-suspend and only resume it for a run. Keep CI in its own database/schema and use a non-production role.
- Ask an administrator whether a warehouse-level [resource monitor](https://docs.snowflake.com/en/user-guide/resource-monitors) is appropriate. It can notify and suspend assigned warehouses at credit thresholds, but suspension may take time and it does not stop all cloud-services or serverless charges.
- Use the [Snowflake cost checks](../editions/Snowflake/docs/cost-checks.md) guide to review billed daily credits and hourly warehouse usage. Reconcile credits against the account's invoice or usage statement for currency amounts.

## Before a warehouse run

1. Confirm the active project/account, dataset/database/schema, role, and warehouse.
2. Confirm the BigQuery bytes threshold or Snowflake warehouse size and auto-suspend settings.
3. Check that the relevant budget alert recipients or resource monitor are configured.
4. Estimate what the run will scan or how long the warehouse will run; keep the run scoped to sample data.
5. Record the run window, edition, target, billed amount/credits, and follow-up action in a private cost review record.

## Official references

- [Google Cloud budgets and alerts](https://cloud.google.com/billing/docs/how-to/budgets)
- [Google Cloud spend-cap budgets and eligibility](https://cloud.google.com/billing/docs/how-to/budgets-spend-caps)
- [Snowflake resource monitors](https://docs.snowflake.com/en/user-guide/resource-monitors)
