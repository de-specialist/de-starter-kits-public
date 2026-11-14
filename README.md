# Data Engineering Starter Kits

Generic dbt examples for learning and evaluation, with independent BigQuery and
Snowflake editions and synthetic migration fixtures.

| Edition | Project directory | Example data |
|---|---|---|
| BigQuery | `editions/Bigquery/` | Queries TheLook eCommerce public dataset in place; no dataset rows are bundled |
| Snowflake | `editions/Snowflake/kit/` | Clearly labeled synthetic retail CSV seeds |

Start with the [first-run guide](docs/first-run.md), the
[BigQuery setup](editions/Bigquery/docs/setup.md), or the
[Snowflake setup](editions/Snowflake/docs/setup.md). The optional
[migration tools](migration/README.md) include synthetic local fixtures.

## Configuration stays private

Copy the edition's `profiles.example.yml` into its ignored `.dbt/profiles.yml`
and supply your own environment variables. Keep real credentials, local target
identifiers, customer mappings, archives and generated warehouse artifacts outside
this repository or in ignored local directories. `.env.example` contains variable
names and placeholders only. Never place a secret in a tracked example file.

This public repository contains no tenant-specific Fabric or HR deployment,
customer schemas, HR records, private deployment configuration or inherited Git
history. Public CI uses GitHub-hosted Ubuntu runners and synthetic/offline checks.
It does not authenticate to a warehouse, provision cloud resources or deploy a
website. dbt parsing does not prove warehouse execution.

## Development

Use Python 3.11 and separate hash-locked environments for each edition. Run dbt
from its edition's project directory. See `AGENTS.md` for project
conventions and [CONTRIBUTING.md](CONTRIBUTING.md) for contributions.

Documentation can be built locally with `bash scripts/build_docs_site.sh`.

## Terms and support

This is source-available under the existing **evaluation licence**, not an
open-source licence. Publication does not grant production-use or redistribution
rights beyond [LICENSE](LICENSE). Read [NOTICE](NOTICE), [SUPPORT.md](SUPPORT.md)
and [SECURITY.md](SECURITY.md) before use. Third-party data terms are separate.
