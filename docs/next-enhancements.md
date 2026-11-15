# Next enhancements

This plan sequences the next 20 improvements for the public starter kits. It
separates buyer evidence from engineering work: implementation does not prove
customer demand, and offline checks do not prove warehouse behavior. Keep
participant records, credentials, and account-specific acceptance evidence
private. Use synthetic fixtures in this repository and keep cloud acceptance in
the designated private environment.

## Phase 1 — establish evidence

Complete these gates before expanding product scope. Record outcomes and update
the plan when evidence changes the direction.

1. **Interview five qualified buyers.** Use the private discovery materials.
   Capture pseudonymous notes about recent problems, buying authority,
   workarounds, and next steps. Decide whether a repeated material problem is
   supported before proceeding to a paid offer.
2. **Test one concrete paid offer.** State the buyer, deliverables, licence,
   update policy, exclusions, and support boundaries. Seek a buying action or
   paid pilot; record proceed, revise, or stop.
3. **Observe two clean-checkout installs.** Have independent users follow the
   lead-edition instructions without maintainer intervention. Record elapsed
   time, errors, and support minutes; fix repeated blockers.
4. **Run controlled warehouse acceptance builds.** Run each edition in an
   isolated non-production target with its authored dbt tests. Record exact
   revision, command, environment, target class, results, and cost evidence in
   the private acceptance record. Do not infer warehouse success from parsing.

## Current engineering priority order

This queue runs alongside buyer discovery. Repository tests and authored
fixtures are implementation evidence; they do not substitute for a buyer
decision or a warehouse acceptance run.

1. **BigQuery acceptance build.** Run the lead edition's authored dbt build and
   tests on an approved isolated target. No successful warehouse result is
   recorded in this repository.
2. **Snowflake acceptance plus UTC parity.** Run its authored dbt build and
   compare timestamp-sensitive Silver/Gold results with BigQuery. Shared local
   fixtures exist; warehouse parity is unverified.
3. **Silver scaffolder write mode.** Deterministic edition-local generation,
   edit-preservation rules, atomic no-replace publication, and clean-checkout
   tests are implemented. Warehouse execution and independent user acceptance
   remain separate evidence gates.
4. **Migration recovery and source snapshots.** Complete the PostgreSQL
   snapshot-guard integration and define operator recovery for interrupted
   replacement. The guard is wired into the loader, dlt 1.32.0 callback support
   is verified, and 402 migration offline tests pass. Live PostgreSQL behavior,
   and BigQuery/Snowflake interruption semantics remain unverified. The private
   counterpart now has an append-only operator-attested retry resolution path;
   its evidence references are not independently verified destination proof.
5. **Fabric product scope decision.** The HR notebook is a synthetic proof of
   concept, not a Fabric dbt edition. Validate demand and the support boundary
   before expanding it; any repeat workspace deployment remains approval-gated.

## Phase 2 — prove migration behavior

Keep these tasks synthetic/offline until the private acceptance process has an
approved target and explicit owner authorization.

5. **Define source snapshot evidence.** The versioned source snapshot contract
   now specifies the stable boundary, pre-extraction checks, and minimal
   evidence. Live source acceptance remains outstanding.
6. **Design resumable extraction checkpoints.** The incremental ingest
   template has checkpoint, restart, and duplicate/loss handling with
   synthetic interruption coverage. Warehouse-backed restart behavior remains
   unverified.
7. **Demonstrate safe reruns and rollback.** Offline tests cover guarded
   replacement, retry and cleanup boundaries. Disposable live-destination
   acceptance remains outstanding.
8. **Validate timestamp semantics across editions.** Offline fixtures now
   cover offset normalization, repeated daylight-saving hours, equivalent
   instants, and UTC date rollover. BigQuery/Snowflake ordering and derived
   business-date parity remain unverified.
9. **Produce warehouse-backed Gold parity evidence.** In isolated targets,
   compare agreed source totals and migration Gold outputs with a full-refresh
   dbt baseline. Keep results private and label unrun editions as unverified.
10. **Add a privacy-safe migration run summary.** The CLI emits a structured
    summary and offline tests cover its privacy boundary. Destination-specific
    acceptance remains outstanding.

## Phase 3 — improve reuse

11. **Close the generic HR template disclosure gates.** Keep leadership
    publication disabled until an owner approves a disclosure policy and
    primary/complementary suppression is implemented and independently tested
    for intersecting filters, repeated releases, exports, and permissions.
12. **Check generated workspaces end to end.** For each edition, generate a
    clean workspace, verify its project/profile guidance and exclusions, then
    run credential-free parsing. Keep cloud credentials and warehouse builds
    out of public CI.
13. **Create an edition-specific first-run path.** Link local preflight,
    profile setup, `dbt debug`, and the first build in one ordered walkthrough
    per edition. Validate both against a clean checkout.
14. **Build the documented sample dashboard.** Create the BigQuery Looker
    Studio report from the documented marts, show its four intended views, and
    explain refresh/query cost and credential boundaries. Do not include a
    customer report or credentials.
15. **Add a source-adaptation walkthrough.** Use a synthetic representative
    input to demonstrate mapping into existing contracts, including grain,
    update/delete behavior, and tests. Select no production connector until
    buyer evidence identifies a repeated need.

## Phase 4 — prepare for support and release

16. **Set a support and update promise.** Decide supported versions, response
    boundaries, update cadence, and how compatibility changes are announced.
17. **Rehearse the release archive process.** Build each edition archive,
    inspect its files and checksums, run credential and path checks, and
    complete the release validation record before publishing.
18. **Schedule dependency compatibility reviews.** Assign an owner and cadence
    for reviewing proposed Python and GitHub Actions updates, regenerating
    locks, and recording supported combinations.
19. **Write an operator incident runbook.** Cover common profile, dbt, source,
    migration, and cost failures with safe diagnostics, recovery boundaries,
    and escalation guidance.
20. **Revisit this plan after interviews and pilots.** Record which evidence
    changed priorities, close or revise stale tasks, and promote only the next
    few validated tasks into active work.

## Suggested sequencing

Start with items 1–4, which establish whether the offer is useful and whether
the core editions work for independent users. Do items 5–10 only as migration
scope is validated. Keep item 11 gated by privacy approval. The remaining
packaging and support work should follow observed user friction and an actual
release decision.

## Completion evidence

For each item, keep a short record of owner, status, evidence, and date. Store
private interview, customer, credential, and cloud-account evidence outside
this public repository. A code change or passing offline test is implementation
evidence only; it does not substitute for buyer validation or warehouse
acceptance.

## Current status (2026-11-15)

| Items | Status | Evidence or remaining gate |
| --- | --- | --- |
| 1–3 | Owner-dependent | No buyer interviews, paid pilot, or independent install records are available in this repository. |
| 4, 9 | Owner-dependent | No approved warehouse acceptance or Gold parity evidence is recorded. |
| 5–8, 10 | Offline implementation present; live acceptance pending | Contracts, checkpoints, recovery guards, timestamp fixtures, summaries, and offline tests are present; adapter and destination semantics remain unverified. |
| 11 | Approval-gated | Synthetic HR assets and disclosure examples exist; owner-approved suppression policy and independent disclosure testing are still required before publication. |
| 12–13 | Local implementation verified; independent installs pending | Clean generated workspaces for both editions passed first-run preflight and credential-free dbt parsing in temporary directories; independent user install observations are not recorded. |
| 14 | Incomplete | No published sample dashboard or report account is available in the evidence record. |
| 15 | Offline implementation present; independent validation pending | Synthetic source fixtures, a canonical mapping, a grain/update/delete walkthrough, and a local DuckDB dry run are present; independent and warehouse acceptance remain outstanding. |
| 16 | Owner-dependent | Support and compatibility promises need an explicit product decision. |
| 17 | Implemented; release record pending | Archive construction and validation tooling exist; no signed release acceptance record is included here. |
| 18 | Implemented; ongoing review required | Dependency review automation is configured; maintainers still need to review and accept update batches. |
| 19 | Implemented | The operator incident runbook is linked from the README and documentation navigation. |
| 20 | Pending evidence | Revisit priorities after buyer interviews and pilots; no such decision record is available here. |
