# Idempotent incremental-ingest template

A small, generic pattern for loading changed rows repeatedly without duplicates or silent loss. It is a starting point to adapt, not a finished pipeline.

**What this is not.** It is not a warehouse or database connector, and nothing here is installed, scheduled or wired into the editions' dbt projects or the full-load migration CLI. The reference runner uses the Python standard library only. It reads local CSV files and writes one local SQLite file. It opens no network connection and reads no credentials.

## Contents

| Path | Purpose |
|---|---|
| `ingest.py` | Reference runner and importable `ingest()` function |
| `manifest.example.json` | Example stream manifest (the only configuration) |
| `examples/mock/` | Three synthetic `MOCK_` batch files, authored for this template |
| `tests/` | Offline tests for every behaviour described below |

All example rows are invented. Identifiers carry a `MOCK-` prefix and describe no real person, order or organisation.

## Quick start

Run from this directory with Python 3.11 or later. Keep the state file outside the repository.

```sh
python3 ingest.py --manifest manifest.example.json --validate-only

STATE="$(mktemp -d)/state.sqlite"
python3 ingest.py --manifest manifest.example.json --input-dir examples/mock --state-db "$STATE"

# Run it again: every file reports skipped_already_applied and nothing changes.
python3 ingest.py --manifest manifest.example.json --input-dir examples/mock --state-db "$STATE"

python3 -m unittest discover -s tests -v
```

The runner prints a JSON summary with per-file counts. Exit codes: `0` success, `1` stopped run, `2` invalid manifest or arguments.

## What the mock batches show

| File | Row | Outcome |
|---|---|---|
| `batch_001` | three orders | inserted; watermark becomes `2025-03-02T08:15Z` |
| `batch_002` | `MOCK-O-0001` with a newer `updated_at` | updated |
| `batch_002` | `MOCK-O-0002` repeated unchanged | counted `unchanged`, no write |
| `batch_002` | `MOCK-O-0003` with an older `updated_at` | `stale_ignored`; the newer stored version wins |
| `batch_002` | `MOCK-O-0004` dated before the watermark, inside the lookback | inserted as a late arrival |
| `batch_003` | new `mock_channel` column | added as a nullable column (`on_new_column: add`) |
| `batch_003` | `MOCK-O-0007` dated before the lookback window | quarantined until a backfill covers it |

After one run the target holds six orders and one quarantined row. This backfill applies the seventh and clears the quarantine:

```sh
python3 ingest.py --manifest manifest.example.json --input-dir examples/mock --state-db "$STATE" \
  --mode backfill --from 2025-02-27T00:00:00Z --to 2025-02-28T00:00:00Z
```

## Manifest

One object per stream in `streams`. Unknown policies and malformed values are rejected before any file is read.

| Field | Default | Meaning |
|---|---|---|
| `name` | required | Lowercase `snake_case`. The target table is `tgt_<name>`. |
| `file_glob` | required | Bare file-name pattern inside `--input-dir`. Path separators and `..` are rejected. |
| `primary_key` | required | One or more declared, non-nullable columns that identify a row. |
| `cursor_column` | required | A non-nullable `timestamp` column that increases whenever the row changes. |
| `lookback_seconds` | `0` | How far behind the watermark a row may be and still load in an incremental run. |
| `late_arrival_policy` | `quarantine` | `quarantine` or `apply` for rows older than the lookback window. |
| `schema_drift.on_new_column` | `fail` | `fail`, `add` or `ignore` for a column not in the manifest. |
| `max_rejected_rows_per_file` | `0` | Invalid rows tolerated per file before the file fails. |
| `max_future_skew_seconds` | `300` | A cursor further ahead of the clock than this is rejected. |
| `columns` | required | `name`, `type` (`string`, `integer`, `decimal`, `date`, `timestamp`) and `nullable`. |

Timestamps must be ISO-8601 with a UTC offset (`Z` or `+hh:mm`). Values without an offset are rejected rather than guessed. An empty cell is a null.

`fixtures/timestamp_parity.csv` pins offset normalization across a repeated
daylight-saving hour, equivalent instants with different offsets, and a UTC
date rollover. The offline contract test checks normalized ordering and UTC
business dates. This does not prove BigQuery or Snowflake session-timezone
behavior; run adapter-specific acceptance tests before claiming cross-warehouse
parity.

## Behaviour contract

### Watermarking

Each stream has one high watermark: the largest cursor value committed so far. It is stored in the same transaction as the rows it describes, so it can never run ahead of the data. It only moves forward. A row with a cursor too far in the future is rejected, because accepting it would push the watermark past rows that have not arrived yet.

In this file-based reference the watermark classifies rows as on time or late. When you adapt the pattern to a query-based source, use `watermark - lookback_seconds` as the lower bound of the extraction query and keep the same merge rule.

### Idempotency

Three mechanisms make a repeated run a no-op:

1. **File ledger.** Each applied file is recorded with its SHA-256. An incremental run skips a file whose name and hash are already recorded.
2. **Key-wise newest-wins merge.** A row replaces the stored row for its key only if its `(cursor, row hash)` pair is greater. An identical row writes nothing, and lineage columns change only when the row does.
3. **Deterministic tie-break.** Two different rows with the same key and the same cursor are settled by row hash, never by arrival order.

Because the merge does not depend on order, loading the same files in any order with `--mode replay` yields the same target. Tests assert this.

### Replay and backfill

| Mode | Reads | Lateness check | Ledger and watermark |
|---|---|---|---|
| `incremental` | files not yet in the ledger | yes | updated |
| `replay` | every matching file | no | updated; watermark still only moves forward |
| `backfill` | every matching file, rows with `from <= cursor < to` | no | untouched |

Use `replay` to rebuild or repair from retained files. Use `backfill` to load a bounded historical window without disturbing the incremental position. Both go through the same merge, so both are safe to repeat. A replay or backfill that applies a row previously quarantined as late removes that quarantine entry.

### Late arrivals

A row dated before the watermark is late. Inside the lookback window it is merged normally and counted as `late_within_lookback`. Older than the window, it follows `late_arrival_policy`: `quarantine` holds it in `_ingest_quarantine` for review and backfill, and `apply` merges it straight away. Choose a lookback from how late your source actually delivers. A larger window costs more re-reading in a query-based source and nothing in correctness.

### Schema drift

The header of every file is compared with the manifest before any row is applied.

| Change | Result |
|---|---|
| New column | Follows `on_new_column`. `add` creates a nullable text column, `ignore` drops the values, `fail` stops the file. |
| Missing nullable column | Loaded as null and recorded. |
| Missing key, cursor or other required column | File fails. |
| Duplicate or empty header name | File fails. |
| Value that does not match the declared type | Row is rejected (see Failures). |

Every drift event is recorded once in `_ingest_schema_history`. Row hashes cover non-null values only, so adding a nullable column does not change the hash of rows loaded before it existed. Type changes and renames are never applied automatically: a rename appears as one missing and one new column, and needs a manifest change and a replay.

### Checkpoints

Each file is one transaction containing its merged rows, quarantine entries, drift records, ledger entry and watermark. A file is either fully applied or leaves no trace. A run that stops part-way keeps earlier files committed, and the next incremental run resumes at the first file not in the ledger. Runs are recorded in `_ingest_runs` as `running`, `succeeded` or `failed`.

### Failures

The runner stops rather than guess. Error messages name the file, line and rule, and never include row values.

| Condition | Result |
|---|---|
| Invalid rows above `max_rejected_rows_per_file` | File rolls back; run fails. |
| Invalid rows within the threshold | Row goes to `_ingest_quarantine` with a reason; the rest of the file loads. |
| A file already in the ledger whose content changed | Run fails. Publish a new batch file, or run `--mode replay` once you have confirmed the change. |
| Undeclared column under `fail`, or a missing required column | File rolls back; run fails. |
| File that is not UTF-8, or is empty | Run fails. |
| Crash or interruption mid-file | That file's transaction rolls back. Rerun the same command. |

A run left as `running` in `_ingest_runs` was interrupted before it could record an outcome. It needs no cleanup.

## State tables

| Table | Holds |
|---|---|
| `tgt_<stream>` | Current row per key, plus `_cursor_value`, `_row_hash`, `_source_file`, `_applied_run_id` |
| `_ingest_checkpoint` | High watermark per stream |
| `_ingest_files` | Applied file names and hashes |
| `_ingest_schema_history` | Drift events |
| `_ingest_quarantine` | Rejected and late rows, with the raw row |
| `_ingest_runs` | Run status and error type |

## Limits

- **Hard deletes are not captured.** A row removed at the source stays in the target. Use a soft-delete column or periodic reconciliation.
- **The cursor must be trustworthy.** A row that changes without its cursor increasing at the source is seen only if its content differs at the same cursor value, and is then settled by hash.
- **Lateness depends on file order.** Files load in name order, so name batches so that they sort chronologically. Replay target state does not depend on order; incremental target state can differ when late rows are quarantined.
- **Quarantine stores raw rows.** Protect the state file as you would the target data.
- **Values are stored as canonical text.** Cast in the staging layer of your warehouse project.
- **Each file is read fully into memory** and merged row by row. That suits a reference, not large batches. In a warehouse, load each batch to a staging table and apply the same rule as one set-based merge.
- **One writer at a time.** The per-file write lock keeps concurrent runs correct, but they repeat work.

## Adapting it

1. Copy this directory, replace the manifest with your own streams and remove the mock files.
2. Keep credentials and connection settings in environment variables or a secret manager. Do not add them to the manifest.
3. Keep the contract when you swap the storage: state and data commit together, the merge is key-wise newest-wins with a deterministic tie-break, and the watermark only moves forward.
4. Extend the tests with synthetic fixtures for your own edge cases before pointing the pattern at real data.
