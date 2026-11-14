# Contributing

## Project layout

This repository contains independent BigQuery and Snowflake dbt starter kits. Keep models, profiles, dependencies, and generated files inside the matching edition. Shared migration utilities live in `migration/`; common docs live in `docs/`.

## Local development

Use Python 3.11 and the edition's hash-locked requirements. Run dbt from the directory containing that edition's `dbt_project.yml`; use its `.ci` profile for credential-free parsing. Check the edition README for profile setup before running a warehouse build.

Offline migration checks use synthetic fixtures and temporary local targets:

```sh
CI=true python -m unittest discover -s scripts/tests -v
CI=true python -m unittest discover -s migration/tests -v
```

Do not use production data in examples, tests, or pull requests. Never commit credentials, local profiles, `.env` files, Terraform variable files, dbt artifacts, or customer data. Keep direct identifiers out of curated marts.

## Changes and review

Use two-space YAML indentation and lowercase `snake_case` SQL model names. Keep cleanup in staging models and business logic in marts. Add dbt schema tests for key constraints and relationships. Use concise imperative commit subjects, such as `Add BigQuery customer value mart`.

Pull requests should explain the user-facing change, identify the affected edition, and list the validation performed. A successful dbt parse checks project configuration; it does not prove warehouse SQL execution. Include screenshots for dashboard or other visual changes.
