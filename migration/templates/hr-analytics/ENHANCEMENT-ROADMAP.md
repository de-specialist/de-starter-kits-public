# HR analytics enhancement status

Scope: synthetic-only Fabric trial proof of concept. The implementation must not treat draft rules as production policy.

| # | Enhancement | Delivered foundation | Release status |
|---|---|---|---|
| 1 | Source-period completeness | Synthetic export manifest fixture loaded into Bronze; notebook preflight accepted explicit completeness evidence and reconciled declared row counts before writing Silver. | **Done for synthetic POC:** production source manifests still need producer controls or owner attestations. |
| 2 | Corrections and overlapping extracts | Natural-key duplicate collapse, conflicting-row quarantine, retained-history conflict detection, and source contract for extract lineage/revision precedence. | **Partial:** no approved source revision key or winner rule; conflicts stay rejected. |
| 3 | Effective-dated workforce history | Month-grain employee attributes and edge cases for monthly state changes. | **Partial:** monthly snapshots do not establish daily effective dates. |
| 4 | Hire and termination reconciliation | Hire event keys and repeated termination event deduplication are specified; invalid negative hire intervals are rejected. | **Synthetic proof only:** stable production event identity and rehire rules need owner approval. |
| 5 | Headcount and turnover reconciliation | Turnover formula and expected synthetic outputs are documented; incomplete adjacent snapshots return no rate. Notebook now collapses exact retained duplicates and quarantines conflicting natural keys. | **Pending redeploy/reconciliation:** live Oct 2032 query returned 2 termination events and turnover 0.666667 vs expected 1 and 0.333333. Schedule rows were also blocked. The correction is in template code only; rerun and compare actual outputs before closing. |
| 6 | Absence reporting gate | Absence category map and formula approval are required; leadership notebook omits absence measures. | **Blocked:** no approved entry-type map or validated denominator/report formula. |
| 7 | Disclosure suppression | Aggregate-only leadership model contract and differencing threat cases are documented. | **Blocked:** primary and complementary suppression are not implemented; no threshold assumed. |
| 8 | Audience access boundaries | Separate leadership and analyst model contracts; no access changes or inferred group IDs. | **Blocked for sharing:** named Entra groups and permission review are needed. |
| 9 | Semantic definitions and report periods | Metric catalog, draft DAX, report crosswalk, single-period snapshot rules, and model-compatible POC Gold schemas. | **Partial:** read-only query confirmed hiring KPIs and headcount match expected values; schedule KPIs are null with `blocked_source_rows_rejected`; analyst model query returned pseudonymous mock detail. Reports and filters remain unverified. |
| 10 | Orchestration, audit and monitoring | Notebook deployed and run once on demand; current-run reject summary and capacity/storage guidance. | **Partial:** no scheduled jobs; alerting, persistent audit/versioning, and billing telemetry are not configured. |

## Next safe execution

1. Investigate retained Silver conflicts for termination and schedule sources; do not revise expected outputs until the duplicate/conflict cause is known.
2. Reconcile the two reports and analyst detail against fixture expectations after that investigation.
3. Keep both reports internal to the synthetic proof of concept until suppression and access review are complete.

The public repo contains only authored mock fixtures. Workspace and Lakehouse identifiers are configured at runtime and are not committed. No recurring workload is configured.

The next 20 reusable template enhancements and their exact release status are tracked in [ENHANCEMENTS-11-30.md](ENHANCEMENTS-11-30.md). Their implementation artifacts do not change the existing business-approval gates.
