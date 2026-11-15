# SCD2 history template

A reusable dbt pattern for type 2 history: one row per version of an entity, valid over a half-open interval `[valid_from, valid_to)`. Copy it into an edition and rename the columns. All names and data here are MOCK examples.

**Status:** the logic is checked offline on SQLite against the mock fixtures. The SQL has not been run in BigQuery or Snowflake. Run `dbt build` in your warehouse before relying on it.

## Files

| Path | Use |
|---|---|
| `bigquery/scd2_history.sql` | History model in BigQuery dialect. |
| `snowflake/scd2_history.sql` | The same model in Snowflake dialect. Only the `scd2_key` expression differs. |
| `shared/snapshot_to_changes.sql` | Portable prep model: periodic full snapshots to change records, with tombstones. |
| `shared/assert_scd2_intervals.sql` | Singular test: no gaps, no overlaps, exactly one open interval per key. |
| `shared/assert_scd2_snapshot_grain.sql` | Singular test: snapshot-observed boundaries are real snapshot dates. |
| `shared/schema.example.yml` | Example generic tests. |
| `fixtures/` | MOCK inputs and expected history for both paths. |
| `check_template.py` | Offline check. Standard library only. |

## Adopt it

1. Copy one edition's `scd2_history.sql` to `models/marts/<your_history>.sql` in that edition. Do not copy both.
2. Build a staging model that meets the input contract below, and point the `ref()` at it.
3. Replace `customer_key`, `tier` and `region` everywhere, including the `lag()` lines and the `versions` filter. Every tracked attribute needs both, plus a correction fallback ordering term.
4. Copy the singular tests into `tests/`, merge `schema.example.yml` into `models/schema.yml`, and fix the `ref()` names.
5. If you load the fixtures as seeds, set `column_types` so dates and timestamps are not loaded as strings.

## Input contract

One row per change record. Do cleanup in staging, not in the history model.

| Column | Meaning |
|---|---|
| natural key | Not null. One or more columns. |
| tracked attributes | A new version starts when any of these changes. |
| `effective_at` | When the values took effect. Not null. One type and one time zone throughout. |
| `recorded_at` | When the record reached you. Breaks ties between corrections. Not null. |
| `is_deleted` | True for a tombstone. Null counts as false. |
| `effective_basis` | `source_effective` or `snapshot_observed`. See "Snapshots and grain". |
| `observation_grain` | `event` for a change log, otherwise the snapshot cadence: `day`, `week` or `month`. |

For a change log, staging adds the two labels as literals:

```sql
select
    customer_key, tier, region, effective_at, recorded_at, is_deleted,
    'source_effective' as effective_basis,
    'event' as observation_grain
from {{ ref('raw_mock_customer_changes') }}
```

## Natural key

- Use the business identifier, not a source row id. Row ids can be reissued after a reload or reused after a delete.
- Normalize the key in staging (trim, case, type) so the same entity always has the same key. Reject null and blank keys there.
- Keep tracked attributes out of the key. If an attribute is in the key, a change creates a new entity instead of a new version.
- For a composite key, list every key column in each `partition by` and in the `scd2_key` hash, with a separator between parts.
- If the source can re-key an entity, map old keys to new keys in staging. The template cannot join two keys into one history.
- `scd2_key` is a hash of the natural key and `valid_from`. It identifies a version and stays the same across rebuilds while that version's start does not move.

## Effective dates

- Intervals are half-open. `valid_to` equals the next version's `valid_from`. An as-of join needs no `between`:

  ```sql
  on f.customer_key = h.customer_key
  and f.event_at >= h.valid_from
  and (h.valid_to is null or f.event_at < h.valid_to)
  ```

- The open version has a null `valid_to`. If a tool needs a sentinel date, add it in a downstream view.
- `is_current` means "latest known version". With future-dated records it may not be in effect yet. Use the as-of predicate with today's date for "in effect now".
- `effective_at` must come from the source's business date. A load time is not an effective date. If load time is all you have, the history shows when you observed a change, so label it `snapshot_observed`.
- One version per key per `effective_at`. If an entity can change twice on one date, use a timestamp or add a source sequence to the ordering.
- Deleted versions stay as rows with `is_deleted = true`, so the interval chain has no gaps. Filter them out in consumers that want live entities only.

## Late corrections

The model is a full rebuild from all change records, so it restates history as currently known:

- **Same effective date, corrected value:** the row with the latest `recorded_at` wins. Staging must test for and reject ambiguous rows with the same key, `effective_at` and `recorded_at`; there is no business-defined winner. If invalid ties slip through, both editions use a defensive fallback: deletion first (`null` means false), then `tier`, `region`, `effective_basis` and `observation_grain` in descending order with nulls last. This covers every retained value; exact output duplicates are interchangeable. The fallback gives repeatable results under fixed warehouse string collation, not business-correct correction semantics or cross-warehouse collation parity. Extend it whenever adding retained columns.
- **Backdated record:** a record with an earlier `effective_at` that arrives late splits the interval it falls in.
- **Correction that undoes a change:** if the corrected values equal the previous version, the version disappears and its neighbours merge.
- **Retraction:** the template has no "this record never happened" flag. Remove retracted records in staging.

Consequences:

- `valid_to`, `is_current` and even the set of versions can change between runs. Do not treat the history as append-only, and expect `scd2_key` values to disappear after a correction.
- The history answers "what do we now believe was true on date X". It does not answer "what did we believe on date X". For that, keep the change records append-only and filter them on `recorded_at` before building.
- Keep the default `table` materialization unless size forces otherwise. A merge on `scd2_key` leaves orphan versions behind after a correction. If you go incremental, rebuild every version of each affected key: select all change records for keys that have a new `recorded_at`, then replace those keys' rows. This variant is not included and not tested here.

## Snapshots and grain

Use `shared/snapshot_to_changes.sql` when the source gives full periodic snapshots instead of change records. Its output is labelled `snapshot_observed`, and that label carries through to the history.

A snapshot shows the state on the snapshot date and nothing in between. For a monthly snapshot:

- `valid_from` is the date a value was first observed. The real change happened at an unknown time after the previous snapshot.
- A value that changed and changed back between two snapshots is not in the history at all.
- **Monthly snapshots do not give daily history.** Do not join the history to a daily date spine, fill forward to days, or report "value on the 15th". Compare at snapshot dates only, and show `observation_grain` wherever the history is exposed.
- `assert_scd2_snapshot_grain.sql` fails if a boundary is not a real snapshot date.
- A key missing from a snapshot becomes a tombstone on that snapshot date. Load only complete snapshots, or a partial load will look like mass deletion.

dbt's own `snapshot` command has the same limit: it records when dbt saw a change, at the cadence it runs. Use it when the source keeps only current state and you are starting history from today. Use this template when you already hold dated change records or stored snapshots.

## Local check

From the repository root:

```sh
python3 migration/templates/scd2-history/check_template.py
```

It runs both edition models on in-memory SQLite for both paths (change log and monthly snapshots), compares the result with `fixtures/expected_*.csv`, and confirms that each singular test catches a deliberately broken history. It stubs `config()` and `ref()` and rewrites the two dialect casts, so it checks logic, not warehouse syntax. CI runs this check, including tied corrections in reversed input orders for every fallback field and exact duplicates.

Fixture cases: an unchanged repeat record (`MOCK-C001`), a corrected effective date (`MOCK-C002`), a backdated record (`MOCK-C003`), delete and return (`MOCK-C004`), a correction that removes a version (`MOCK-C005`), and a key missing from one monthly snapshot (`MOCK-S002`).
