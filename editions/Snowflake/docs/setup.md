# Setup and deployment

## Prerequisites

- Python 3.11.
- A Snowflake account, warehouse, database, schema, and role with permission to create views and tables in the target schema.
- Git and a GitHub repository for CI.

## Local setup

From the repository root, create and activate a Python 3.11 virtual environment, then install the hash-verified dependencies in `editions/Snowflake/requirements-lock.txt`. If you copied this edition into a standalone workspace, use its local `requirements-lock.txt` instead. Create a development database, schema, role, and warehouse, or use equivalent resources provisioned by your Snowflake administrator. Keep development objects separate from production.

### Target naming

Use a disposable target that follows the repository Terraform naming pattern. Pick one short uppercase `<suffix>` (letters, digits, and underscores, for example your initials or `TF`) and use it for all three objects:

| Object | Name |
|---|---|
| Database | `DBT_KIT_DEV_<suffix>_DB` |
| Schema | `DBT_KIT_DEV_<suffix>_SCHEMA` |
| Warehouse | `DBT_KIT_DEV_<suffix>_WH` |

The preferred way to create these is the edition's [Terraform module](../infra/terraform/README.md); its default prefix `DBT_KIT_DEV_TF` produces `DBT_KIT_DEV_TF_DB`, `DBT_KIT_DEV_TF_SCHEMA`, and `DBT_KIT_DEV_TF_WH`, and its outputs give the exact names for your profile. Never point the kit at a pre-existing shared development database or schema that does not carry the `DBT_KIT_DEV_` prefix, and never reuse production names.

If you cannot use Terraform, an administrator can provision an equivalent isolated target manually. Replace every `<suffix>` with the same value before running:

```sql
CREATE ROLE IF NOT EXISTS DBT_DEV_ROLE;
CREATE DATABASE IF NOT EXISTS DBT_KIT_DEV_<suffix>_DB;
CREATE SCHEMA IF NOT EXISTS DBT_KIT_DEV_<suffix>_DB.DBT_KIT_DEV_<suffix>_SCHEMA;
CREATE WAREHOUSE IF NOT EXISTS DBT_KIT_DEV_<suffix>_WH
  WAREHOUSE_SIZE = XSMALL
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE;
GRANT USAGE ON DATABASE DBT_KIT_DEV_<suffix>_DB TO ROLE DBT_DEV_ROLE;
GRANT USAGE ON SCHEMA DBT_KIT_DEV_<suffix>_DB.DBT_KIT_DEV_<suffix>_SCHEMA TO ROLE DBT_DEV_ROLE;
GRANT CREATE TABLE, CREATE VIEW ON SCHEMA DBT_KIT_DEV_<suffix>_DB.DBT_KIT_DEV_<suffix>_SCHEMA TO ROLE DBT_DEV_ROLE;
GRANT USAGE ON WAREHOUSE DBT_KIT_DEV_<suffix>_WH TO ROLE DBT_DEV_ROLE;
GRANT ROLE DBT_DEV_ROLE TO USER <your_user>;
```

Replace `<your_user>` with your Snowflake username. Run provisioning statements using an administrator role with the required privileges. `DBT_DEV_ROLE` is only the example name of the non-admin dbt role; it is not a database or schema. Personal databases can restrict grants on schemas; use a regular, dedicated `DBT_KIT_DEV_<suffix>_DB` database if schema grants fail.

### Key-pair authentication (recommended for local development)

Register a named key pair for your user with `ROLE_RESTRICTION = 'DBT_DEV_ROLE'` and a suitable expiry. Generate and store the private key outside the repository, restrict its file permissions (for example, `chmod 600 ~/.snowflake/keys/dbt_dev_key.p8`), and register only the public key. Register it from a Snowflake session authenticated as `DBT_DEV_ROLE`; the active session role must match the requested role restriction. Your administrator may need to grant `MODIFY PROGRAMMATIC AUTHENTICATION METHODS` on your user to `DBT_DEV_ROLE` for key registration, then revoke that privilege after setup.

From `editions/Snowflake/kit/` (or the copied workspace's `kit/` folder), keep the local profile inside this edition and point dbt to it:

```sh
mkdir -p .dbt
cp -n profiles.example.yml .dbt/profiles.yml
export DBT_PROFILES_DIR="$PWD/.dbt"
```

The `-n` option preserves an existing local profile. Edit `.dbt/profiles.yml` to use your account, username, and local private key path:

```yaml
reliable_pipeline:
  target: dev
  outputs:
    dev:
      type: snowflake
      account: <account_identifier>
      user: <your_user>
      authenticator: SNOWFLAKE_JWT
      private_key_path: /absolute/path/to/dbt_dev_key.p8
      role: DBT_DEV_ROLE
      database: DBT_KIT_DEV_<suffix>_DB
      warehouse: DBT_KIT_DEV_<suffix>_WH
      schema: DBT_KIT_DEV_<suffix>_SCHEMA
      threads: 4
      client_session_keep_alive: false
```

Replace each `<suffix>` with the value used for your target (or copy the Terraform `database_name`, `schema_name`, and `warehouse_name` outputs). Set every target identifier explicitly; do not leave any of them blank or rely on a default. Run `dbt debug` and confirm the reported database, schema, and warehouse before `dbt build`.

Do not commit `profiles.yml`, private keys, or credentials. The repository ignores local profile files.

### Password authentication

For environments that require password authentication, keep the local `.dbt/profiles.yml` profile copied from `profiles.example.yml` and set all of `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD`, `SNOWFLAKE_ROLE`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_SCHEMA`, and `SNOWFLAKE_WAREHOUSE`. The example profile has no fallback for the role, database, schema, or warehouse, so dbt stops if any is unset. Use the disposable target names, for example:

```sh
export SNOWFLAKE_ROLE=DBT_DEV_ROLE
export SNOWFLAKE_DATABASE=DBT_KIT_DEV_<suffix>_DB
export SNOWFLAKE_SCHEMA=DBT_KIT_DEV_<suffix>_SCHEMA
export SNOWFLAKE_WAREHOUSE=DBT_KIT_DEV_<suffix>_WH
```

Prefer a secrets manager or environment variables over storing passwords in files.

Run from `editions/Snowflake/kit/` (or the copied workspace's `kit/` folder):

```sh
dbt debug
dbt build
```

`dbt build` loads the two seeds, then runs models and tests in dependency order. For subsequent runs, `dbt seed --full-refresh` reloads the small example CSV files after they change.

The sample project loads two synthetic seeds, creates staging views and a customer orders mart, and runs the schema tests declared under `models/`. Run `dbt debug` first to confirm profile and warehouse access. The sample profile tags sessions as `de_starter_snowflake`; set `SNOWFLAKE_QUERY_TAG` to another non-secret value to attribute development runs separately. See the [cost checks guide](cost-checks.md).

The optional migration target is separate from the default development target and uses the custom non-admin `DBT_MIGRATION_ROLE` with key-pair authentication. See the [Snowflake migration guide](migration.md) for its Bronze grants, local profile variables, expected source fields, and explicit opt-in build command.

## GitHub Actions

The workflow's real build runs only after a trusted push to `main`; pull requests get credential-free parsing. Add these repository secrets before enabling the build: `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_SCHEMA`, `SNOWFLAKE_WAREHOUSE`, and `SNOWFLAKE_ROLE`. The pre-build guard requires every value and verifies that the database is an isolated Terraform-style `DBT_KIT_DEV..._DB` or `DBT_KIT_CI..._DB` name and that the schema uses the identical prefix with `_SCHEMA` (for example, `DBT_KIT_CI_TEAM_DB` with `DBT_KIT_CI_TEAM_SCHEMA`). This matches the schema Terraform provisions and grants; do not set the schema to a generic `CI`. The credentialed steps explicitly use the `.ci` profile's `ci` target; secrets are scoped only to the guard and `dbt build`, while parsing and dependency installation use safe placeholder defaults. Use a dedicated CI role and disposable Terraform resources, never production credentials.

Edit `requirements.txt`, then regenerate the universal Python 3.11 lock from `editions/Snowflake/` with `uv pip compile requirements.txt --python-version 3.11 --universal --generate-hashes --output-file requirements-lock.txt` (requires `uv` on `PATH`). Commit both files together.

The provided workflow validates pull requests against the example seeds. For production, replace seeded inputs with isolated test sources and follow your organization's approval and deployment controls.
