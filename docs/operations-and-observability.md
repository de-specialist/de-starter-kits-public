# Operations and Observability

The kit uses dbt's built-in artifacts and platform-native CI notifications. It does not send project data to a third-party monitoring service.

## Inspect a run

Run `dbt build` from the selected edition's project directory. dbt writes `target/manifest.json`, `target/run_results.json`, and logs under `logs/`; these local paths are Git-ignored. Inspect terminal output and `run_results.json` for failed models/tests, then review `logs/dbt.log` for command context. Review artifacts before sharing: compiled SQL can include environment-specific metadata or literal values.

GitHub Actions shows CI status for parsing/builds and can notify repository watchers. The BigQuery warehouse build is manually dispatched on `main`; Snowflake's credentialed build uses its isolated CI target. The default BigQuery pull-request job only parses the project and does not prove that warehouse SQL runs successfully.

## Freshness and alert limits

No source-freshness threshold is configured yet. The BigQuery public source exposes business event timestamps, which are not a reliable ingestion/update timestamp; Snowflake uses local synthetic seeds. Do not present event recency as pipeline freshness. Add a freshness check only after a source supplies a trustworthy load/update timestamp and a threshold has been agreed.

For now, use failed CI status as the actionable alert. There is no anomaly detection, paging, or automatic warehouse cleanup. Keep credentialed runs opt-in and inspect warehouse cost in the provider console using the [BigQuery](../editions/Bigquery/docs/cost-check.md) or [Snowflake](../editions/Snowflake/docs/cost-checks.md) guide. Set provider-native budgets or warehouse monitors as appropriate; see [cost-control guardrails](cost-control-guardrails.md) for limits and caveats.

## Artifact retention

Keep local dbt artifacts only as long as needed to diagnose a run. Do not commit `target/`, `logs/`, compiled SQL containing sensitive literals, or completed customer-pilot records. A future CI artifact upload should have short retention and an explicit review for sensitive metadata.
