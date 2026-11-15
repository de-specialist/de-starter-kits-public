# Follow-on enhancement delivery status

Scope is synthetic, generic, and tenant neutral. “Delivered” means a reusable template artifact exists; it does not imply business approval or production readiness.

| # | Enhancement | Artifact or implementation | State / gate |
|---|---|---|---|
| 1 | Silver history reconciliation | Notebook collapses exact normalized duplicates and quarantines retained-key conflicts. | Implemented; conflicts intentionally remain unresolved. |
| 2 | Source correction policy | Insert-only default, optional explicit replacement flag, and conflict quarantine. | Partial; winner precedence requires source-owner approval. |
| 3 | Reconciliation ledger | Notebook appends per-run outcome and source counts. | Implemented for successful runs; platform-level failure alerting remains. |
| 4 | Rerun safety | Deterministic Gold overwrite; Silver natural-key merge; duplicate legacy rows collapsed. | Partial; atomic cross-table publication is not guaranteed by this template. |
| 5 | Schema drift handling | Required contract fields fail fast; optional strict mode rejects extra columns. | Implemented; validate with each producer export. |
| 6 | Freshness/completeness status | Gold quality output reports manifest periods and row-count agreement. | Implemented for manifest-backed snapshots; event freshness needs source SLAs. |
| 7 | Data quality scorecard | Source counts, rejected counts, and snapshot completeness are materialized. | Implemented; quality thresholds are not assumed. |
| 8 | Fixture-to-Gold reconciliation | `reconcile_outputs.py` compares actual CSV exports to authored expected KPIs. | Implemented locally; live Fabric output still needs reconciliation. |
| 9 | Effective-dated workforce model | Workforce contract documents monthly snapshots vs effective-dated attributes. | Design only; needs effective-dated source history. |
| 10 | Rehire/multiple employment | Episode-grain identity and explicit owner gate documented. | Design only; event identities and rehire policy need approval. |
| 11 | Absence mapping | Versioned mapping template with unmapped-code release gate. | Design only; no codes or rate definition invented. |
| 12 | Recruitment funnel | Candidate-stage grain and required source fields documented. | Blocked until candidate-stage source exists. |
| 13 | Workforce movement bridge | Movement categories and transfer limitation documented. | Partial; monthly snapshots can show joins/exits only, not causal transfers. |
| 14 | Metric versioning | Versioned catalog and change-control metadata. | Implemented as a governance contract; historical recalculation policy needs owner approval. |
| 15 | Period-aware measures | Single-snapshot safeguards and draft period rules retained in semantic measures. | Partial; report filter behavior remains unverified. |
| 16 | Report acceptance criteria | Synthetic acceptance cases and report metric crosswalk. | Implemented as a checklist; report outputs need reconciliation. |
| 17 | Disclosure control | Fail-closed policy template requires primary and complementary suppression. | Blocked; threshold and suppression algorithm need approval and independent validation. |
| 18 | Role and row-access design | Example access boundary contract with no inferred group IDs. | Design only; actual groups and item permissions require review. |
| 19 | Configurable orchestration | Manual Fabric job configuration with parameters and retry guidance. | Design only; no schedule or alert is enabled. |
| 20 | Deployment and monitoring | Tenant-neutral deployment checklist for environments, audit, capacity, and storage. | Implemented as guidance; real telemetry depends on deployment integration. |

## Next release gate

Resolve the live synthetic Silver-history conflict, rerun the notebook, export Gold, and reconcile it with `fixtures/expected/monthly_hr_kpis.csv`. Do not publish reports until the suppression, access, and report acceptance gates are satisfied.
