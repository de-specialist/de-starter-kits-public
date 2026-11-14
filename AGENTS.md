# Repository Guidelines

## Project Structure

This repository contains two independent dbt editions. Keep edition-specific code and documentation under its matching folder; use root `docs/` for shared guidance:

- `editions/Bigquery/`: BigQuery online retail project, based on TheLook eCommerce public data.
- `editions/Snowflake/`: Snowflake project, synthetic seeds, and setup/adaptation docs.
- `docs/`: shared walkthroughs and cross-edition documentation.
- `.github/workflows/`: separate CI workflows for each edition.

Do not mix dbt profiles, dependencies, or generated artifacts between editions. Each edition has its own `requirements.txt` and `dbt_project.yml`.

| Edition | dbt project dir | `requirements.txt` | dbt project / profile name | Data source |
|---|---|---|---|---|
| BigQuery | `editions/Bigquery/` | same dir | `retail_analytics` | `bigquery-public-data.thelook_ecommerce` (declared in `models/sources.yml`) |
| Snowflake | `editions/Snowflake/kit/` | `editions/Snowflake/` (one level **above** the dbt project) | `reliable_pipeline` | Synthetic CSV seeds in `kit/seeds/` (referenced via `ref('raw_*')`; only the optional migration models use `source()`) |

Always run dbt from the directory containing that edition's `dbt_project.yml`.

## Development Commands

Use Python 3.11 and install the edition's pinned dependencies from its directory with `python -m pip install --require-hashes -r requirements-lock.txt`. Edit `requirements.txt` and regenerate its lock file when changing dependencies. Keep each local profile within its edition and set `DBT_PROFILES_DIR` before running dbt. Run dbt commands from the same directory as that edition's `dbt_project.yml`. BigQuery uses `dbt debug` and `dbt build`; Snowflake uses `dbt debug`, `dbt seed`, and `dbt build` from `editions/Snowflake/kit/`.

```sh
# BigQuery (from editions/Bigquery/)
python -m pip install --require-hashes -r requirements-lock.txt
mkdir -p .dbt && cp -n profiles.example.yml .dbt/profiles.yml
export DBT_PROFILES_DIR="$PWD/.dbt"
dbt debug
dbt build
dbt parse --profiles-dir .ci

# Snowflake (from editions/Snowflake/kit/)
python -m pip install --require-hashes -r ../requirements-lock.txt
mkdir -p .dbt && cp -n profiles.example.yml .dbt/profiles.yml
export DBT_PROFILES_DIR="$PWD/.dbt"
dbt debug
dbt seed
dbt build

# Offline migration safety/regression tests (from repository root)
python3.11 -m venv migration/.venv
migration/.venv/bin/python -m pip install --disable-pip-version-check --require-hashes -r migration/tests-requirements-lock.txt
CI=true migration/.venv/bin/python -m unittest discover -s scripts/tests -v
CI=true migration/.venv/bin/python -m unittest discover -s migration/tests -v
```

`CI=true` makes migration tests fail instead of silently skipping when runtime dependencies are missing. These tests use temporary fixtures and do not connect to a warehouse.

Each edition keeps its local profile in its own ignored `.dbt/` folder (`editions/Bigquery/.dbt/`, `editions/Snowflake/kit/.dbt/`) under its profile key (`retail_analytics`, `reliable_pipeline`); point `DBT_PROFILES_DIR` at it rather than at a shared `~/.dbt/profiles.yml`. `requirements.txt` pins dbt `<2.0`; don't follow dbt's "v2 is available" upgrade prompt.

## Style and Testing

Use two spaces for YAML indentation, descriptive lowercase `snake_case` names for SQL models, and concise Markdown instructions. Keep cleanup in `staging/` and business logic in `marts/`. Add dbt schema tests for key constraints and relationships; `dbt build` runs models and tests in dependency order. BigQuery CI currently runs `dbt parse` without cloud credentials; a successful parse does not validate warehouse execution.

- Layering: `models/staging/` (views) for renaming/casts/normalization; `models/marts/` (tables) for joins and aggregates. Materializations are set per folder in `dbt_project.yml`.
- Mart naming: BigQuery uses `mart_*`, Snowflake uses `fct_*`. Staging is `stg_*` in both.
- Write SQL in each warehouse's dialect — BigQuery: `if()`, `countif()`, `int64`; Snowflake: `iff()`, `count_if()`, `::` casts.
- Core model tests live in each edition's `models/schema.yml` using dbt 1.10+ `data_tests:` with parameters under `arguments:`.
- BigQuery curated models must exclude direct personal identifiers; `stg_users` deliberately drops them.
- Keep business metric definitions in edition READMEs in sync with SQL.

## Commits and Pull Requests

Use concise imperative commit subjects (for example, `Add BigQuery customer value mart`). Pull requests should describe the user-facing change, identify which edition is affected, and state the validation performed. Include screenshots for dashboard or other visual changes.

Public CI is credential-free and always uses GitHub-hosted Ubuntu runners. Keep
cloud acceptance and deployment in a separate private repository. Do not add
self-hosted runner routing, automatic merge workflows or deployment credentials.
Do not copy private histories, internal records or customer-specific files into
this public repository. Use synthetic fixtures for all tests.

## Security and Data Handling

Never commit credentials, local `profiles.yml` files, or private customer data. Keep local API credentials in the root `.env` (ignored by Git) and never commit their values. Use environment variables and secret managers for deployed credentials. Keep synthetic examples clearly labeled; review source dataset terms before redistributing public data. Exclude direct personal identifiers from curated example marts unless the use case requires them.
