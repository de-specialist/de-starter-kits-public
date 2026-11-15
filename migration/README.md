# Full-load migration and Medallion extension

This optional extension copies selected source data to a dedicated Bronze namespace with [dlt](https://dlthub.com/docs/intro), then uses the edition's dbt project for Silver cleanup and Gold retail marts. The BigQuery and Snowflake destinations remain independent. It does **not** alter the default dbt build.

## Supported migration scope

- **CSV:** UTF-8 headed CSV files, one entity per file. The filename (without `.csv`) becomes the Bronze table name. Rows stream one at a time; each row gets `_migration_run_id`, `_ingested_at_utc`, and `_source_file_name` metadata.
- **SQL databases:** SQLAlchemy's `sql_database` source with an explicit schema and selected table list. Only PostgreSQL using `postgresql+psycopg://` and existing SQLite files are accepted; all other dialects fail closed. Rows receive run, ingestion-time, and source-entity metadata.
- **Initial load behavior:** this version performs a full table replacement for selected Bronze tables. Rerunning replaces those same selected tables. It does not yet capture ongoing changes or perform CDC. It migrates table data, not indexes, constraints, views, stored procedures, users, or grants.
- **Retail Silver/Gold template:** expects `customers`, `products`, `orders`, and `order_items` with the sample field names in `examples/csv/`. Arbitrary SQL tables can be copied to Bronze, but need adapted Silver models before they can use the retail Gold marts.

## Generic template catalog

For a worked example that maps renamed synthetic exports into the canonical
retail fields and explains grain, updates, deletes, and validation, see the
[source adaptation walkthrough](../docs/source-adaptation-walkthrough.md).

- [snapshot-contract](https://github.com/de-specialist/de-starter-kits-public/blob/main/migration/templates/snapshot-contract/README.md): Defines delivery completeness, correction, duplicate, and empty-period rules for full snapshots; does not enforce them.
- [scd2-history](https://github.com/de-specialist/de-starter-kits-public/blob/main/migration/templates/scd2-history/README.md): Builds type 2 entity history from complete snapshots or changes. Snapshot history records observed states and cannot recover changes between observations.
- [incremental-ingest](https://github.com/de-specialist/de-starter-kits-public/blob/main/migration/templates/incremental-ingest/README.md): A local CSV-to-SQLite reference runner with watermarks and late-arrival handling, not a warehouse connector or CDC. Hard deletes are not captured.
- [hr-analytics](https://github.com/de-specialist/de-starter-kits-public/blob/main/migration/templates/hr-analytics/README.md): A design-only synthetic HR example with contracts, mappings, proposed reports, and draft DAX; no deployed pipeline.

## Generic HR design template

The optional [generic HR template](https://github.com/de-specialist/de-starter-kits-public/blob/main/migration/templates/hr-analytics/README.md) is a self-contained, design-only synthetic example: generic canonical contracts and mappings, proposed report views, separate analyst and leadership semantic contracts, draft DAX, and synthetic fixtures. Restricted or unapproved attributes are excluded; all fixture values are synthetic. It does not extend the migration CLI's supported sources or destinations, install dbt models, or deploy Fabric resources. Logical Gold names require adaptation to the selected edition's conventions; existing retail builds, profiles, and dependencies remain separate.

**Leadership publication is blocked** until an owner approves the disclosure policy and primary and complementary suppression are implemented upstream and independently tested. Leadership must use a separate aggregate-only model with no employee keys, detail facts, exact employee event dates, or access path to detail. Tests must cover totals, intersecting filters, repeated releases/differencing, exports, and permission isolation. Draft DAX, hidden columns, RLS, and a disclosure-status column do not establish this security boundary; no suppression threshold is assumed.

Run the local checks from the repository root (requires PyYAML):

```sh
python3 migration/templates/hr-analytics/validate_fixtures.py
```

The validator checks public-content policy markers, MOCK identifier prefixes, YAML/CSV structure, contract/mapping and analyst DAX references, report coverage, synthetic KPI arithmetic, and edge scenarios. It does not execute DAX/dbt, implement suppression, verify Fabric permissions, or establish real report equivalence. Source authority, complete snapshot manifests, historical attributes, corrected-event identity, overlapping extracts, and approved absence counter definitions remain production release blockers. Keep real-data onboarding, cloud acceptance, and deployment in a separate private repository.

## Offline regression tests

From the repository root, install the hash-locked test dependencies and run the suite with `CI=true`. The CI flag prevents migration tests from passing by skipping when a required dependency is absent. The suite uses local synthetic fixtures and temporary targets; it does not connect to BigQuery or Snowflake.

```sh
python3.11 -m venv migration/.venv
migration/.venv/bin/python -m pip install --disable-pip-version-check --require-hashes -r migration/tests-requirements-lock.txt
CI=true migration/.venv/bin/python -m unittest discover -s scripts/tests -v
CI=true migration/.venv/bin/python -m unittest discover -s migration/tests -v
```

Schema drift baselines may be dry-run reports or completed run manifests. A run manifest is accepted only when both its name and type-family fingerprints were recorded as matching the schema at extraction. Failed, running, interrupted, or mismatched runs are not review evidence. Baseline files and their parent directories must be owner-only, and every path component must be a real directory rather than a symlink.

The dedicated Bronze namespace also contains dlt-managed system tables: `_dlt_loads`, `_dlt_pipeline_state`, and `_dlt_version` ([dlt internal table reference](https://dlthub.com/docs/general-usage/dataset-access/dataset)). Loaded data tables may contain dlt lineage columns such as `_dlt_load_id`, `_dlt_id`, and parent-row identifiers. Treat the `_dlt_` prefix and these objects as reserved; do not alter or remove them manually. Keep the Bronze namespace isolated so dlt metadata stays separate from application models.

The SQL source URL is read from an environment variable (default name `MIGRATION_SOURCE_URL`) and never added to the manifest or command output. Do not put passwords or tokens in a command-line argument. SQL source connections are constructed by the CLI as read-only, bounded engines and passed directly to dlt. PostgreSQL connections set and verify `default_transaction_read_only=on`, a 60-second server statement timeout, a 10-second lock timeout, and a 10-second libpq connection timeout; multi-host PostgreSQL URLs are rejected. Use a least-privilege SELECT-only source role and prefer base tables: database views or extensions could invoke functions with external side effects outside ordinary SQL DML. SQLite must be an existing, stable file snapshot; it opens through SQLite URI `mode=ro&immutable=1`, verifies `PRAGMA query_only=ON`, and applies a 60-second cooperative statement progress deadline. `immutable=1` prevents SQLite from creating WAL/SHM sidecars, so do not use it for a database that is changing during the migration. Existing SQLite query overrides and PostgreSQL `options`/`connect_timeout` overrides are rejected. SQLite's progress handler may not interrupt a driver blocked in I/O; neither dialect's single-row materialization is bounded by these query timers. These controls protect extraction, inventory, and optional profiling; they do not guarantee total process-memory use. The implementation follows SQLAlchemy's [SQLite URI configuration](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#uri-connections), PostgreSQL's [libpq connection options](https://www.postgresql.org/docs/current/libpq-connect.html), and [client connection defaults](https://www.postgresql.org/docs/current/runtime-config-client.html); SQLite's [URI](https://www.sqlite.org/uri.html) and [WAL read-only](https://www.sqlite.org/wal.html#read_only_databases) rules explain the immutable snapshot constraint. dlt working directories and pipeline state are pinned to `migration/.dlt/pipelines/`; extracted files and run manifests are also under `migration/` and ignored by Git. They may contain copied data or sensitive operational metadata, so keep them private. On POSIX systems, the CLI applies a private umask and creates/chmods its state and manifest directories to owner-only access; inspect existing files separately if they predate this change. Run manifests are created exclusively; later status updates use a private temporary file and atomic replacement so an interrupted update preserves the previous complete manifest. A process interruption during the initial create can still leave a partial manifest, which downstream readers reject.

The CLI pins dlt's project settings to `migration/.dlt` before importing dlt, preserves source paths relative to the caller's working directory, and disables telemetry before runtime initialization. Keep `runtime.dlthub_telemetry = false` in the example config as a second guard. Python logging is suppressed in these CLI processes so driver diagnostics cannot print credentials or connection details; operational status is emitted through explicit CLI messages.

The CLI uses dlt 1.32.0's resolved destination client configuration, so environment variables and project/global `.dlt` config layers are checked as one effective configuration. BigQuery requires the effective `project_id` and `BQ_PROJECT` to match `--target-id`; Snowflake requires the resolved account/database and matching `SNOWFLAKE_ACCOUNT`/`SNOWFLAKE_DATABASE`. Snowflake account/database identifiers may contain underscores. Custom Snowflake endpoints, staging destinations, custom staging namespace layouts, and replacement strategies other than `truncate-and-insert` are rejected because they can redirect or expand the confirmed write scope. Cloud destinations must already have the confirmed namespace. The CLI performs a read-only metadata readiness probe before extraction and repeats it after normalization immediately before `pipeline.load`; any probe error or absent namespace fails closed without starting the load. A race remains if another actor removes or changes the namespace after the final probe and before the load operation; these separate API calls cannot make the check atomic with loading. The CLI never prints credentials or local secrets file contents.

## Bronze reconciliation report

After a successful load, the run manifest records application-counted rows yielded during extraction and canonical JSON payload bytes for CSV and SQL inputs, plus dlt 1.32.0 `NormalizeInfo.row_counts` as normalized/prepared item counts. For SQL, the empty-source preflight uses only a bounded existence probe; the extraction row count is not an independent pre-load `COUNT(*)` or proof of the source's full size. Generate an offline report without contacting the destination:

Failed manifests identify each entity as `not_started`, `incomplete`, or `complete`. `source_row_counts.rows` and payload bytes are null unless extraction completed; `accepted_extraction_progress` separately records rows and bytes accepted before an interruption or rejection. An explicitly authorized empty entity is recorded as complete with zero rows only after its resource finishes successfully.

```sh
migration/.venv/bin/python migration/reconcile_bronze.py --run-id YOUR_COMPLETED_RUN_UUID
```

The CLI prints the exact comma-separated normalized table confirmation needed for the query step. The offline report includes the same list in `normalized_destination_tables`. Reports are saved as `<run-id>.bronze-reconciliation.offline.json` or `<run-id>.bronze-reconciliation.queried.json`; each mode has its own path, and an existing report is never overwritten. The reconciliation command also requires the run-manifest directory and manifest file to be current-user-owned with owner-only permissions. To additionally compare prepared counts with current full-table Bronze counts, opt in to a destination query and type both the exact target identity from the run manifest and the exact comma-separated normalized table list printed by the offline command:

```sh
migration/.venv/bin/python migration/reconcile_bronze.py \
  --run-id YOUR_COMPLETED_RUN_UUID \
  --query-destination \
  --target-id YOUR_PROJECT_OR_ACCOUNT_DATABASE \
  --confirm-target EXACT_TARGET_IDENTITY_FROM_RUN \
  --confirm-tables EXACT_COMMA_SEPARATED_TABLE_LIST_FROM_OFFLINE_REPORT
```

The query runs dlt `Dataset.row_counts` for the confirmed normalized table names; it contacts the warehouse and may incur compute cost. Reports are written exclusively with owner-only permissions beside the private run manifest and contain table names and counts, but no row values or credentials. Destination counts are current full-table counts after replacement, not load-job row counts; concurrent writes can change them. Each table and the report carry explicit `count_match`, `count_mismatch`, or `count_unavailable` status, plus `report_type: count_only_diagnostic` and `cutover_ready: false`. This report does not establish cutover readiness: matching counts do not establish key, value, reject, schema, or business-metric equivalence. The report covers Bronze counts only and does not reconcile Silver or Gold.

For supported retail entities from a local CSV directory or SQLite source, run a source-only key/relationship diagnostic without querying a destination:

```sh
migration/.venv/bin/python migration/reconcile_bronze.py \
  --run-id YOUR_COMPLETED_RUN_UUID --local-keys
```

The command re-reads the source and checks missing/blank and duplicate keys plus the modeled order/customer and order-item/order/product relationships. It refuses PostgreSQL and other unsupported sources, enforces a per-entity row cap, and writes an exclusive owner-only `key-reconciliation.local.json` containing aggregate counts and statuses only. Key values remain in memory and never enter the report. The run manifest, source schema, mapping fingerprint, and row counts must still match. This is source evidence only: it does not compare keys with Bronze, Silver, or Gold and does not make the run cutover-ready. `acceptance_report.py RUN_UUID` includes the local report when present; use `--key-reconciliation local` to require it or `none` to omit it.

For this command, `--target-id` is the non-secret project or account/database selected for configuration checks; `--confirm-target` must exactly match the fully resolved target identity, including the physical namespace, printed by dry-run. The target and table flags serve different checks. Before a real load, also pass the exact `schema_confirmation_fingerprint` printed by dry-run as `--confirm-schema-fingerprint`.

`--source-binding-id` is an optional, non-sensitive operator alias matching `[a-z][a-z0-9_]{0,39}` (for example, `retail_source_a`). Pass the same generic alias on the dry run and the load; when supplied, it is recorded exactly as `source_binding_id` in the dry-run JSON and the run manifest so a later scaffold can require the two to be equal. It is an operator assertion only: it is never a credential, is not read from environment variables or config, and is not proof of source identity. Do not put hostnames, database names, URLs, or secrets in it. Invalid values fail with a fixed message that does not repeat the input. When omitted, behavior is unchanged and the field is absent; such legacy evidence cannot be used for final scaffold generation.

Before replacing tables, the CLI prints the dlt-normalized destination table list. Every destination, including local DuckDB, requires `--confirm-target` with the exact target identity, `--confirm-tables` with the exact comma-separated destination table list in the printed order, and `--confirm-schema-fingerprint` with the combined schema/mapping fingerprint from dry-run. The fingerprint binds ordered table/column names, available source type families, and the reviewed mapping fingerprint; for CSV, source types are marked untyped text and do not include dlt's later inferred destination types. The real run recomputes the value from current inventory and mapping configuration and stops before extraction if it differs. It also compares inventory with the actual opened CSV headers or SQL metadata passed to extraction, stopping before load on change. For DuckDB, the target identity includes its canonical absolute file path under `migration/.dlt/pipelines/<pipeline_name>/<pipeline_name>.duckdb` plus namespace. For cloud, it includes project or account/database plus physical namespace. Get all three confirmation values from `--dry-run` output. dlt pending packages are rejected before loading, preventing a new invocation from silently processing leftovers. Failed run manifests retain safely formatted load IDs where available plus aggregate package/job counts; `destination_write_may_be_partial: true` is set immediately before `pipeline.load()` and means writes may have started, not that a write was observed. A failed replacement is not rolled back automatically. A zero-row CSV or SQL source is blocked by default so it cannot replace a non-empty Bronze table with an empty table. To intentionally replace an empty source, pass `--allow-empty-tables` with only the selected empty table names, for example `--allow-empty-tables customers`. SQL preflight uses one bounded `SELECT 1 ... LIMIT 1` probe per selected table; this still reads the source and may incur source-side query/load costs. A table could change between preflight and extraction, so run against a stable source or maintenance window.

### Extraction limits and rejection handling

The loader fully replaces selected tables, so it enforces payload budgets while reading each row and before destination loading. Defaults are 250,000 rows per table, 16 MiB per row, 256 MiB per table, plus 1,000,000 rows and 1 GiB across the run. Raise these only after reviewing source size and available local disk; all limits accept positive integers:

```sh
export BQ_PROJECT='YOUR_NONPROD_PROJECT_ID'
export DESTINATION__BIGQUERY__PROJECT_ID="$BQ_PROJECT"
migration/.venv/bin/python migration/run_migration.py \
  --source csv --destination duckdb --dataset starterkit_migration_bronze \
  --csv-dir migration/examples/csv \
  --max-rows-per-table 500000 --max-bytes-per-row 33554432 \
  --max-bytes-per-table 536870912 \
  --max-rows-total 2000000 --max-bytes-total 2147483648 --dry-run
```

The example uses `--dry-run` to show the option syntax; it reports the configured limits but does not extract rows or prove that the source fits within them. Limits are enforced only during an actual load, before destination writes.

Payload bytes are measured from deterministic canonical UTF-8 JSON after migration lineage fields are added. The 16 MiB per-row limit rejects an oversized row before the loader builds its JSON serialization copy. dlt 1.32.0's SQLAlchemy backend uses `chunk_size=1`; its internal result buffer may hold up to twice that chunk size. A database driver may still materialize one oversized source row before the application can inspect it. CSV parsing, driver buffers, Python object overhead, and dlt normalization also use memory, so these payload limits do not bound total process memory or dlt package size. They do not predict destination storage size either. Extraction and normalization are staged before `pipeline.load`; malformed CSV rows, unsupported SQL values, serialization failures, and exceeded limits stop before destination loading. The private failure manifest contains only the table, ordinal where available, reason code, and accepted row/byte counts. It never records rejected values or exception text. SQL empty checks still probe the source before extraction; `--dry-run` also scans CSV files to report row counts. Failed extraction may leave a local dlt package; inspect it using the pending-package guidance above before choosing to abort it. No automatic resume or cleanup is performed.

### Inspect and resolve stale pending packages

If the loader stops because a pipeline has pending dlt packages, do not rerun the migration or remove files from `.dlt/pipelines` by hand. An initial pending-package refusal happens before a new run manifest is created. If no manifest exists, list local pipelines and identify the expected pipeline from the original `--pipeline-name`, destination, dataset, and target configuration. Pipeline names include a derived target hash, so do not guess a name; inspect the list and resolve ambiguity before acting. If a private run manifest exists, use its pipeline name, recorded load IDs, and conservative `destination_write_may_be_partial` flag as additional context; the flag is set immediately before `pipeline.load()` and means writes may have started, not that a write was observed. Keep command output private because it may include destination error details or local file paths. These commands follow the [dlt CLI reference](https://dlthub.com/docs/reference/command-line-interface):

```sh
migration/.venv/bin/dlt pipeline --list-pipelines --pipelines-dir migration/.dlt/pipelines
migration/.venv/bin/dlt pipeline --pipelines-dir migration/.dlt/pipelines PIPELINE_NAME info
migration/.venv/bin/dlt pipeline --pipelines-dir migration/.dlt/pipelines PIPELINE_NAME load-package LOAD_ID info
migration/.venv/bin/dlt pipeline --pipelines-dir migration/.dlt/pipelines PIPELINE_NAME load-package LOAD_ID row-counts
migration/.venv/bin/dlt pipeline --pipelines-dir migration/.dlt/pipelines PIPELINE_NAME failed-jobs
```

Check the destination before resolving a failed package: some jobs may already have committed rows even though the package is incomplete. If, after reviewing every pending package and any partial destination writes, you deliberately choose to abandon the pending work, use dlt's interactive abort command:

```sh
migration/.venv/bin/dlt pipeline --pipelines-dir migration/.dlt/pipelines PIPELINE_NAME abort-packages
```

Review dlt's abort plan before confirming. dlt documents that a failed load may already have committed some jobs; aborting pending packages can discard extracted/normalized data and does not roll back rows already written to the destination ([load recovery guidance](https://dlthub.com/docs/running-in-production/running)). Reconcile or repair the affected Bronze tables before starting a new full replacement. Do not use the deprecated `drop-pending-packages` command as a cleanup shortcut.

## Aggregate reconciliation across layers

`aggregate_reconciliation.py` compares allowlisted business metrics across Source, Bronze, Silver, and Gold using local SQLite (`.sqlite`, `.sqlite3`, `.db`) or DuckDB (`.duckdb`) files opened read-only. It never contacts a warehouse. DuckDB files are opened with external access, extension autoloading and autoinstalling, and Python replacement scans disabled, and the configuration is locked before any query. A view stored in a layer file that reads another file or a URL therefore fails as `table_unavailable` instead of being scanned.

```sh
migration/.venv/bin/python migration/aggregate_reconciliation.py \
  --run-id YOUR_RUN_UUID --contract aggregate-contract.json \
  --layer source=source.sqlite --layer bronze=bronze.duckdb \
  --layer silver=silver.duckdb --layer gold=gold.duckdb
```

The contract (`contract_version: 1`) lists metrics. Each metric states its rules explicitly:

- `aggregation`: `count_rows`, `count_distinct`, or `sum`.
- `unit`: `count`, `currency_major`, `currency_minor`, or `quantity`. Currency units also need an ISO 4217 `currency`.
- `scale`: decimal places, from 0 to 9.
- `grain`: logical dimensions. An empty grain means one total.
- `null_measure`: `ignore` or `fail`.
- `null_grain`: `exclude`, `group`, or `fail`.
- `zero_value_groups`: `equivalent_to_absent` or `significant`. This decides whether a Gold customer with zero orders matches a customer that has no source rows.
- `status_rule`: `null`, or a name plus the canonical statuses to include.

Each of at least two layers gives a `table`, `grain_columns`, and an optional `measure`. A layer with a status rule also gives a status `column`, with optional per-layer raw-value `values`, or declares `prefiltered: true`. Use `aggregation: precounted` for a layer that stores a pre-computed count. Pre-computed row counts may be summed. A pre-computed distinct count is accepted only when the layer has one row per group; otherwise it is reported as `distinct_count_not_summable`. Unknown fields and duplicate JSON keys are rejected.

The report compares adjacent layers, plus first-to-last. For each comparison it records how many groups matched, had a different value, were missing on the right, or were extra on the right. Status precedence is `mismatched` > `null_policy_violation` > `unavailable` > `matched`. Within one layer, a null-policy violation takes precedence over a data error such as `measure_not_numeric`, wherever the rows appear. Rows with both a null grain and a null measure are counted only as `null_grain`. Per-layer entries give row counts (scanned, kept, excluded by status, null measure, null grain), group counts, and machine reason codes; database error text is never included. Sums are canonical decimal strings at the metric scale, never floats.

Grain keys are matched after text normalisation, not by type, so the same group can be found across engines. Text `'1'`, integer `1`, and decimal `1.0` are the same key, as are `true` and `'true'`, and a date and its ISO text. Within one layer, such values merge into one group. Text is not trimmed or case-folded, so a blank or space-only key is its own group. A type drift that keeps the same text is therefore not reported; check column types separately if that matters.

What the report protects:

- Grain keys, row values, raw statuses, and database error text never appear in it. They stay in process memory.
- A layer's total is emitted only when the layer kept at least `min_rows_for_total` rows (default 10); otherwise `total_status` is `suppressed_min_rows`.
- Two totals are never emitted together when their layers differ by a nonzero number of kept rows, or of differing groups (value mismatch, missing, or extra), below `min_rows_for_total`. Every pair of layers is checked, including pairs that are not reported as comparisons. The most conflicted, then most downstream, layer gets `suppressed_small_difference` until no such pair remains. So one extra Silver row, or one changed group, cannot be read off as the difference between two totals.

What it does not protect:

- Differences between reports from separate runs. Two runs that differ by one row each emit totals, and the difference reveals that row. Keep reports private and do not compare totals across runs that may differ by a few rows.
- Row and group counts. These are always reported, so a count metric's value (or the number of rows that changed) is visible.
- Differences of `min_rows_for_total` or more rows or groups reveal only their combined value, not a single row. The threshold is a simple disclosure control, not differential privacy.
- Physical table and column names appear only as an unsalted SHA-256 `contract_fingerprint`. Anyone who can guess the contract can confirm the guess, so do not rely on the fingerprint to hide names if a report leaves the machine.

`validate_report` rejects reports whose statuses, counts, or totals are inconsistent, including a matched comparison with different totals, a direct row-count total that differs from its kept rows, and totals disclosed across a small difference.

The report is created exclusively as an owner-only `<run-id>.aggregate-reconciliation.local.json` in `migration/run-manifests/` (change it with `--output-dir`), and an existing report is never overwritten. It is marked `evidence_scope: local_only` and `cutover_ready: false`. It is not yet included in `acceptance_report.py`.

## Inventory state for retention planning

Use the standard-library inventory command to review migration pipeline-state and run-manifest paths, filesystem sizes, modification ages, and ownership/permission warnings:

```sh
migration/.venv/bin/python migration/inventory_state.py
```

Use `--older-than-days 30` to mark metadata paths for retention review. This is a candidate preview only: it reads filesystem metadata, opens no manifest contents, and has no deletion mode. A safe retry decision follows this sequence: (1) classify state as extraction, normalization, or load; (2) inspect pending package and load-job evidence; (3) check whether any destination jobs committed; (4) reconcile affected Bronze tables; (5) if deliberately abandoning pending state, review and confirm `abort-packages`; and (6) only after pending state is resolved, start a new replacement. The tool never resumes or aborts automatically, and local package cleanup cannot roll back destination writes. No cloud cleanup or rollback command is provided.

It prints a JSON report to stdout and does not write a report file, delete or modify anything, or open file contents. It reports metadata only; pipeline files and manifests may still contain sensitive data, so keep the output private. Use the paths, sizes, ages, and warnings to propose a retention period with the data owner. This command performs no cleanup, and age alone is not approval to remove state or manifests.

## Source inventory dry run

Use `--dry-run` to inspect selected source inputs before loading. It prints JSON with source and destination table names, columns and source types, missing/empty status, row counts when available, name-level and typed schema fingerprints, the source mapping fingerprint, combined `schema_confirmation_fingerprint`, exact target/table confirmation strings, normalized-identifier safety, `target_verified`, `namespace_verified`, `preflight_ready`, configured extraction limits, and `writes_performed: false`. The name-level fingerprint covers selected table and ordered column names. The typed fingerprint adds normalized SQL type families; CSV fields are marked untyped input text because destination inference is not known before normalization. The combined fingerprint is a SHA-256 over the name, type, and mapping fingerprints plus an algorithm label. These fingerprints do not cover constraints or data values and can reveal schema equality, so they are not anonymization mechanisms. Copy `schema_confirmation_fingerprint` exactly into `--confirm-schema-fingerprint` for a real invocation. The loader recomputes it from current inventory and mapping configuration, refuses to extract if missing or mismatched, and separately compares fingerprints with the exact opened CSV headers or SQLAlchemy metadata used for extraction before loading.

`--mapping-file` optionally adds reviewed canonical aliases to Bronze rows. Source columns are preserved except when a mapped target has the same dlt-normalized name as its source: then the mapping casts and replaces that source in place, avoiding a normalized-name collision (for example, `Order Date` to `order_date`). The mapping file’s final path component is opened without following symlinks and read with a 1 MiB cap; parent directories in a caller-supplied path may contain symlinks. Platforms without `O_NOFOLLOW` fail closed. Safe mapping validation messages are shown to help correct the file; JSON parser internals and file contents are suppressed. It must be a regular file, use version 1, and contain only `version` and `tables` top-level keys. Each rule names `source`, `target`, and an allowlisted cast (`string`, `integer`, `numeric`, `date`, `timestamp`, or `boolean`) using Python conversion semantics. A mapping source is the exact raw column name, bounded to 255 characters and rejected if it contains control characters or newlines; it is matched as an in-memory row key and is never interpolated into SQL. CSV headers are trimmed of surrounding whitespace before matching. SQL source names match the exact SQLAlchemy-reflected identifier, including case; SQL reads use reflected column objects. Targets and table names remain restricted safe identifiers. Known retail targets are checked after dlt snake-case normalization, so aliases such as `OrderDate` cannot bypass the canonical cast requirement. Unknown sources, existing normalized targets, duplicate rules, reserved names, and failed casts stop before destination loading; only reason codes and counts are recorded, never values. Source columns that collide after dlt normalization also stop before extraction, including when a mapping is supplied; the in-place replacement exception applies only when the mapped source is the sole column with that normalized name. Binary SQL values cannot be implicitly mapped to strings because Python would stringify them as representations such as `b'value'`; decode them with a reviewed source-side conversion before mapping. These Python conversions are not warehouse-equivalent validations: they do not prove BigQuery or Snowflake will accept the same values or preserve them without rounding, timezone conversion, or type coercion. In particular, warehouse numeric precision/range and timestamp interpretation remain unverified. A blank CSV cell is an empty string and fails a non-string mapping cast; a SQL NULL remains null. In the value profile, blank means an empty string or ASCII U+0020-space-only string; tabs and other whitespace remain data. Blank values in non-string contract fields increment both `blank` and `cast_failure`. The mapping fingerprint is deterministic and is not anonymization. Use the same mapping file in dry-run and load, and review its report before confirming a replacement. [retail-source.example.json](mappings/retail-source.example.json) demonstrates hypothetical source headers; adapt it to your actual inputs before use. Do not pass it unchanged with the bundled sample CSVs.

### Source snapshot consistency

See the [source snapshot contract](../docs/source-snapshot-contract.md) for CSV, immutable SQLite, and PostgreSQL guarantees and limitations across inventory and extraction, including what operators must record and what must not enter manifests.

Before extraction, determine whether the source can provide a reliable, consistent snapshot or version boundary. Where the source platform exposes a supported snapshot/version marker, verify it through that platform's normal controls and retain the evidence in an appropriately restricted operational record; do not add raw snapshot identifiers to migration manifests. When a reliable marker is unavailable, agree and record a maintenance window with the source owner, and avoid concurrent source changes during inventory and extraction where practical. The loader does not open a transactionally consistent snapshot or verify that source rows remained unchanged. Schema fingerprints describe schema and mapping, while CSV filenames and file sizes are pre-extraction metadata; neither proves row-level snapshot consistency or which exact source version was loaded.

Private load manifests record source references without local paths or connection URLs. CSV filenames and filesystem sizes are labeled `pre_extraction_file_metadata`; they are read before extraction, may change before extraction, and do not prove which bytes were consumed. No content hash is recorded, avoiding a second full-file read and deterministic equality leakage. SQL schema/table names are labeled `descriptive_names_not_unique`; the redacted connection marker does not distinguish databases. Filenames and sizes are still identifying metadata, so keep manifests private. The evidence index accepts legacy hash-bearing manifests but drops their content hashes from its output. Shared private JSON readers reject duplicate object keys and accept UTF-8 only; nonblocking, no-follow opens reject special files without hanging. Manifests written by this CLI use that format. Manifests contain no row values.

### Bounded retail value profile

For source diagnostics, add `--profile-retail-values` to a dry run for a bounded aggregate profile of the modeled retail fields. The default cap is 10,000 rows and 64 MiB of canonicalized source-row payload per table; override with `--profile-max-rows-per-table` and `--profile-max-bytes-per-table`. It reports sampled rows/bytes, truncation, nulls, blank strings, cast failures, and duplicate configured primary keys. For keys, blank means NULL, empty text, or ASCII U+0020 spaces only, matching the Silver filter; padded nonblank keys are preserved, and tabs or other non-space characters remain data. Cast counts use Python conversion semantics; `cast_validation.warehouse_castability` is explicitly `not_verified`. Missing contract fields are marked unavailable rather than counted as null, and their duplicate counts are unavailable. Source and key values are never printed or stored. The capped size estimator checks a row before constructing a JSON serialization copy; the CSV parser or SQL driver may already have materialized one row. The byte cap therefore does not bound total process memory or driver buffers. CSV inventory separately scans all rows to report its full count. SQL profiling reads contract fields only with a row limit and incurs source query cost; it does not provide a transactionally consistent snapshot. The profile is diagnostic and does not authorize a load or prove full-table relationship coverage.

When the bounded profile includes both sides of a modeled relationship, it also reports sampled non-null child-key counts and sampled orphan counts. These comparisons retain key values only in process memory and emit counts only. NULL, empty, and ASCII U+0020-space-only keys are excluded, matching the Silver key policy; tabs, other whitespace, and padded nonblank keys remain exact values. Complete scans of both selected tables are labeled `sample_complete`; row/byte truncation is `sample_only`, and missing key fields or unselected tables are explicitly unavailable. These are not full-table reconciliation results and never set cutover readiness.

To create a private summary index from a completed or failed run manifest, run `migration/.venv/bin/python migration/evidence_index.py <manifest-filename>`. It accepts only an owner-only manifest under `migration/run-manifests/`, creates a new owner-only `evidence-index-<run_id>.json` there, and refuses to overwrite. Review the recorded partial-write flag and stated validation limits; this index is not an independent warehouse check or release approval.

### Machine-readable acceptance summary

After a run, generate an allowlisted machine-readable summary with:

```sh
migration/.venv/bin/python migration/acceptance_report.py YOUR_RUN_UUID
```

The command reads the owner-only run manifest and, when present, an existing Bronze count report and local key report. It never contacts a destination. Use `--reconciliation none` to omit count reconciliation, or explicitly select `offline` or `queried`. Use `--key-reconciliation local` to require local source-key evidence or `none` to omit it. The output is created exclusively as an owner-only `acceptance-report-<run_id>.json`; it refuses to overwrite an existing report. It contains aggregate extraction counts, schema/mapping fingerprints and mapping rule count, allowlisted reject reason/count evidence, recorded dlt load/job evidence, optional Bronze count reconciliation, and optional local key/relationship counts. Source entity names are paired with normalized Bronze table names in manifest order; these names may differ after dlt normalization. Reconciliation reports whose counts or provenance contradict the run manifest are rejected. The report omits target identities, local paths, source URLs, credentials, key values, row values, and unrecognized manifest fields. It marks `cutover_ready: false`; mapping semantics are not stored in the manifest, and count/local-key reports do not prove source-to-Bronze equality or Silver/Gold metric equivalence. Human mapping review, dbt Silver/Gold checks, cloud parity, cost review, and cutover approval remain separate.

Example preflight after adapting the mapping to the actual source fields:

```sh
migration/.venv/bin/python migration/run_migration.py \
  --source csv --destination duckdb --dataset starterkit_migration_bronze \
  --csv-dir migration/examples/csv --mapping-file migration/mappings/your-reviewed-map.json \
  --dry-run --check-retail-contract
```

For the real load, pass the same `--mapping-file` along with the exact `--confirm-target`, `--confirm-tables`, and `--confirm-schema-fingerprint` returned by that dry run. A failed cast rejects extraction before destination loading; the manifest records a generic `mapping_cast_failed` reason and accepted counts. This is a fail-closed conversion path, not a complete value-loss profiling report. Review timezone assumptions and numeric precision before approving casts.

Mapped `integer` and `numeric` values are also checked against an explicit destination numeric type profile while rows are extracted, before normalization or loading. The profiles mirror the column types dlt creates for the loader's `bigint` and unparameterized `decimal` hints:

| Destination | `integer` column / enforced range | `numeric` column / enforced bounds |
|---|---|---|
| BigQuery | `INT64` / signed 64-bit | `NUMERIC(38,9)` / ≤29 integer digits, ≤9 significant fractional digits |
| Snowflake | `NUMBER(19,0)` / signed 64-bit (stricter than the column, matching BigQuery) | `NUMBER(38,9)` / same as BigQuery |
| DuckDB (local rehearsal) | `BIGINT` / signed 64-bit | `DECIMAL(38,9)` / same as BigQuery |

Values that would need rounding are rejected rather than rounded; trailing fractional zeros are not significant. A violation stops the run with one fixed reason code — `integer_out_of_destination_range`, `decimal_integer_digits_exceed_destination_profile`, `decimal_scale_exceeds_destination_profile`, or `destination_bound_value_unsupported` — and never records the rejected value. Dry-run output and run manifests include the profile under `destination_type_profile`. Offline tests compare the profiles with the installed dlt type mappers, so a dlt upgrade that changes these column types fails the test suite. Limits: unmapped columns, non-numeric casts, dbt Silver/Gold warehouse casts, and Snowflake `use_decfloat` configurations (where the profile remains stricter than `DECFLOAT`) are not covered. No BigQuery or Snowflake load has validated these bounds.

`ready_to_load` is always false in dry-run because the actual row payload budgets are enforced only during extraction; `extraction_limits_enforced` is false. Identifier safety uses the effective dlt destination schema naming and case-folding rules to identify table or column collisions with dlt's reserved `_dlt` prefix, collisions among normalized columns, and collisions with generated migration lineage fields. During a real run this check is repeated from the exact opened CSV header or from SQLAlchemy metadata passed to dlt for extraction. A dry run uses and removes a temporary local dlt state directory and does not create persistent state, a manifest, or destination objects. It never verifies cloud namespace existence, so `namespace_verified` is false for cloud targets; `preflight_ready` is not a cloud-write authorization. A real cloud invocation must independently pass its pre-write namespace check and the exact target/table confirmations. CSV row counts are exact and require scanning the files; field values enter the loader as text, but this report does not validate dlt destination-schema inference, variant splitting, or later dbt castability. SQL inventory reflects selected tables and reports reflected SQLAlchemy types; it runs a bounded existence probe and does not count every row, and may incur source-side query costs. SQL errors other than a confirmed missing table stop the inventory without exposing connection details. Review blockers, `blocked_reserved_metadata_columns`, normalized-identifier issues, mapping errors, and missing inputs before attempting a load.

Add `--check-retail-contract` to a dry run for an informational comparison with the canonical Silver source columns, casts, and key tests in both editions' `models/migration/` projects. The report identifies missing/extra columns and tables, reflected SQL type mismatches, and CSV fields whose values need review. CSV fields enter extraction as text, but dlt may infer destination types or split variants during normalization; neither that inference nor later dbt castability is validated here. SQL numeric, decimal, and floating source types used as modeled string keys are marked `numeric_key_stringification_unverified`: BigQuery and Snowflake can stringify such values differently, and this profile does not verify parity. Integer and string source-key classifications retain their existing behavior. Read `medallion_readiness` as a schema-level signal: missing/incompatible inputs, review required, or schema-compatible with dbt data tests still pending. It also lists uniqueness and relationship tests that dbt must run after loading. This option does not block or change Bronze loads; arbitrary tables remain loadable, and no implicit mappings are proposed.

```sh
migration/.venv/bin/python migration/run_migration.py \
  --source csv --destination bigquery --dataset starterkit_migration_bronze \
  --target-id "$BQ_PROJECT" --csv-dir migration/examples/csv \
  --dry-run --check-retail-contract

# Set MIGRATION_SOURCE_URL securely in your shell or secret manager first.
migration/.venv/bin/python migration/run_migration.py \
  --source sql --destination snowflake --dataset starterkit_migration_bronze \
  --source-schema public --tables customers,orders --dry-run
```

The dry run does not write to the destination or require destructive-action confirmation, but it resolves the effective dlt destination configuration and may resolve local credentials (for example, Google Application Default Credentials may inspect local or machine identity sources). It does not query the destination or verify cloud namespace existence. `preflight_ready` covers inventory and local target checks only; `ready_to_load` remains false because row budgets have not been exercised. An actual cloud load separately checks the namespace and requires exact target and table confirmations; the target must also match both dlt and dbt environment settings.

For the first dry run, do not expect an immediately load-ready report. Missing target/table confirmations and `namespace_not_verified_by_config_resolving_dry_run` are expected because the report is the source of the exact confirmation values and dry-run never checks the remote dataset. Review other blockers as real issues; then use the reported target, tables, and schema fingerprint for the confirmed load, which performs its own namespace check.

## Install

Python 3.11 is required. `uv` is optional; use either installation path below from the repository root:

```sh
uv venv --python 3.11 migration/.venv
uv pip install --python migration/.venv/bin/python --require-hashes -r migration/requirements-lock.txt
```

Without `uv`, use Python's built-in venv and pip:

```sh
python3.11 -m venv migration/.venv
migration/.venv/bin/python -m pip install --disable-pip-version-check --require-hashes -r migration/requirements-lock.txt
```

Then create the private dlt config directory and copy the example:

```sh
umask 077
install -d -m 700 migration/.dlt
cp -n migration/config.example.toml migration/.dlt/config.toml
```

Run `umask` and `install` before creating `migration/.dlt/secrets.toml`; keep that file owner-readable only. The copied config disables dlt telemetry for this project as described above.

The lock includes dlt's BigQuery, Snowflake, and DuckDB extras plus the PostgreSQL driver. The SQL loader currently accepts PostgreSQL via psycopg and existing immutable SQLite snapshots only; adding another driver will not enable it without reviewed code changes. Run `uv pip compile migration/requirements.txt --python-version 3.11 --universal --generate-hashes --output-file migration/requirements-lock.txt` after changing direct dependencies.

The generated dlt pipeline name combines `--pipeline-name`, destination, dataset, and a target hash. dlt also uses it as a schema name and limits it to 64 characters. Names that already fit are unchanged; longer names are compacted deterministically while retaining the prefix and hashing the full generated name. If the prefix itself makes the compact form too long, shorten `--pipeline-name`.

## Local synthetic source

Load the same retail sample as CSV files or create a disposable SQLite source database:

The bundled SQLite fixture declares `customers.created_at` and `orders.updated_at` as `TIMESTAMP`, and `orders.order_date` as `DATE`. Their sample values remain ISO timestamps (UTC values end in `Z`) and `YYYY-MM-DD` dates for SQLite/SQLAlchemy reflection and the Silver casts.

```sh
migration/.venv/bin/python migration/run_migration.py \
  --source csv --destination duckdb --dataset starterkit_migration_bronze \
  --csv-dir migration/examples/csv --dry-run

MIGRATION_DEMO_DIR="$(mktemp -d)"
chmod 700 "$MIGRATION_DEMO_DIR"
migration/.venv/bin/python migration/create_sqlite_demo.py "$MIGRATION_DEMO_DIR/starterkit-retail.sqlite"
export MIGRATION_SOURCE_URL="sqlite:///$MIGRATION_DEMO_DIR/starterkit-retail.sqlite"
migration/.venv/bin/python migration/run_migration.py \
  --source sql --destination duckdb --dataset starterkit_migration_bronze \
  --source-schema main --tables customers,products,orders,order_items --dry-run
```

The local DuckDB destination is for a synthetic walkthrough only. It does not deploy or write to either cloud edition. Replacements there require the exact canonical file path and table list printed by `--dry-run`; use those strings as `--confirm-target` and `--confirm-tables` in the load command. The CLI accepts only dlt's default `<pipeline_name>.duckdb` file inside its private per-pipeline directory and rejects symlinks and custom DuckDB connection settings.

For the synthetic SQLite-to-DuckDB walkthrough, keep `MIGRATION_SOURCE_URL` exported from the SQLite demo block above, run its `--dry-run` command, and copy the three confirmation values into the following local load. Only the dry run uses temporary dlt state. The load writes the DuckDB file and dlt state under the persistent `migration/.dlt/pipelines/` directory and a private run manifest under `migration/run-manifests/`:

```sh
migration/.venv/bin/python migration/run_migration.py \
  --source sql --destination duckdb --dataset starterkit_migration_bronze \
  --source-schema main --tables customers,products,orders,order_items \
  --confirm-target 'EXACT_TARGET_IDENTITY_FROM_DRY_RUN' \
  --confirm-tables 'EXACT_TABLE_LIST_FROM_DRY_RUN' \
  --confirm-schema-fingerprint 'EXACT_SCHEMA_CONFIRMATION_FINGERPRINT_FROM_DRY_RUN'
```

This performs a local synthetic load, not a cloud run. A successful load creates a private run manifest; the smoke fixture `scripts/tests/test_migration_clean_checkout_smoke.py` checks the dry-run and confirmed-load path in isolated temporary state.

## BigQuery destination

Pre-create three dedicated BigQuery datasets in the same location as the jobs: `starterkit_migration_bronze` for dlt raw tables, `starterkit_migration_silver` for Silver models, and `starterkit_migration_gold` for Gold marts. These are derived from the dbt target dataset `MIGRATION_DBT_DATASET` (default `starterkit_migration`) plus the custom schema names `silver` and `gold`; if you change the target dataset, pre-create its corresponding Silver and Gold datasets too. Keep Bronze separate from the dbt output datasets. Set the dlt `destination.bigquery.location` in `migration/.dlt/config.toml` to the same value as `BQ_LOCATION` and the three datasets; the example config defaults to `US` for TheLook.

Grant the loader identity BigQuery Job User (`roles/bigquery.jobUser`) on the project and BigQuery Data Editor (`roles/bigquery.dataEditor`) on Bronze. Grant the dbt migration runner Job User on the project, Data Viewer (`roles/bigquery.dataViewer`) on Bronze, and Data Editor on Silver and Gold. These roles let dlt load raw data and let dbt read Bronze and create/update models. Pre-creating datasets avoids granting either identity project-level dataset creation privileges. Separate identities are preferable; if one identity runs both workloads, combine these grants. Authenticate locally with ADC and set `BQ_PROJECT` plus `DESTINATION__BIGQUERY__PROJECT_ID` to the same project ID. Keep `BQ_LOCATION` and all three datasets in the same location, and keep `MIGRATION_SOURCE_URL` secure for SQL sources. For the full edition setup, see [BigQuery setup](../editions/Bigquery/docs/setup.md).

The `gcloud` command below is part of the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install); install it if you need to create local Application Default Credentials. If ADC is already configured through an approved environment, you do not need to run the login command.

```sh
gcloud auth application-default login
export BQ_PROJECT='YOUR_NONPROD_PROJECT_ID'
export DESTINATION__BIGQUERY__PROJECT_ID="$BQ_PROJECT"
migration/.venv/bin/python migration/run_migration.py \
  --source csv --destination bigquery --dataset starterkit_migration_bronze \
  --target-id "$BQ_PROJECT" --csv-dir migration/examples/csv --dry-run
```

Copy `target_identity`, `table_confirmation`, and `schema_confirmation_fingerprint` from the dry-run JSON into `--confirm-target`, `--confirm-tables`, and `--confirm-schema-fingerprint`. The CLI requires the `starterkit_migration_` namespace and replaces only the selected tables. Since dry-run does not check cloud namespace existence, the actual run checks that the dataset was pre-created before loading. Review the project, dataset, table selection, data classification, and estimated cost before running. This does not create a billing budget.

## Snowflake destination

Pre-create a dedicated database, small auto-suspending warehouse, and Bronze schema named `STARTERKIT_MIGRATION_BRONZE`. Give the loader role access to that database, warehouse, and schema plus the required table/stage/file-format creation privileges. Do not use an administrative role. Set credentials in `migration/.dlt/secrets.toml` (ignored by Git) or dlt's documented `DESTINATION__SNOWFLAKE__...` environment variables:

```toml
[destination.snowflake.credentials]
host = "YOUR_ACCOUNT_IDENTIFIER"
database = "YOUR_NONPROD_DATABASE"
username = "YOUR_LOADER_USER"
password = "SET_LOCALLY"
warehouse = "YOUR_AUTO_SUSPENDING_WAREHOUSE"
role = "YOUR_NONADMIN_LOADER_ROLE"
```

Also set `SNOWFLAKE_ACCOUNT` and `SNOWFLAKE_DATABASE` to the same account and database identifiers (these are required for dbt target binding). Run `--dry-run` first with `--destination snowflake --dataset starterkit_migration_bronze --target-id ACCOUNT/DATABASE`, then copy `target_identity`, `table_confirmation`, and `schema_confirmation_fingerprint` from its JSON output into `--confirm-target`, `--confirm-tables`, and `--confirm-schema-fingerprint`. The account/database values are identifiers, not credentials. The CLI compares them with effective dlt and dbt target settings before loading. Snowflake warehouse compute and storage charges apply. Do not copy real keys or passwords into the example file.

## Silver and Gold build

The sample Medallion models expect the canonical four-table field contract shown in the fixtures. Configure each edition's local dbt profile, then point its Bronze source variable at the loaded namespace:

- BigQuery: set `MIGRATION_BRONZE_NAMESPACE=starterkit_migration_bronze` and `MIGRATION_DBT_DATASET=starterkit_migration`.
- Snowflake: set `MIGRATION_BRONZE_NAMESPACE=STARTERKIT_MIGRATION_BRONZE` and `MIGRATION_DBT_SCHEMA=STARTERKIT_MIGRATION`.

From `editions/Bigquery/`, run:

```sh
dbt debug --target migration
dbt build --target migration --vars '{enable_migration_models: true}' --select tag:medallion
```

From `editions/Snowflake/kit/`, use the same commands. The explicit target and variable keep the optional models out of an ordinary `dbt build`. Silver models deduplicate by business key with deterministic field-value tie-breakers and standardize types/statuses; these tie-breakers do not prove business-correct latest-row semantics when source version fields are absent. **Cross-warehouse timestamp/date parity is not verified.** As a review inference, Snowflake timestamp-to-date and timestamp casts may depend on timestamp type and session timezone, while BigQuery can apply different conversion defaults. A timezone shift could change the ordering of tied or near-boundary timestamps, deduplication winners, or the date assigned to daily sales. Explicitly align UTC conversion/session behavior and test timezone-boundary fixtures in both warehouses before claiming parity; no warehouse verification has been performed. Gold models provide customer value and daily product sales. Run the declared dbt data tests, compare source-to-Bronze counts, and compare agreed metric totals. Do not expect raw Bronze and Silver row counts to match: Silver filters null keys and deduplicates. The source-only local key diagnostic does not compare Bronze to Silver; inspect that key equality manually before cutover. The sample template treats `returned` as an order-level status affecting all its items; adapt business semantics for each source.

## Limits and migration controls

This release performs explicit full loads; interruption recovery and safe reruns still need a destination-specific end-to-end demonstration. dlt tracks local pipeline state. The CLI writes a run manifest without credentials or row values, but table names and target identifiers can still be sensitive; keep the manifest and dlt state private. The optional count reconciliation compares application-counted CSV/SQL rows with dlt normalized/prepared counts and can query current Bronze table counts. A local source-key diagnostic exists for CSV/SQLite, but no source-to-Bronze/Silver key equality or business-metric cutover reconciliation exists. Schema mapping is opt-in and does not migrate CDC, delete propagation after a full load, or table-level constraints. Use a disposable target first, preserve the source unchanged, and reconcile remaining evidence before cutover. Do not cut over until an owner approves the evidence and rollback plan.
