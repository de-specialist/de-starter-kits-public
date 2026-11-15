# Operator incident runbook

Use this guide for setup, dbt, migration, and unexpected cost incidents. Stop
before retrying an operation that may have written to a warehouse. Keep local
profiles, source files, manifests, logs, and target identifiers private.

## First response

1. Record the edition, repository revision, command, approximate start time,
   result, and the non-secret error code or message.
2. Confirm the intended non-production project/account, database, schema, and
   warehouse using your own approved configuration. Do not paste profile
   contents, environment values, row data, or full connection strings into an
   issue or shared log.
3. Preserve the relevant private run artifacts before retrying. A parse-only
   result is not evidence that a warehouse build or migration succeeded.
4. If a credential may have been exposed, stop using it and rotate or revoke it
   through the credential owner. Removing it from a working file does not
   remove it from repository history or logs.

## Setup or authentication failure

- Check that Python and the edition's hash-locked requirements were installed
  in the intended environment.
- Check that `DBT_PROFILES_DIR` points to that edition's ignored `.dbt/`
  directory, not a shared profile directory.
- Run `dbt debug` from the directory containing the selected edition's
  `dbt_project.yml`, with the exact profile and target you intend to use. This
  contacts the configured warehouse; confirm the target first.
- Do not include `.dbt/profiles.yml`, `.env`, key files, or authentication
  output in a support request.

## Parse or model build failure

- Preserve the command, failing model/test name, revision, and a redacted error
  excerpt. Keep full logs local if they include account names or SQL values.
- A successful `dbt parse` checks project configuration only. For a failed
  build, inspect the selected target, source grants, schema, and failing model
  before retrying.
- Do not add `--full-refresh`, drop tables, or delete warehouse objects as a
  first response. Those actions can replace data and increase query cost.
- Confirm that any retry is directed at the same approved isolated target.

## Migration interruption or incomplete load

- Stop further loads for the same target and preserve the private run manifest,
  dlt state, and any destination job identifiers.
- Follow the recovery and reconciliation limits in the
  [migration operator guide](../migration/README.md). A failed replacement may
  have written some destination tables. This kit does not automatically resume,
  roll back, or prove destination cleanup.
- Reconcile the affected destination objects independently. Retry only after
  the designated operator has recorded the required evidence and confirmed the
  exact target and affected tables.
- Never edit a run manifest to clear a refusal or delete state to force a retry.

## Unexpected warehouse cost or activity

- Stop the responsible query or suspend the non-production warehouse using the
  warehouse owner's approved console or incident process.
- Record the job/query identifier, project or account, time range, and billed
  usage privately. Do not copy query results or credentials into a public issue.
- Review recent jobs and scheduled refreshes, then check the configured target
  and query date filters before restarting.
- Budgets, per-query byte limits, and dashboards are monitoring controls; they
  do not guarantee a total spend cap or stop every query.

## Suspected security exposure

- Revoke exposed credentials, remove unauthorized access, and preserve
  timestamps and affected resource names for the owner.
- Report a repository vulnerability through the private channel in
  [SECURITY.md](../SECURITY.md). Do not publish credentials or customer data in
  an issue.
- Review repository history and CI logs for the exposed value. Treat the value
  as compromised even if it has since been deleted from the current files.

## Escalation record

Keep a private incident note with the owner, affected edition and target,
revision, timeline, actions taken, reconciliation evidence, and the person who
approved any retry or recovery. Share only redacted technical symptoms in
public discussions.
