# Private warehouse acceptance

Warehouse acceptance belongs in an isolated private environment, outside public
CI. Configure your own identity and disposable target through local environment
variables and ignored profiles. Validate connectivity, seed synthetic examples,
run the edition's build and compare incremental results with a full refresh.

Record target details, costs and execution evidence privately. Tear down temporary
resources using your normal approval process. Public CI does not grant permission
to access accounts, incur cloud costs or destroy resources.

## Offline readiness check

Run `python scripts/cloud_acceptance_preflight.py --help` for edition-specific
options. This preflight makes no network call and never plans or applies Terraform.
It checks project files and the explicitly selected local profile without printing
its values. Passing it does not prove credentials, permissions or disposable scope.

For Snowflake, use your own disposable `DBT_KIT_DEV..._DB` or `DBT_KIT_CI..._DB`
target and matching schema; the legacy `DBT_DEV` target is refused. Supply the
explicit `--account` confirmation locally when required; its expected form is
`ACCOUNT/DATABASE.SCHEMA`. Keep those actual values in private configuration.
