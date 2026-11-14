# Source Change Contract

Complete this contract before adding or adapting a source connector. It describes how records arrive and change so an incremental model can be correct, not only fast. Keep client-specific values and credentials out of committed copies.

## Required source details

| Contract field | Decision / evidence |
|---|---|
| Source system and owner | |
| Table/entity and row grain | |
| Stable unique key | |
| Event/business timestamp | |
| Source-created timestamp | |
| Source-updated/ingestion timestamp and timezone | |
| Expected update delay (p50 / worst case) | |
| Late-arrival/backfill window | |
| Delete behavior (hard delete, tombstone, never) | |
| Duplicate and out-of-order behavior | |
| Pagination, API quota, and retry policy | |
| Full-refresh recovery path | |
| Data retention and privacy limits | |
| Safe synthetic fixture and source terms | |

## Incremental design decision

Document which timestamp drives the watermark, how ties are handled, how old updates are recovered, and whether deletions require a snapshot or tombstone. State the lookback window, expected scan cost, and when an operator must full-refresh. A business event date is not automatically a trustworthy update timestamp. If the source has no reliable update signal, prefer a periodic full refresh or snapshot comparison over a false freshness guarantee.

Also decide how rows with a null, future-dated, or late update timestamp are handled. The Snowflake `fct_orders_incremental` example re-selects null-timestamp rows on every run so incremental output matches a full refresh, bounds future timestamps with `incremental_future_skew_days` so they cannot freeze the watermark, and recovers late arrivals inside `incremental_lookback_days`; see the [Snowflake operations guide](../editions/Snowflake/docs/operations.md#timestamp-edge-contract). That behavior is checked by an offline DuckDB regression, not on Snowflake.

## Acceptance before merging a connector

- The fixture includes an insert, an update inside the lookback, a late update outside the lookback, a duplicate, and a delete/tombstone case where supported.
- Incremental and full-refresh results match after the documented recovery process.
- Tests cover uniqueness, null required fields, status/metric behavior, and late changes.
- API credentials remain local or in a secret manager; logs and tests contain no customer data.
- A maintainer can explain query/warehouse costs and the operational recovery path.

Current state: no additional commerce connector has been chosen. The BigQuery TheLook source and synthetic Snowflake seeds remain the examples.
