# Snapshot source contract template

A reusable agreement for a source that delivers **full snapshots of one period at a time** (for example one file set per month), each with an export manifest. It states what a complete delivery is and what the consumer does when a delivery is late, corrected, duplicated or empty.

Everything here is a template with MOCK values. Nothing is wired into `run_migration.py`, the scaffold or any CI job, and nothing connects to a warehouse.

## Files

| File | Purpose |
|---|---|
| `snapshot-contract.v1.yaml` | The contract: period definition, delivery format, schema, rules and outcomes. |
| `export-manifest.v1.example.yaml` | Annotated manifest for a first delivery. |
| `mock/export-manifest.correction.v1.example.yaml` | Manifest for a late correction that replaces the same period. |
| `mock/export-manifest.empty-period.v1.example.yaml` | Manifest for a period with no rows. |
| `mock/MOCK_widget_readings_2026-01_seq{1,2}.csv` | The two tiny MOCK data files those manifests describe. |

## How to adapt it

1. Copy this directory to a private location. Do not commit an adapted copy with real source names or data to this repository.
2. In the contract, replace the values marked `ADAPT`, the schema and the control totals. Set `mock_only: false` and `status: draft`.
3. Agree the rules with the source owner, then set `status: agreed`. Leave the rule ids unchanged so reports and discussions can cite them.
4. Ask the source to write one manifest per snapshot in the example's shape.

## Versioning

- `contract_format_version` and `manifest_format_version` (both `1`, also in the file names) version the **shape** of these files. A breaking change to the shape gets a new number and new file names.
- `contract.revision` versions the **agreement**. Bump it on any change to the schema, period or rules. Every manifest names the contract id and revision it was exported under.

## Rules in brief

The contract holds the full wording. Each rule has an id and one outcome when it is broken: `reject_snapshot`, `hold_for_review` or `no_op`. A rejected or held snapshot never changes the data already loaded for its period.

**Completeness (C1 to C8).** The manifest is written last and is the only completion marker; data files without a `complete` manifest are never read. Listed files must match by size and SHA-256, with no unlisted file. Row counts must match per file, in total and against the count taken at the source. Every row must fall inside the period. Control totals must match exactly. Periods must be contiguous: a later period can be loaded, but nothing is reported as complete past the first missing period. A snapshot is all or nothing.

**Late corrections (L1 to L7).** Delivered snapshots are immutable. A correction is a new full snapshot of the same period with `sequence` one higher, `supersedes` naming the snapshot it replaces and a `correction_reason`. The highest accepted sequence wins and replaces the whole period in one step. Corrections are accepted on their own only inside the correction window; after that the period is closed and needs a recorded exception. A row that surfaces late goes into a correction of its own period, never into a later period's snapshot.

**Duplicates (D1 to D6).** The primary key is unique across a snapshot. One repeated key rejects the snapshot, even when the rows are identical; the consumer never picks a winner. Null keys are rejected. Redelivering the same `snapshot_id` with identical checksums changes nothing; the same id with different content, or a second id for the same period and sequence, is rejected. A key does not appear in two periods.

**Empty periods (E1 to E5).** An empty period is still delivered, as a manifest with `empty_period.declared: true`, a reason code, no files and zero counts. A missing manifest never means empty. Zero rows without the declaration is rejected, as is a declaration alongside files or non-zero counts. An empty correction that removes a previously non-empty period, or a long run of empty periods, is reviewed before it is applied.

## Out of scope for format version 1

Row-level deltas, change streams, snapshots spanning several periods, and any format other than headed UTF-8 CSV. The contract describes behaviour; this repository does not ship a loader that enforces it.

## Check the examples locally

From the repository root, with PyYAML available (it is in the migration lock files):

```sh
python3 - <<'PY'
import hashlib, pathlib, yaml
root = pathlib.Path("migration/templates/snapshot-contract")
for path in sorted(root.rglob("*.yaml")):
    doc = yaml.safe_load(path.read_text())
    for f in doc.get("files", []):
        data = (path.parent / f["name"]).read_bytes()
        assert len(data) == f["bytes"], f["name"]
        assert hashlib.sha256(data).hexdigest() == f["sha256"], f["name"]
    print("ok", path.relative_to(root))
PY
```
