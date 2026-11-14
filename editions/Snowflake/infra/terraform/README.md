# Snowflake development infrastructure (Terraform)

This edition-local root module creates one disposable Snowflake database, schema, and small standard warehouse for development. Names must start with `DBT_KIT_DEV` or `DBT_KIT_CI`, which prevents accidental use of production-like names. The warehouse is created suspended, auto-resumes when used, and auto-suspends after 60 seconds by default. The database is transient, so its tables have no Fail-safe period and dbt's repeated table rebuilds do not accumulate seven days of billable Fail-safe storage. Database Time Travel retention defaults to one day. Because the database is transient, dropped or replaced data cannot be recovered after the Time Travel window.

The module also grants an existing dbt account role `USAGE` on the database and warehouse, and `USAGE`, `CREATE TABLE`, and `CREATE VIEW` on the dedicated schema. That is enough for `dbt seed` and `dbt build`: dbt-snowflake loads seeds with `CREATE TABLE` and batched `INSERT` statements, not with stages or file formats. Grant `CREATE STAGE` or `CREATE FILE FORMAT` yourself only if you add models that load files through a stage. The module does not create or alter account roles, users, networks, or production resources.

## Prerequisites and permissions

- Terraform CLI 1.1.5 or newer.
- A dedicated non-admin Snowflake provisioning identity using a protected local Snowflake CLI profile or secret-injected provider environment. Do not put passwords, private keys, or tokens in Terraform files, `.tfvars`, shell history, or Git.
- An existing custom, non-admin `dbt_role_name` to receive the narrow target grants. The module rejects Snowflake's built-in administrative roles and `PUBLIC`.
- The Terraform provisioning role needs the account-level `CREATE DATABASE` and `CREATE WAREHOUSE` privileges. It becomes owner of the resources it creates and uses that ownership to grant the listed privileges on only those resources. It does not need `ACCOUNTADMIN`, `MANAGE GRANTS`, `CREATE ROLE`, or `CREATE USER`.
- An administrator must provision the dedicated Terraform role and its two create privileges, and ensure the named dbt role exists. No admin role or credential is configured by this module.

Snowflake resource monitors are intentionally omitted. Snowflake currently limits resource-monitor creation to `ACCOUNTADMIN`; assigning one to a warehouse would also require an elevated path in the Terraform provider. Do not use an account-level monitor for this sample. The warehouse's X-Small/Small size, 60-second auto-suspend, a per-statement timeout (`warehouse_statement_timeout_seconds`, default 1800 seconds, capped at 3600), and dedicated naming are the least-privilege cost controls in this module.

## Configure and apply

Authenticate the Snowflake provider through its supported environment variables or a protected `~/.snowflake/config` profile. The provider accepts `SNOWFLAKE_ORGANIZATION_NAME`, `SNOWFLAKE_ACCOUNT_NAME`, `SNOWFLAKE_USER`, `SNOWFLAKE_ROLE`, and authentication variables; inject credentials from a secret manager or protected local environment. Select the dedicated non-admin Terraform role, not `ACCOUNTADMIN`.

Copy the sample variable file and set the name of the existing dbt role before applying. The default resource prefix creates new resources with a `DBT_KIT_DEV_TF` prefix; change it only to a unique `DBT_KIT_DEV...` or `DBT_KIT_CI...` prefix. Keep the resulting `terraform.tfvars`, state, plan files, and `.terraform/` directory local. This root module uses local Terraform state; do not share or commit it.

```sh
cp -n editions/Snowflake/infra/terraform/terraform.tfvars.example editions/Snowflake/infra/terraform/terraform.tfvars
```

Edit `editions/Snowflake/infra/terraform/terraform.tfvars` and replace the placeholder with the existing dbt role name. Verify the provider authentication selects the intended non-admin provisioning role and the correct Snowflake account.

From the repository root, use the shared lifecycle command. `apply` remains interactive and asks for confirmation:

```sh
./scripts/terraform_lifecycle.sh snowflake apply
```

In the edition ZIP, run from the extracted `de-starter-kit-snowflake/` directory:

```sh
cp -n infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
./scripts/terraform_lifecycle.sh snowflake apply
```

Review the proposed resource names and account before confirming. Never use `-auto-approve`. The committed `.terraform.lock.hcl` pins provider selections; use `terraform init -lockfile=readonly` in CI and review lockfile changes when intentionally updating providers. Use the output names to configure the Snowflake dbt profile. Confirm the selected dbt profile points only at these isolated resources before running dbt.

### Upgrading an existing deployment

Earlier versions of this module created a permanent database. Snowflake cannot convert a permanent database to a transient one, so if you applied an earlier version, the next apply plans to **replace** the database: Terraform drops it with all its schemas, tables, and data, then creates an empty transient database. Check the plan for `must be replaced` on `snowflake_database.dev` and copy anything you need first; then rerun `dbt seed` and `dbt build`.

## Tear down

Use the shared lifecycle command when the target is disposable and no longer needed. Terraform prompts before destroying resources; review the plan and confirm interactively. Never use `-auto-approve`:

```sh
./scripts/terraform_lifecycle.sh snowflake destroy
```

Destroying this database drops the database and its contained schemas, tables, views, and data. This is destructive even if Snowflake may retain dropped data temporarily under its Time Travel rules; transient databases have no Fail-safe, so do not rely on recovery. Keep this module's state and resources isolated to a disposable sample or CI target. Do not point it at a shared, production, or user-managed database. The warehouse and database are billable resources while in use; auto-suspend does not remove storage charges.

## References

- [Snowflake Terraform provider 2.22.0](https://registry.terraform.io/providers/snowflakedb/snowflake/2.22.0)
- [Provider authentication methods](https://registry.terraform.io/providers/snowflakedb/snowflake/latest/docs/guides/authentication_methods)
- [Warehouse resource](https://registry.terraform.io/providers/snowflakedb/snowflake/latest/docs/resources/warehouse)
- [Database and schema resources](https://registry.terraform.io/providers/snowflakedb/snowflake/latest/docs/resources/database), [schema](https://registry.terraform.io/providers/snowflakedb/snowflake/latest/docs/resources/schema)
- [Grant privileges to an account role](https://registry.terraform.io/providers/snowflakedb/snowflake/latest/docs/resources/grant_privileges_to_account_role)
- [Snowflake resource monitor access and scope](https://docs.snowflake.com/en/user-guide/resource-monitors)
- [Create warehouse](https://docs.snowflake.com/en/sql-reference/sql/create-warehouse)
- [dbt Snowflake setup and privilege requirements](https://docs.getdbt.com/docs/local/connect-data-platform/snowflake-setup)
