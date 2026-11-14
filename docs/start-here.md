# Start here

Pick the path that matches your role. Each path is an ordered reading list of existing pages; read them in order and stop when you have what you need. Unfamiliar terms are defined in the [glossary](glossary.md).

## Data consultant

You want to know whether the kit is a sound base for client work, what it costs to try, and what you may do with it.

1. [Overview](../README.md): what the two editions contain and how to choose one.
2. [FAQ](faq.md): short answers on cost, data, credentials, licence, and what has been tested.
3. Decision 0001: licence and data terms: why the licence is source-available and what needs a separate written licence.
4. [Notice and attribution](../NOTICE): how the TheLook public dataset is credited and why no rows ship with the kit.
5. [Mart contract](mart-contract.md): which metrics match across editions and which are edition-specific.
6. [BigQuery architecture](../editions/Bigquery/docs/architecture.md) or [Snowflake architecture](../editions/Snowflake/docs/architecture.md): one diagram of sources, models, CI, and docs.
7. [Cost control guardrails](cost-control-guardrails.md): which warehouse controls to set before a run, and what each one does not cover.
8. [Compatibility](compatibility.md): the pinned Python, dbt, and adapter versions and what has actually run.
9. [Edition workspaces](edition-workspaces.md): how to copy one edition into a clean client workspace.
10. [Support](../SUPPORT.md): the best-effort support baseline.

## Analyst

You want to understand the marts, what each row means, and how to report on them.

1. [Overview](../README.md): choose the edition whose data fits your question.
2. [Glossary](glossary.md): grain, mart, staging, and the other terms used below.
3. [First-run walkthrough](first-run.md): the data flow and example queries for both editions.
4. [Mart contract](mart-contract.md): the grain, inputs, and metric definitions of every mart.
5. [BigQuery overview](../editions/Bigquery/README.md): business metric definitions for the TheLook marts.
6. [Looker Studio walkthrough](../editions/Bigquery/docs/looker-studio.md): connect the BigQuery marts and build four charts.
7. [BigQuery dbt docs](../editions/Bigquery/dbt-docs-catalog.md) or [Snowflake dbt docs](../editions/Snowflake/dbt-docs-catalog.md): browse model descriptions, columns, and the lineage graph.
8. [FAQ](faq.md): what is real data, what is synthetic, and whether the editions are equivalent.

## Analytics engineer

You want to run an edition, change it safely, and extend it.

1. [First-run walkthrough](first-run.md): prerequisites and the run sequence for each edition.
2. [BigQuery setup](../editions/Bigquery/docs/setup.md) or [Snowflake setup](../editions/Snowflake/docs/setup.md): profile, credentials, and the first `dbt build`.
3. [BigQuery architecture](../editions/Bigquery/docs/architecture.md) or [Snowflake architecture](../editions/Snowflake/docs/architecture.md): how sources, staging, marts, CI, and docs connect.
4. [Mart contract](mart-contract.md): the grains and definitions to preserve when you add models.
5. [BigQuery overview](../editions/Bigquery/README.md): the optional incremental mart and its lookback window.
6. [Snowflake operations](../editions/Snowflake/docs/operations.md) and [adapting the kit](../editions/Snowflake/docs/adapting-the-kit.md): warehouse controls, the CI boundary, and replacing the seeds.
7. [Operations and observability](operations-and-observability.md): inspecting runs, freshness, and artifact retention.
8. [Source change contract](source-change-contract.md): what to agree about a source before building an incremental model on it.
9. [Medallion migration](medallion-migration-extension.md) and [migration loader](../migration/README.md): the optional Bronze, Silver, and Gold path and its current limits.
10. [Cloud acceptance runbook](cloud-acceptance-runbook.md) and [Terraform lifecycle](terraform-lifecycle.md): provisioning a disposable target and running the preflight before any warehouse run.
11. [CI security review](ci-security-review.md): the trust boundaries of each workflow.
12. [Contributing](../CONTRIBUTING.md): repository conventions for SQL, YAML, tests, and commits.
