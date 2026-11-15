# Source adaptation walkthrough

This synthetic walkthrough maps a retailer's renamed CSV exports onto the
starter's existing retail contracts. It demonstrates grain, field aliases,
update timestamps, deletion limits, and the checks to run before adapting a
real source. The examples contain synthetic values only.

## 1. Identify the row grain and stable key

| File / entity | One row represents | Candidate key | Update signal |
| --- | --- | --- | --- |
| `customers.csv` | One customer | `customer_number` | No update timestamp in this sample |
| `products.csv` | One product | `sku` | No update timestamp in this sample |
| `orders.csv` | One order's current state | `order_number` | `modified_at` |
| `order_items.csv` | One line item | `line_id` | No update timestamp in this sample |

The sample files are under [`migration/examples/source-adaptation/`](https://github.com/de-specialist/de-starter-kits-public/tree/main/migration/examples/source-adaptation).
Their `MOCK-` identifiers make it clear that they are synthetic. The mapping
file is [`retail-source.example.json`](https://github.com/de-specialist/de-starter-kits-public/blob/main/migration/mappings/retail-source.example.json).

Do not choose a key by convenience. Confirm its uniqueness and stability with
the source owner. The sample's `modified_at` is a record-change time, while
`purchase_date` is a business date; only the former can be considered as an
incremental cursor, and only after confirming the source updates it for every
change.

## 2. Map aliases into the canonical fields

The example maps `customer_number` to `customer_id`, `order_number` to
`order_id`, `purchase_date` to `order_date`, and `modified_at` to `updated_at`.
For line items, it maps `line_id`, `order_number`, `sku`, `units`, and
`item_price` to `order_item_id`, `order_id`, `product_id`, `quantity`, and
`unit_price`. The file also maps customer and product aliases.

The loader adds canonical aliases while retaining renamed source fields. These
are prepared rows for downstream models, not a schema-evolution policy. Review
the accepted input columns, casts, and normalized destination names before
loading. Mapping validates Python conversion rules; it cannot establish that
either warehouse will preserve the same numeric or timestamp values.

## 3. Inspect locally without a cloud destination

From the repository root, run the dry run against local DuckDB configuration:

```sh
migration/.venv/bin/python migration/run_migration.py \
  --source csv \
  --destination duckdb \
  --dataset starterkit_migration_example \
  --pipeline-name source_adaptation_example \
  --csv-dir migration/examples/source-adaptation \
  --mapping-file migration/mappings/retail-source.example.json \
  --dry-run \
  --check-retail-contract
```

The report should resolve the four expected tables, show the mapped retail
fields, and set `writes_performed` to `false`. For CSV, field values are text at
inventory time, so `manual_type_or_value_review_required` is expected. That
report does not verify warehouse casts, relationships over a full source, or
warehouse behavior. Do not treat its local DuckDB target identity as a cloud
target approval.

## 4. Define updates and deletes separately

The base migration loader replaces only the selected Bronze tables from the
current export. It does not capture ongoing changes. The `incremental-ingest`
example demonstrates watermarks, replay, lookback, and late-arrival handling
for an append/update feed; it explicitly does not capture hard deletes.

In this fixture, `cancelled` is an order status, not a row deletion. If a source
performs hard deletes, this mapping cannot recover them. Use a complete-snapshot
comparison or a source-provided tombstone/deletion feed and test the chosen
behavior. Do not infer deletion from absence in a partial export.

## 5. Validate before adapting a real source

- Compare incremental results with a full-refresh baseline after inserts,
  updates inside and outside the lookback, duplicate records, and supported
  deletion events.
- Run the canonical uniqueness and relationship tests for customer, product,
  order, and line-item keys. Confirm that cancelled or returned statuses match
  the business metric definitions.
- Record source-owner decisions about grain, key stability, update time,
  timezone, late arrivals, deletes, privacy, and recovery in the
  [source change contract](source-change-contract.md).
- Use a real warehouse only with an approved isolated target. Keep live source
  identifiers, credentials, row values, and acceptance evidence outside this
  public example.

This walkthrough validates the existing CSV mapping and local dry-run path. It
does not add a production connector or prove buyer demand, warehouse parity,
source snapshot consistency, or release readiness.
