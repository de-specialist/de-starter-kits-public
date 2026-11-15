# Scaffolder input contract (item 145)

**Status: a plan-only item-145 scaffolder exists; write mode is not implemented.** This document defines which existing migration evidence the Silver starter-model scaffolder may trust, how that evidence must bind together, and the fixed refusal reason codes. It covers inputs only. Generated-file layout, edit preservation, and naming remain proposals; plan output never writes files.

The baseline migration contract was read at commit `3bdceb6`; the optional source-binding addition was reviewed at commit `161e2d8` (PR #7). Nothing was executed against a source or warehouse. "Present" means the loader writes the field when supplied; optional fields may be absent in legacy evidence, and the generator must not infer them.

## Trust model

- **Local, private, offline.** The generator reads only explicitly named local files. It never opens a source connection, reads `MIGRATION_SOURCE_URL` or any other credential variable, resolves dlt destination configuration, or queries a warehouse.
- **Fingerprints are integrity checks, not authentication.** All fingerprints are unkeyed SHA-256 values. Anyone who can edit an artifact can recompute them. A match proves that the artifacts are internally consistent and agree with each other. It does not prove which source produced them, who reviewed them, or that the source is unchanged. The only local trust boundary is the existing owner-only, no-symlink, single-link file rule in `_private_json.read_private_json`. No signature or attestation exists.
- **Fingerprints describe schema and mapping, not data.** They do not cover row values, constraints, or the dlt-inferred Bronze types, and they reveal schema equality. Treat them as private metadata.
- **Fail closed.** Any check below that fails stops the generator before it writes any file. It emits exactly one reason code from the [fixed list](#fixed-reason-codes) and no partial output.

## Input artifacts

| Role | Artifact | Required | How it is produced today |
|---|---|---|---|
| `mapping` | Reviewed mapping file (version 1 JSON) | Yes | Operator-authored; read by `--mapping-file` |
| `run_manifest` | `migration/run-manifests/<run_id>.json` | Yes | Written by a real load; status `completed` |
| `dry_run` | Saved `--dry-run` JSON | Yes | Printed to stdout only; the operator must save it as an owner-only file, as `schema_drift.py` already expects |

**Derived reports are not accepted as inputs:** `evidence-index-*.json`, `acceptance-report-*.json`, and the reconciliation reports. They intentionally drop fields that binding requires: the snapshot, the confirmation fingerprint, and normalized columns, and the acceptance report also drops target identity.

**Why all three are required:** each artifact lacks something the others provide:

- Only the mapping file holds the rules.
- Only the manifest holds a completion status, timestamps, SQL source schema name, and evidence that the schema matched at extraction.
- Only the dry run holds the normalized Bronze column names.

A dry-run-only bundle therefore fails with `scaffold_evidence_freshness_unavailable` (see the gaps section).

### Admission limits (all artifacts)

| Check | Limit | Source of limit |
|---|---|---|
| Mapping file | Regular, non-symlink final component; ≤ 1 MiB; UTF-8; duplicate JSON keys rejected | `_load_mapping_file`, `_MAX_MAPPING_FILE_BYTES` |
| Dry run and manifest | Current-user-owned, owner-only, single-link regular file; owner-only parent with no symlink components; ≤ 10 MiB; UTF-8 JSON object; duplicate keys rejected | `read_private_json`, `DEFAULT_MAX_BYTES` |
| Schema snapshot | ≤ 1,000 tables; ≤ 4,096 columns per table; names and type families ≤ 1,024 characters | `schema_drift._MAX_*` |
| Manifest entities | 1–1,000 safe identifiers, unique ignoring case | `evidence_index._validate_manifest` |
| Mapping source name | 1–255 characters, no control characters or newlines | `_MAX_MAPPING_SOURCE_NAME_LENGTH` |

The generator must use these existing readers and validators, not reimplement them. It must not raise any limit.

## Trusted fields

### `mapping`: mapping file

| Field | Status | Constraint |
|---|---|---|
| `version` | Present | Exactly integer `1` |
| `tables` | Present | Object of safe-identifier table names, unique ignoring case |
| `tables.<t>[].source` | Present | Exact raw source column name |
| `tables.<t>[].target` | Present | Safe identifier; unique within the table ignoring case |
| `tables.<t>[].cast` | Present | One of `string`, `integer`, `numeric`, `date`, `timestamp`, `boolean` |

The top-level keys must be exactly `version` and `tables`, and each rule must have exactly three keys. **Mapping fingerprint:** compute it with `mappings_fingerprint(load_mapping_file(path))`. That is SHA-256 over `json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`. Table keys are case-folded, and rules keep file order. An empty mapping hashes to `44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a` (`{}`); the generator refuses it.

### `dry_run`: saved dry-run report

| Field | Status | Use / constraint |
|---|---|---|
| `mode` | Present | Exactly `dry_run` |
| `source_binding_id` | Optional | If present, exact generic operator alias matching `[a-z][a-z0-9_]{0,39}`; never authentication. Final generation requires it and rejects missing values |
| `source` | Present | `csv` or `sql`: the source kind |
| `destination` | Present | `bigquery`, `snowflake`, or `duckdb` |
| `bronze_namespace` | Present | Starts with `starterkit_migration_` |
| `target_identity` | Present, **nullable** | Null when cloud `--target-id` is absent or resolution failed |
| `resolved_tables` | Present | Selected entities whose status is `ready` or `empty`; sorted ignoring case |
| `destination_tables` | Present | dlt-normalized Bronze names, index-paired with `resolved_tables` |
| `inventory[].table`, `inventory[].status` | Present | Every status must be `ready` or `empty` |
| `source_schema_snapshot`, `source_schema_snapshot_algorithm` | Present | Algorithm `table-ordered-column-name-type-family-v1` |
| `source_schema_fingerprint` (+ `_algorithm`) | Present | `sha256-table-and-ordered-column-names-v1` |
| `typed_schema_fingerprint` (+ `_algorithm`) | Present | `sha256-table-column-type-families-v1` |
| `source_mapping_fingerprint` | Present | 64 lowercase hex characters |
| `schema_confirmation_fingerprint`, `schema_confirmation_algorithm` | Present | `sha256-schema-and-mapping-v1` |
| `source_mapping_report.valid`, `.errors`, `.mappings[]` | Present | `valid: true`; `errors` empty; `mappings` equals the file rules |
| `normalized_identifier_safety.safe_to_extract` | Present, **nullable** | Must be `true`; null means dlt naming was unavailable |
| `normalized_identifier_safety.tables[].normalized_destination_table`, `.normalized_columns[]` | Present, **nullable** | The only recorded source-to-Bronze column-name map (includes mapped targets) |
| `destination_type_profile.destination`, `.name`, `.algorithm` | Present | Algorithm `destination-numeric-type-profile-v1`; destination must match |
| `schema_drift_review` | Present, **nullable** | If present, `fresh_confirmation_verified` must be `true` |
| `blockers` | Present | May contain only `target_not_verified`, `table_list_not_confirmed`, and `namespace_not_verified_by_config_resolving_dry_run`, because a config-only dry run always reports these |
| `writes_performed` | Present | Exactly `false` |

**Ignored, never copied into output:** `inventory[].columns[].type` (raw dialect type labels), `inventory[].row_count`, `retail_value_profile`, `retail_contract_compatibility`, `extraction_limits`, and `readiness_note`. Use only the snapshot's `type_family` values.

### `run_manifest`: completed run manifest

The manifest must first pass `evidence_index._validate_manifest`. That check validates the run ID and filename, status, kind, destination, namespace, pipeline name, entities, write disposition, source and target identity shape, and the SHA-256 fields. The manifest must then also satisfy the following:

| Field | Status | Constraint |
|---|---|---|
| `run_id` | Present | Canonical UUID equal to the filename |
| `source_binding_id` | Optional | If present, same validated generic operator alias as the dry run; final generation requires equality across dry run, completed manifest, and invocation |
| `status` | Present | Exactly `completed` |
| `source_kind` | Present | Equals `source_identity.kind` |
| `source_identity` (SQL) `.schema`, `.tables` | Present | `connection_identity: "redacted"`, `identity_scope: "descriptive_names_not_unique"` |
| `source_identity` (CSV) `.files[].entity` | Present | `identity_scope: "pre_extraction_file_metadata"`; `file_name` and `size_bytes` are read only for shape validation and never used or emitted |
| `entities`, `destination_tables` | Present | Same length; index-paired; destination names are already case-folded |
| `destination`, `bronze_namespace`, `target_identity`, `pipeline_name` | Present | Validated by `_safe_target_identity` |
| `write_disposition` | Present | Exactly `replace` |
| `started_at_utc`, `finished_at_utc` | Present | ISO-8601 with UTC offset; start ≤ finish ≤ generator clock |
| `source_schema_fingerprint_status`, `typed_schema_fingerprint_status` | Present | Both exactly `matched_at_extraction` |
| `source_schema_fingerprint_observed`, `typed_schema_fingerprint_observed` | Present | Equal to the expected fingerprints |
| Snapshot, fingerprints, algorithms, `schema_confirmation_fingerprint` | Present | Same values and algorithms as in the dry-run table |
| `source_mapping_fingerprint`, `source_mapping_rule_count` | Present | The rule count must equal the total number of mapping rules |
| `destination_type_profile` | Present | Must match the dry run |
| `destination_write_may_be_partial` | Present | Exactly `false` |
| `load_evidence_incomplete` | Optional; set only if load-evidence capture failed | Absent or `false` |

**Ignored, never copied into output:** `source_row_counts`, `accepted_extraction_progress`, `dlt_prepared_row_counts`, `load_ids`, `load_package_count`, `load_job_counts`, `extraction_reject_summary`, `extraction_limits`, `allow_empty_tables`, and `source_identity.note`.

## Binding rules

The generator checks these in order. All case-insensitive comparisons use Python `str.casefold()`. List order is not a binding property: the dry run sorts tables, while SQL manifest entities follow the `--tables` order. Pairs are compared as sets of `(entity, destination_table)` tuples.

1. **Internal integrity, applied to each JSON artifact separately.** Run `schema_drift.reviewed_schema_from_record`. It recomputes both snapshot fingerprints and the confirmation fingerprint, and checks the manifest's extraction-match fields. Any difference fails with `scaffold_fingerprint_recompute_mismatch`.
2. **Mapping fingerprint.** The value recomputed from the mapping file must equal `dry_run.source_mapping_fingerprint` and `run_manifest.source_mapping_fingerprint`. The dry run's `source_mapping_report.mappings` must equal the file's rules as a set of `(table, source, target, cast)` tuples, and the manifest rule count must match.
3. **Combined fingerprint.** `schema_confirmation_fingerprint` must be identical in the dry run and the manifest. Because this value covers names, type families, and the mapping, a difference means the two artifacts describe different reviewed states.
4. **Source kind.** `dry_run.source` = `run_manifest.source_kind` = `run_manifest.source_identity.kind`.
5. **Source binding.** Require `source_binding_id` in both artifacts and require exact equality with the value supplied to the scaffolder invocation. If absent, refuse with `scaffold_source_identity_unavailable`; if values differ, refuse with `scaffold_source_identity_mismatch`. The loader validates the alias and records it only when the operator supplies `--source-binding-id`; it does not compare it with a prior dry run. SQL schema/table names and CSV entity names remain descriptive only. This alias is an operator assertion, not authentication or proof of source identity. Never substitute matching names or fingerprints, and never read connection settings.
6. **Source schema.** For SQL, the manifest's `source_identity.schema` must be a safe identifier. The dry run does not record it, so the generator cannot cross-check it; that gap is listed below. For CSV, no schema exists, and the field must be absent. Shape equality is covered by step 3.
7. **Selected tables.** These sets must be equal, case-insensitively:
   - `dry_run.resolved_tables`
   - `run_manifest.entities`
   - the manifest's identity table or entity set
   - the snapshot table set
   - the `normalized_identifier_safety.tables[].table` set

   The `(entity, destination_table)` pair sets from the dry run and the manifest must also be equal. Every mapping table must be in the selected set. Every `normalized_destination_table` must equal its paired destination table, case-insensitively.
8. **Destination and namespace.**
   - `destination` must be equal in both artifacts.
   - `bronze_namespace` must be equal ignoring case and start with `starterkit_migration_`.
   - `dry_run.target_identity` must be non-null and exactly equal to `run_manifest.target_identity`, and its final path segment must equal the namespace ignoring case.
   - `destination_type_profile.destination` must equal `destination`.
   - The destination selects the edition: `bigquery` selects `editions/Bigquery/`, and `snowflake` selects `editions/Snowflake/kit/`. `duckdb` is a local rehearsal and does not select an edition, so the generator refuses it.
9. **Readiness.** Every `inventory[].status` must be `ready` or `empty`. `safe_to_extract` must be `true`, and `blockers` must contain only the three expected entries. Any `schema_drift_review` must be verified.
10. **Freshness.**
    - The manifest must be `completed` with both statuses `matched_at_extraction`.
    - `finished_at_utc` must be within an operator-supplied maximum age. No default exists, and a missing value fails.
    - A timestamp in the future fails.
    - The dry run has no timestamp, so the manifest timestamp is its only freshness anchor. Step 3 ties the dry run to that manifest's reviewed state.

## Fixed reason codes

The generator emits exactly one code, for the first check that fails. Checks run in this order:

1. Admission limits.
2. Field tables. A manifest status or extraction-match failure found here maps to `scaffold_evidence_run_not_completed` or `scaffold_evidence_schema_not_matched`, not to a recompute mismatch.
3. Binding steps 1–10.

Codes are stable, lowercase identifiers. Messages are fixed text and never interpolate names, paths, fingerprints, or values.

| Code | Category | Trigger |
|---|---|---|
| `scaffold_input_missing` | missing | A required artifact role was not supplied or does not exist |
| `scaffold_input_not_private` | malformed | The artifact fails the owner-only, no-symlink, single-link, or regular-file checks, or the platform cannot verify them |
| `scaffold_input_oversized` | oversized | An artifact exceeds 1 MiB (mapping) or 10 MiB (JSON), or snapshot, entity, or name bounds are exceeded |
| `scaffold_input_malformed` | malformed | The file is not UTF-8 or JSON, has duplicate keys, or is not an object |
| `scaffold_input_unsupported_version` | malformed | Mapping `version` ≠ 1, `mode` ≠ `dry_run`, or an algorithm label is not one listed here |
| `scaffold_field_missing` | missing | A field this contract marks required is absent, or is null where non-null is required |
| `scaffold_field_malformed` | malformed | A field fails its type, pattern, or enumerated-value check |
| `scaffold_mapping_invalid` | malformed | The loader's mapping parser rejects the file, the dry-run mapping report is invalid, or the mapping is empty |
| `scaffold_fingerprint_recompute_mismatch` | mismatched | Integrity step 1 fails |
| `scaffold_mapping_fingerprint_mismatch` | mismatched | Binding step 2 fails |
| `scaffold_schema_confirmation_mismatch` | mismatched | Binding step 3 fails |
| `scaffold_source_kind_mismatch` | mismatched | Binding step 4 fails |
| `scaffold_source_identity_unavailable` | missing | Either required artifact lacks `source_binding_id`; refuse final generation even when descriptive schema/table fields match |
| `scaffold_source_identity_mismatch` | mismatched | `source_binding_id` disagrees across the dry run, completed manifest, or scaffolder invocation (step 5) |
| `scaffold_source_schema_unavailable` | missing | The SQL source schema name is absent or unsafe in the manifest, or present for CSV (step 6) |
| `scaffold_selected_tables_mismatch` | mismatched | Binding step 7 fails |
| `scaffold_destination_mismatch` | mismatched | Destination or type-profile destination disagrees (step 8) |
| `scaffold_namespace_mismatch` | mismatched | The Bronze namespace or its prefix disagrees (step 8) |
| `scaffold_target_identity_mismatch` | mismatched | Target identity is null or unequal, or its namespace segment differs (step 8) |
| `scaffold_destination_edition_unbound` | mismatched | The destination is `duckdb` or otherwise does not select an edition |
| `scaffold_evidence_not_ready` | stale | A disallowed blocker, inventory status, unsafe identifiers, or an unverified drift review is present (step 9) |
| `scaffold_evidence_run_not_completed` | stale | Manifest status is not `completed`, or it may have partial writes or incomplete load evidence |
| `scaffold_evidence_schema_not_matched` | stale | Fingerprint statuses or observed values do not confirm a match at extraction |
| `scaffold_evidence_stale` | stale | `finished_at_utc` is older than the configured maximum age or is in the future |
| `scaffold_evidence_freshness_unavailable` | missing | No maximum age was configured, or no completed manifest is available to anchor freshness |

## Privacy rules

- **Never required as input:** credentials, `MIGRATION_SOURCE_URL` or any connection URL, dlt `secrets.toml`, dbt `profiles.yml`, row values, key values, value profiles, or snapshot/version markers.
- **Never emitted, in logs or generated assets:**
  - `target_identity`, including the project ID, Snowflake account/database, or DuckDB file path
  - `pipeline_name` and `run_id`
  - source directory, CSV file names, and file sizes
  - connection markers
  - row, byte, and reject counts
  - load IDs
  - raw dialect type labels
  - any artifact path outside the generated workspace
- **Logs:** contain only the reason code, artifact role (`mapping`, `dry_run`, or `run_manifest`), and an allowlisted field path from this document. Fingerprints, names, and exception text never appear.
- **Generated assets** may contain only:
  - mapped source and target column names and Bronze table names, because they are the model's subject
  - cast names
  - type families
  - the two integrity markers `source_mapping_fingerprint` and `schema_confirmation_fingerprint`, labeled as integrity checks, not provenance or authentication

  The Bronze namespace must be referenced through the edition's existing `MIGRATION_BRONZE_NAMESPACE` mechanism, not written as a literal. Generated assets still reveal source schema shape, so keep the generated workspace private.
- Fixtures for generator tests must be synthetic and labeled as such.

## Evidence gaps

These gaps exist in the current tracked code and must not be filled by inference:

1. **No item-145 generator or edition Silver models are tracked.** The contract proposal documents the planned boundary, but `scripts/scaffold_edition.py` and the edition `models/migration/` files that the migration README and `MIGRATION_BRONZE_NAMESPACE` docs describe are not tracked. The generator's output targets therefore cannot be verified against shipped files.
2. **The dry run is not persisted.** It is stdout only, with no `report_version`, `generated_at_utc`, `run_id`, or `pipeline_name`. Its age cannot be judged on its own.
3. **The dry run lacks the SQL source schema name.** Only the manifest's `source_identity.schema` records it, so step 6 is single-sourced.
4. **The optional source-binding value is not authentication.** PR #7 adds an operator-supplied alias to both artifacts when passed, but does not require it or compare it with an earlier dry run. The scaffolder must require it and enforce equality across artifacts and invocation. It correlates operator-selected evidence; any stronger proof requires a source-native snapshot/version contract.
5. **The manifest does not record normalized Bronze column names or the mapping rules.** Column names exist only in the dry run's `normalized_identifier_safety` (nullable). The manifest stores only the mapping fingerprint and rule count.
6. **Actual Bronze columns and types are unrecorded.** dlt type inference for unmapped CSV text and possible variant columns are not captured by any artifact. dlt lineage columns (`_dlt_load_id`, `_dlt_id`) are documented but not listed per table.
7. **There is no authenticity mechanism:** no signature, keyed MAC, or reviewer attestation. Mapping review status is explicitly `not_included_in_manifest`.
8. **There is no freshness policy.** No maximum evidence age is defined anywhere, and source snapshot/version evidence is deliberately kept out of manifests (`docs/source-snapshot-contract.md`).
9. **Snapshots include missing tables.** A missing table appears in `source_schema_snapshot` with zero columns, so the snapshot alone cannot distinguish missing from columnless tables. `inventory[].status` must be consulted.
10. **There is no edition binding for DuckDB.** The only end-to-end-validated loader path (local DuckDB) does not select a target edition.
11. **The temporal and destination-type contracts are not settled.** Backlog item 138 is open, and the backlog orders 145 after 138 and 139. Cast semantics in generated Silver SQL remain unverified in both warehouses.

Closing gaps 2, 3, and 5 would require loader changes with matching privacy tests. Until then, the generator may produce a plan-only report; final generation must still require `source_binding_id` and refuse when it is absent or mismatched.
