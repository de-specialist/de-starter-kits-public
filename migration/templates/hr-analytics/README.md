# Generic HR analytics migration template

**Status: design template / mock proof only.** Contracts, proposed report views, and fixtures are newly authored public examples. They illustrate generic HR migration patterns using synthetic data.

## Example target

- Audience: HR leadership and analysts.
- Report coverage: Workforce / HR Analytics, Talent Acquisition, Turnover, and Absenteeism.
- History: retain monthly snapshots for trends.
- Access: leadership receives aggregate-only outputs; approved HR analysts may receive pseudonymous employee detail in a separately permissioned model.
- Attribute policy: restricted or unapproved attributes are excluded.
- Initial metric rules: turnover is terminations divided by average month-start and month-end active headcount; time-to-hire is requisition-created date to employee start date; schedule variance is actual minus scheduled hours.

The template defines narrow generic business inputs. See [canonical contract](source-contract.yml), [example mappings](source-mapping.example.yml), [proposed report views](report-crosswalk.yml), [semantic model](semantic-model.yml), and [mock fixtures](fixtures/input/). Deployment adapters must establish their own approved export contracts.

## Proposed Fabric architecture (design only)

Use a **medallion flow with separate security boundaries** for the real HR data product:

1. **HR Staging (restricted):** Bronze stores immutable source files / source-aligned tables; Silver standardizes dates, codes, snapshot identifiers and pseudonymous employee keys. Keep row-level employee data, the re-identification key, and any raw identifiers here. Only the ingestion and HR data engineering groups receive access.
2. **HR Analyst Detail:** employee-grain Gold facts and the pseudonymous analyst model remain restricted to the approved HR analyst group.
3. **HR Leadership:** publish pre-aggregated, approved monthly Gold outputs through a leadership-only model. Do not expose employee keys, employee-grain facts, or exact event dates. Implement and independently test primary and complementary disclosure controls before release. A DAX measure or hidden column is not a security boundary.

For a hypothetical synthetic proof of concept, keep the footprint small: one isolated non-production workspace with a Warehouse using `bronze`, `silver`, and `gold` schemas, synthetic fixtures only, and no business-user access. A real-data deployment needs separate restricted staging, analyst-detail, and leadership-aggregate permission boundaries where the audiences differ. Confirm actual Fabric item/SQL endpoint permissions with the tenant before relying on workspace roles as the sole control. Use an isolated environment for any future implementation. Before any real data load, confirm data-owner authorization, tenant controls, location, retention, and access groups. See [Fabric workspace deployment patterns](https://learn.microsoft.com/en-us/fabric/fundamentals/understand-best-practices-fabric-cicd) and the [Fabric medallion architecture workspace strategy](https://learn.microsoft.com/en-us/azure/architecture/databases/architecture/dataops-mdw).

Use a **Warehouse-centric Gold** layer for the hypothetical periodic CSV/Excel inputs assumed by this design and the transformation / semantic contract is relational and SQL-first. Land files unchanged in Bronze, then use explicit Silver models and dimensional Gold models. Fabric Warehouse is the dbt target. Create a Power BI semantic model over Gold in Direct Lake mode where supported; use Import if model features or source constraints require it. Fabric supports semantic models in Direct Lake, Import, and DirectQuery, and Warehouse is intended for curated data marts and governed BI models. See [Create a Fabric semantic model](https://learn.microsoft.com/en-us/fabric/data-warehouse/create-semantic-model) and the [Fabric Warehouse overview](https://learn.microsoft.com/en-us/fabric/data-warehouse/data-warehousing). A Lakehouse-first design is more suitable if later requirements include large-scale Spark processing, extensive semi-structured files, or data-science workloads; those needs are not established by this sample.

For SQL/dbt transformations, the Fabric Warehouse executes the SQL. A scheduled Fabric dbt job or pipeline can also host the job orchestration and consume the assigned Fabric capacity; use it if you want recurring runs without a laptop runner. Fabric notebooks offer managed Python/Spark compute, but a general-purpose VM or Spark pool is unnecessary for these relational monthly snapshots. Keep developer agents and repository CI on local or hosted runners. Capacity is shared across workspaces, so monitor capacity usage in any future deployment and avoid Spark unless measured workload needs justify it. See [Fabric dbt job patterns](https://learn.microsoft.com/en-us/fabric/data-factory/common-dbt-job-patterns), [dbt job capacity usage](https://learn.microsoft.com/en-us/fabric/data-factory/dbt-job-pricing), and [Fabric Spark capacity usage](https://learn.microsoft.com/en-us/fabric/data-engineering/billing-capacity-management-for-spark).

Keep Dev, Test, and Prod isolated in any future production implementation. Business users should consume the semantic model through an app or Viewer access; do not grant workspace edit roles to report consumers. Apply RLS only where separate HR populations need row filtering, and do not rely on it as a substitute for keeping restricted or unapproved attributes out of the published model.

## Proposed semantic model

The model is a dimensional star, not one flattened employee-timesheet table. It separates periodic state from events and preserves the natural grain of each export. Define one-to-many, single-direction relationships from dimensions to facts; use the active date relationship stated in the contract for each fact. Avoid fact-to-fact joins and many-to-many employee relationships. Draft DAX measures are in [semantic/measures.dax](semantic/measures.dax); validate their relationships and filter context against the final model before publishing. Monthly snapshot measures must select one month-end, and event facts must deduplicate repeated monthly extracts.

| Fact | Grain | Main use |
|---|---|---|
| `fact_workforce_snapshot` | One employee key × month-end snapshot | Headcount, job/location mix, tenure trends |
| `fact_requisition_snapshot` | One requisition × month-end snapshot | Open roles, openings, recruitment status |
| `fact_hire` | One accepted hire / employee start event | Hires and time-to-hire |
| `fact_termination` | One termination event | Termination counts and turnover |
| `fact_schedule_actual` | One employee × work date after source-row reconciliation | Scheduled/actual hours and variance |
| `fact_time_entry` | One employee × entry date × entry type code after source-row reconciliation | Ordinary time, overtime and category-filtered absence hours |
| `fact_leave_balance_snapshot` | One employee × leave type × month-end snapshot | Accrued, used, scheduled and remaining leave |

Shared dimensions: `dim_date` (role-playing), `dim_employee_period` (pseudonymous key and approved as-of attributes), `dim_organization` (company/location/department), `dim_job`, `dim_requisition`, `dim_time_entry`, and `dim_leave_type`. Use surrogate keys in Gold. Keep source employee IDs and the HMAC key mapping in restricted staging only. A pseudonymous employee key is still personal data; exact dates and rare job/location combinations can re-identify people. Leadership must read only pre-aggregated approved outputs. Suppression policy, including complementary suppression, has not been implemented and blocks publication; no group-size threshold is assumed in this template.

Suggested measures and rules are in [semantic-model.yml](semantic-model.yml). Absenteeism needs an approved absence-type map. Requisition-level applicant totals do not establish a candidate-stage funnel. Proposed report views illustrate coverage only; production equivalence requires independent validation.

## Validate the synthetic fixtures

These files are synthetic design fixtures. Their expected values validate the example starter definitions only. They can be used to build dbt models once the source-to-target mappings and report calculations are approved.

```text
fixtures/input/employee_snapshot.csv
fixtures/input/requisition_snapshot.csv
fixtures/input/schedule_actual.csv
fixtures/input/time_entry.csv
fixtures/input/time_to_hire.csv
fixtures/input/leave_balance_snapshot.csv
fixtures/input/termination_event.csv
fixtures/expected/monthly_hr_kpis.csv
```

The neutral `time_entry` input uses `entry_date`, `entry_type_code`, `hours`, `reported_date`, and `entry_category` for ordinary time, overtime and absence. Absence measures filter `entry_category = ABSENCE`. The `entry_category` in the fixture is already normalized for testing. Production must map raw entry type codes through a reviewed allowlist; do not infer absence from a scheduled-versus-actual-hours difference. The fixtures do not implement or verify privacy suppression, historical employee attribute handling, production source overlap reconciliation, or production snapshot completeness.

## Source onboarding and release gates

1. For any future deployment, define authoritative inputs and required outputs for each subject area. Confirm snapshot date, time zone, row grain, keys, code lists, and whether each export is a complete or incremental snapshot.
2. Approve the field allowlist and retention policy. Classification-sensitive values require owner approval. Reject raw direct identifiers and sensitive fields from Gold. HMAC employee keys with a secret stored outside the repository and outside model outputs.
3. Map source headers into the normalized contract and add explicit type, null, duplicate, relationship, and date-range checks.
4. Build Silver and Gold against synthetic fixtures first; require exact KPI fixture outputs, separate fact-grain tests, export-manifest completeness checks, and overlap-reconciliation tests.
5. Reconcile the approved semantic measures with the intended generic output definitions (including filter context, blank handling, annualization, and date boundaries).
6. Use only synthetic data for this design proof. A real-data pilot needs explicit data-owner authorization and a security review before upload. Leadership publication additionally requires a pre-aggregated model plus tested primary and complementary suppression; the current draft is not safe for that release.

This generic design intentionally leaves business meanings unresolved. Unresolved items include active-worker status rules, whether terminated employees remain in a month-end snapshot, requisition versus opening counts, the official absence type mapping, treatment of rehires/transfers, and annualization conventions and date-field selection for any future measures. Record decisions before generating production-facing marts.

## Contract boundaries and local validation

The `fact_*` Gold names are logical design names, not installed dbt models. Any later dbt implementation must follow the selected edition's naming and SQL conventions (`mart_*` for BigQuery, `fct_*` for Snowflake), keep profiles and dependencies separate, and place cloud acceptance/deployment in a separate private repository. The separate Fabric notebook below is a synthetic-only Spark proof of concept; it is not a dbt adapter.

Logical fact names in the crosswalk map through `facts[].model_name` to snake-case Gold names and through `semantic_table` to DAX names. `semantic/measures.dax` is analyst-only; `semantic/leadership-measures.dax` belongs to a separate aggregate-only model with a month-grain calendar. Neither file is executable model deployment or privacy enforcement. All snapshot measures must select exactly one month-end and check the source-specific validated manifest; the draft DAX includes these guards but still requires model execution and filter-context tests.

Canonical headers and normalized columns are generic example allowlists, not observations of an existing export. Deployment adapters must validate authority, historical completeness, action semantics, and stable correction identities independently.

Primary and complementary suppression are **hard release gates**. Leadership has only approved aggregate tables, no detail dimensions, employee keys, or exact employee dates, and no route to detail through SQL endpoints, exports, drillthrough, or model permissions. Totals, intersecting filters and repeat releases must be tested for differencing. This is a proposed security structure, not deployed or tested access enforcement.

Run `python3 migration/templates/hr-analytics/validate_fixtures.py` from the repository root. It checks generic contract structure, MOCK identifier prefixes, YAML/CSV shape, mapping consistency, baseline synthetic KPI arithmetic and isolated edge-case expectations. Edge cases are normalized synthetic scenarios, not source extracts. It does not execute DAX, suppression, Fabric permissions, warehouse execution or real report equivalence. Source overlap resolution and stable correction identity remain design-only production release blockers.

The baseline contains a repeated employee row and repeated termination exports; deduplication leaves expected counts unchanged. Edge scenarios cover missing prior months, active-only treatment of leave status (a fixture convention awaiting owner approval), multi-month selection, complete empty and incomplete nonempty snapshots, rehire episodes, corrected start dates, conflicting revisions, and overlapping same-day shifts. Stable identities in these scenarios are synthetic assumptions; they do not prove source-key availability. Input CSV headers follow the source contract’s `fixture_input_policy`: canonical source columns with pseudonymous `employee_key`, explicit optional source columns, and required derived columns. The validator rejects missing required or unapproved columns. Snapshot dates come from the manifest, event keys and termination dates from mapped derivations, and `entry_category` from the versioned type map. Expected absence arithmetic is synthetic-only and never authorizes an absence metric release.

Analyst DAX covers requisition counts/openings, applicant totals, schedule/actual variance, category-filtered absence hours, and annual leave accrued/taken/scheduled/remaining balances. Category-filtered absence hours remain a draft supporting measure pending approval. Median time-to-hire excludes missing dates and negative intervals defensively; upstream negative intervals still fail validation/quarantine, and hires with missing requisition dates retain a blank duration. Complete empty snapshots return zero; absent/incomplete or multi-month snapshots return blank.

Mock data recipe: fictional MOCK-WORKER/MOCK-REQ/MOCK-EVENT identifiers, MOCK-ORG-QUARTZ organization, MOCK-SITE locations, and invented 2032 periods. All eight CSVs were authored together for this public example; the expected KPI values follow the mock inputs. Edge scenarios remain unrelated synthetic regression cases.

## Fabric Spark proof of concept

[`fabric/hr_analytics_lakehouse_notebook.py`](fabric/hr_analytics_lakehouse_notebook.py) transforms the seven synthetic Bronze Delta tables into Silver and Gold Delta tables. Configure the three Lakehouse IDs (and workspace ID) using the notebook parameters or Spark properties. Before it writes any Silver data, it requires a `bronze_export_manifest` table with the contract fields and complete-evidence flag, and reconciles declared row counts against Bronze. The synthetic manifest fixture is in `fixtures/input/export_manifest.csv`; it was loaded into Bronze for the proof of concept. The notebook never creates completeness claims from observed nonzero rows. It is on-demand only; do not schedule it.

The notebook writes `gold_monthly_hr_kpis` and `gold_analyst_pseudonymous_detail` using the current proof-of-concept semantic-model column names. It deliberately excludes leave balances from leadership output and keeps absence measures blocked. One manual synthetic-only Fabric run completed and produced the Silver and Gold tables. The documented release policy blocks leadership publication until approved primary and complementary suppression is implemented. It does not create permissions, security groups, production effective-dated history, or source correction winner rules. Workspace IDs are supplied at runtime and are not committed here.

## Follow-on implementation package

The next 20 enhancement artifacts are tracked in [ENHANCEMENTS-11-30.md](ENHANCEMENTS-11-30.md). Reusable contracts cover metric versions, absence-code mapping, disclosure policy, access boundaries, workforce episodes, recruitment-funnel grain, manual orchestration, and report acceptance. [DEPLOYMENT.md](DEPLOYMENT.md) documents generic environment, recovery, monitoring, and release gates.

The Fabric notebook now deduplicates exact retained Silver rows, quarantines conflicting retained keys, and supports `strict_schema=true` to reject columns outside a source contract. It also writes `gold_hr_quality_scorecard` and appends `gold_hr_run_audit`. The audit records a `started` row before the run and `completed` rows after success; an abandoned `started` entry signals an interrupted run but does not replace platform alerting. `gold_hr_quality_scorecard` contains source counts and manifest-backed snapshot row-count checks. These additions are template code and have not been redeployed to the trial workspace.

To compare an exported Gold KPI CSV with the authored mock output, run `python3 migration/templates/hr-analytics/reconcile_outputs.py <actual.csv>` from the repository root. This comparison is a release aid; it does not approve metric definitions or privacy policy. A live synthetic query previously found a termination-count discrepancy and rejected schedule rows, so expected results remain unreconciled until the retained-history cause is verified.
