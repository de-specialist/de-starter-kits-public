# Source snapshot contract

This contract states what the migration loader enforces for CSV, SQLite, and PostgreSQL across inventory (including `--dry-run`) and extraction. It is aligned with `migration/run_migration.py` and the private run-manifest rules.

Evidence classes used below:

- **Loader-enforced:** checked or configured in `migration/run_migration.py` (and fail closed when stated).
- **Platform open-mode behavior:** follows the source engine's documented URI/session semantics once the loader sets those flags; not independently proven by hashing or version markers.
- **Operator process:** required for row-level consistency; not verified by the loader.

The loader performs full table replacement (`write_disposition="replace"`). It does not open a transactionally consistent multi-table row snapshot, and it does not prove that source rows stayed unchanged between inventory and load. Manifest fields named `source_schema_snapshot` / schema fingerprints are **schema and mapping identity only** (ordered names and, for SQL, normalized type families)—not a stable source-data version.

## Supported sources

### CSV

- **Loader-enforced:** UTF-8 headed CSV (BOM-tolerant via `utf-8-sig`), one entity per file. The filename stem becomes the Bronze table name.
- **Loader-enforced:** Inventory, extraction, and identity metadata use the same `discover_csv_files` policy.
- **Loader-enforced:** Before yield, extraction recomputes the name-level schema fingerprint from the opened header and stops with `source_schema_changed_since_preflight` if it differs from inventory. That check covers ordered headers only, not file bytes or row values.
- **Loader-enforced (identity only):** Manifest identity is labeled `pre_extraction_file_metadata`: filename and filesystem size captured before extraction. Sizes may change before the file is read; no content hash is recorded. Filenames and sizes are identifying metadata—keep manifests private.
- **Operator process:** Freeze approved input files for the run if byte- or row-level stability matters.

### SQLite (`mode=ro&immutable=1`)

- **Loader-enforced:** Only an existing regular database file is accepted (`sqlite` + `pysqlite`). In-memory sources and URL query overrides fail closed.
- **Loader-enforced:** The CLI opens the file through SQLite URI `mode=ro&immutable=1`, verifies `PRAGMA query_only=ON`, and applies a cooperative 60-second statement progress deadline (see `_SQL_STATEMENT_TIMEOUT_MS`). The same engine is reused for inventory, preflight, and dlt extraction.
- **Platform open-mode behavior:** Per SQLite URI rules, `immutable=1` tells SQLite to assume the file will not change and therefore not create WAL/SHM sidecars. That is an open-mode assumption, not a loader-verified content freeze or OS lock against external writers. Do not point it at a database that is changing during the run.
- **Operator process:** Treat the file as an operator-prepared copy: freeze or copy it before inventory, keep that copy stable through extraction, and record how it was produced outside the manifest. The loader does not compare file bytes or a source version marker.
- **Loader limit:** The progress handler may not interrupt a driver blocked in I/O; single-row materialization is not bounded by the statement deadline.

### PostgreSQL

- **Loader-enforced:** Only `postgresql+psycopg://` is accepted. Multi-host URLs and caller `options` / `connect_timeout` overrides fail closed.
- **Loader-enforced:** Connections set and verify `default_transaction_read_only=on`, a 60-second statement timeout, a 10-second lock timeout, and a 10-second libpq connect timeout. Use a least-privilege SELECT-only role and prefer base tables.
- **Loader-enforced:** Empty-source preflight is a bounded `SELECT 1 … LIMIT 1` per selected table, not a full `COUNT(*)`. Extraction row counts are application-counted yielded rows, not an independent proof of source size.
- **Loader-enforced:** PostgreSQL loads now require either the exported-snapshot guard or explicit `--acknowledge-unverified-source-snapshot`. Guarded runs hold one repeatable-read, read-only exported snapshot across inventory, preflight, reflection, and dlt extraction; failure to confirm stops before destination loading. Acknowledged unguarded runs are recorded as consistency-unverified.
- **Operator process:** Agree a maintenance window or platform-native snapshot/export with the source owner when consistency matters; verify that marker through the platform's own controls, then extract from that frozen source. Never place raw snapshot IDs in manifests.

## PostgreSQL snapshot guard (wired into the loader; warehouse execution remains unverified)

`migration/postgres_snapshot_guard.py` provides a fail-closed guard for PostgreSQL. The CLI exposes `--enable-postgres-snapshot-guard` and `--acknowledge-unverified-source-snapshot`; it refuses a real PostgreSQL load unless one is selected. Guarded runs record sanitized source consistency evidence in the private run manifest. The guard has offline tests, but no live PostgreSQL or warehouse run has verified this integration.

### Research findings (pinned clients: psycopg 3.3.6, SQLAlchemy 2.1.6, dlt 1.32.0)

| Finding | Evidence class |
|---|---|
| A `REPEATABLE READ` transaction sees one snapshot taken at its first query. `READ COMMITTED` takes a new snapshot per statement. | PostgreSQL documented guarantee |
| `pg_export_snapshot()` returns a text identifier. Another session can import it with `SET TRANSACTION SNAPSHOT '<id>'` only while the exporting transaction is still open. The import must be the first statement in a `REPEATABLE READ` or `SERIALIZABLE` transaction. The identifier must be a string literal. | PostgreSQL documented guarantee |
| `DEFERRABLE` has no effect unless the transaction is also `SERIALIZABLE READ ONLY`. A `SERIALIZABLE` importer also needs a `SERIALIZABLE` exporter. The guard uses `REPEATABLE READ, READ ONLY` and does not use `DEFERRABLE`. | PostgreSQL documented guarantee |
| `pg_current_snapshot()` is available from PostgreSQL 13. The older `txid_current_snapshot()` path is out of scope, and older servers are `unsupported`. | PostgreSQL documented guarantee |
| An importing transaction is expected to report the same `pg_current_snapshot()` text as the exporter. The guard compares keyed digests of those values. If they differ, it fails closed as `changed`, so a mistaken match can block a load but cannot authorize one. | Implementation inference |
| psycopg 3 binds parameters on the server, which `SET` does not accept. The guard therefore checks the identifier against a strict hex-and-hyphen pattern before writing it as a literal. Any other shape is `unsupported`. The identifier format is not documented. | Library documented behaviour + implementation choice |
| The SQLAlchemy psycopg `pool_pre_ping` check runs in autocommit and does not open a transaction. SQLAlchemy fires the `begin` event before the first statement in each transaction. | Pinned-library source inspection |
| dlt `sql_database` opens a new `engine.connect()` for each table and exposes `engine_adapter_callback`. Every extraction transaction can therefore be bound through a `begin` hook. | Pinned-library source inspection |
| Snapshot export on hot standbys, poolers that do not keep the exporting session open (statement/transaction pooling across the run), and managed-service restrictions are not handled. Standbys (`pg_is_in_recovery()`) are `unsupported`. Pooler behaviour cannot be detected and is covered by the operator gate. | Unresolved; fail closed or operator gate |

### Guard lifecycle

1. `open(coordinator)` uses a dedicated DBAPI connection that stays outside the pool for the whole run. It rolls back any implicit transaction, then runs `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY`. It checks that the server is PostgreSQL 13 or later and not in recovery, checks the isolation level and read-only mode, and exports the snapshot.
2. `attach(engine)` registers a SQLAlchemy `begin` hook. It accepts only `postgresql+psycopg`. Each new transaction rolls back implicit state, sets `REPEATABLE READ, READ ONLY`, imports the snapshot, and checks the isolation level, read-only mode, and snapshot digest. `attach` returns the engine, so it can also be used as dlt's `engine_adapter_callback`.
3. `confirm()` runs after extraction finishes and before any destination load. It requires at least one bound transaction, then rechecks the coordinator transaction. Only after that check does the state become `verified`. No more transactions can bind after this point.
   The coordinator transaction stays open for the whole run. `idle_in_transaction_session_timeout` or a managed-service session limit can end it; that fails closed as `coordinator_lost`, so size those limits for the extraction time.
4. `close()` rolls back and closes the coordinator. It is idempotent and suppresses driver errors. If the guard closes before `confirm()`, the state is `interrupted`.

| State | Meaning | `consistency_ready` |
|---|---|---|
| `unverified` | The guard is not open yet, or is open but not confirmed. This is the default. | false |
| `verified` | Every bound transaction used the exported snapshot, and the coordinator still held it after extraction. | true |
| `changed` | A transaction or the coordinator saw a different snapshot. | false |
| `unsupported` | The engine, server version, standby, a participant transaction's autocommit/isolation mode, or the identifier format is not supported. | false |
| `interrupted` | Setup (including a coordinator that is not in `REPEATABLE READ, READ ONLY`), import, or confirmation failed; the coordinator was lost; the run was interrupted (including `KeyboardInterrupt`); or the guard closed before confirmation. | false |

Failure states persist: once a guard fails, later `bind`/`confirm` calls are refused. `consistency_ready(record)` returns true only for well-formed, allowlisted evidence that is `verified` and has at least one bound transaction. Missing, malformed, or extra-field evidence is not ready. Use `unsupported_source_evidence()` for CSV, SQLite, and other sources.

**Privacy:** The raw identifier and `pg_current_snapshot()` text stay in process memory and are cleared on confirm, failure, and close. They never appear in `repr`, evidence, or error messages. Comparisons use HMAC-SHA256 with a random key created for each guard, and the digests are not persisted. Driver exceptions, which can echo the identifier, are replaced with fixed-message `SnapshotGuardError` codes raised after the handler, so no driver exception is chained as `__cause__` or `__context__`. The raw identifier is also cleared from local variables before an error propagates, so locals-capturing error reporters do not record it.

### Loader integration in `migration/run_migration.py`

0. Before pipeline state or a manifest is created for a real load, `require_operator_gate` checks the SQL backend and operator choice. PostgreSQL without the guard requires `--acknowledge-unverified-source-snapshot`; the flag is rejected for CSV, SQLite, and guarded runs. Acknowledged runs record `unsupported_source_evidence(acknowledged=True)`, which is never consistency-ready.
1. For `postgresql+psycopg` sources, create the guard and call `open()` with `engine.raw_connection()` before the first inventory query. Call `attach(engine)` before inventory and preflight, so those steps also read the snapshot.
2. Pass `engine_adapter_callback=guard.attach` (or keep the engine already attached) to `sql_database` in `make_sql_source`. Keep dlt's `sqlalchemy` backend: backends that open their own connections (for example `connectorx`) and any other `raw_connection()` reads bypass the `begin` hook. `confirm()` only proves that at least one transaction bound, not that every read did.
3. Call `guard.confirm()` after dlt extraction and before normalize/load. If it raises, stop with a fixed reason code and do not load anything.
4. Call `guard.close()` in a `finally` that covers dry-run, preflight failure, extraction failure, and success.
5. Merge `guard.evidence()` (or `unsupported_source_evidence()` for CSV and SQLite) into the dry-run report and run manifest. Make readiness (`ready_to_load` / acceptance report) also require `consistency_ready(...)`.
6. The existing `connect` listener runs a `SELECT` on each new connection. The guard rolls this back before importing. The migration-core slice should still end that listener's transaction explicitly, so the rollback is not hidden.

Until these hooks land and are reviewed, PostgreSQL consistency stays `unverified`, and readiness must not claim it.

### Operator gate (always required for PostgreSQL today; still required after integration for unsupported cases)

Get the source owner to agree to one of these before running:

- **(a)** A direct (non-pooled, or session-pooled) primary connection on PostgreSQL 13 or later, so the guard can reach `verified`.
- **(b)** A maintenance window with no concurrent writes.
- **(c)** A platform-native snapshot or restore that is checked through that platform's own controls.

Record the decision in the restricted operational record described below. If the guard reports anything other than `verified`, do not load. Either fix the cause, or choose (b) or (c) and record that choice.

## Inventory vs extraction

| Claim | CSV | SQLite | PostgreSQL | Evidence class |
|---|---|---|---|---|
| Schema fingerprint binding (names; SQL also typed families) | Yes; rechecked on open header | Yes; rechecked on reflected metadata before dlt extract | Yes; rechecked on reflected metadata before dlt extract | Loader-enforced |
| Row-level / byte-level freeze across inventory → extract | No | No; operator must keep the opened file unchanged (not verified) | No | Operator process / absent |
| Manifest proves exact source version loaded | No | No | No | Absent |

Schema and mapping fingerprints authorize a reviewed shape; they are not anonymization and do not cover constraints or data values. For SQL, fingerprint recheck pins the reflected column set passed into dlt; it still does not freeze row values for the subsequent `SELECT`. SQL inventory may incur source query cost for the existence probe; CSV inventory scans files for exact row counts in dry-run reports (real-load inventory skips CSV row counting).

## Source mutation decision matrix

| Source | Mutation the loader can detect | Mutation it cannot reliably detect | Snapshot claim and operator decision |
|---|---|---|---|
| CSV | An ordered header change visible when extraction opens a file fails closed against the inventory schema fingerprint. | Row edits with the same header; replacement or edits after the pre-extraction filename/size metadata is captured; and changes while an opened file is being read. File size is not a content fingerprint. | No byte- or row-level freeze is loader-guaranteed. Freeze the approved input files for the run, or record that concurrent changes were accepted. |
| SQLite | A schema change visible during reflected-metadata recheck fails closed against the inventory fingerprint. Read-only + `query_only` prevent writes on this connection; `immutable=1` avoids WAL/SHM sidecars under SQLite's assumption that the file is static. | A changed database file or row contents are not compared against a saved content/version marker. Immutable mode assumes the file will not change externally; it does not prove that assumption. | No loader-verified row snapshot. Treat a stable, operator-prepared copy as the operational snapshot; freeze it from inventory through extraction and record how it was produced outside manifests. |
| PostgreSQL | A schema change visible during reflected-metadata recheck fails closed against the inventory fingerprint. | Row inserts, updates, or deletes between inventory, existence preflight, and extraction. Read-only settings and statement/lock timeouts do not create one repeatable snapshot across those steps. | No transactionally consistent source snapshot is loader-guaranteed. Arrange a maintenance window or use a platform-native snapshot/export and verify it through that platform's controls. |

Choose the row that matches the source and record the operator decision in a restricted operational record. None of these checks proves that a manifest corresponds to an exact set of loaded row values. Keep snapshot identifiers and source values out of the manifest.

## Operator records (outside manifests)

Retain in a restricted operational record (ticket, change log, or source-platform audit)—not in migration manifests:

- Snapshot or export identifier / restore point when the platform provides one, plus who verified it and when.
- Maintenance-window agreement when no reliable marker exists, including owner, window, and concurrent-write expectation.
- For SQLite: how the read-only file copy was produced (copy, backup restore) and the path handled under local secret/path policy.
- For CSV: which directory/glob was approved and whether files were frozen for the run.
- Post-run notes that inventory/extraction ran against that agreed boundary (or that concurrent change risk was accepted).

## Must not enter manifests

Private run manifests, reconciliation reports, evidence indexes, and acceptance reports must not contain:

- Credentials, passwords, tokens, or connection URLs (`MIGRATION_SOURCE_URL` stays in the environment; SQL `connection_identity` is always `redacted`).
- Raw platform snapshot, backup, or export identifiers.
- Local absolute source paths (CSV identity stores file names and sizes only; SQL stores schema/table names under `descriptive_names_not_unique`).
- Row values, rejected payload contents, or exception text that could echo source data (failure records keep table, ordinal when available, reason code, and accepted counts only).

Manifests may still hold identifying operational metadata (entity names, pre-extraction sizes, schema/mapping fingerprints, target/namespace identifiers). Keep the `migration/run-manifests/` tree private and owner-only.
