# Generic deployment and operations guide

This guide contains no tenant IDs, credentials, group IDs, or client data. It is an implementation checklist for a future isolated deployment; the current synthetic template has no scheduled workload.

## Environment separation

- Use separate Dev, Test, and Prod workspaces and service identities.
- Store tenant-specific IDs and secrets in the deployment environment or secret manager. Never commit them to this template.
- Restrict Bronze and Silver to data engineering. Keep employee detail in a separately permissioned analyst model and leadership outputs preaggregated.
- Validate workspace, Lakehouse, semantic model, SQL endpoint, and report permissions as separate surfaces.

## Run gates

1. Validate source headers, manifest evidence, source-period coverage, and row counts.
2. Stage all sources before any Silver writes. Quarantine malformed rows and conflicting natural keys.
3. Reconcile retained Silver history. Deduplicate exact normalized repeats; quarantine different rows with the same natural key until a source-owner revision rule is approved.
4. Build Gold and the quality scorecard. Keep metrics blocked when an input source has rejected rows.
5. Export semantic-model results and compare them with the synthetic acceptance file using `python3 reconcile_outputs.py <actual.csv>`.
6. Keep report publication disabled until metric, privacy, disclosure, access, and report-level acceptance gates have named approvals.

## Orchestration and recovery

The example configuration in `orchestration/fabric-job.example.yml` is manual and on demand. Do not enable recurrence until an owner approves the cadence, retry policy, incident route, and capacity budget. Contract failures are not transient and should not be retried automatically.

The notebook uses deterministic table names and replaces Gold outputs on rerun. Silver keeps historical rows and only admits rows that do not conflict with retained natural keys. The current-run reject table is replaced on each run; the run audit table is append-only. Preserve both tables under an approved retention policy before relying on them for production audit evidence.

## Monitoring

Track run status and duration, source completeness, schema warnings, accepted/rejected counts, Gold reconciliation status, capacity consumption, and OneLake storage. Azure billing or capacity metrics may lag; treat the platform billing view as authoritative for charges and keep trial quota separate from billable usage.

## Release checklist

- [ ] Metric catalog version and owner approval recorded.
- [ ] Absence codes and denominator approved, if absence measures are in scope.
- [ ] Source correction, rehire, and effective-date rules approved.
- [ ] Disclosure threshold and complementary suppression reviewed and independently validated.
- [ ] Entra groups and item-level permissions verified; no audience group inferred.
- [ ] All report acceptance cases reconciled against approved outputs.
- [ ] Schedule, alerts, retention, cost limits, and rollback owner approved.
