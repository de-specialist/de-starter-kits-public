# Edition Workspaces and Release Archives

Use the scaffolder when adapting an edition for a separate client workspace. Use the packager only after completing the release checklist and resolving software and data terms.

## Create a clean workspace

```sh
python scripts/scaffold_edition.py bigquery /path/outside/repo/client-retail
python scripts/scaffold_edition.py snowflake /path/outside/repo/client-retail-snowflake
```

The destination must be new and outside this repository. The script copies only the selected edition and skips CI/local profiles, `.env` files, common credential directories, key/certificate files, virtual environments, dbt targets/packages/logs, and symlinks. It scans other included files and refuses to copy if it finds obvious private-key markers, cloud/provider token formats, Google service-account fields, or non-placeholder credential assignments. The error names the file and marker category without printing the value. Resolve the finding and review the source tree before retrying.

This is a defense-in-depth check, not a complete secret scanner. It can miss unfamiliar encodings, obfuscated credentials, binary secrets without a sensitive filename/extension, or values split across files. It can also flag a legitimate example containing a credential-like literal; replace the literal with a placeholder rather than bypassing the check. The script preserves edition sample profiles such as `profiles.example.yml`, but never copies local `profiles.yml` files.

The scaffolder does not rename dbt project identifiers or configure cloud resources; review the copied README and project config for the client before use. Do not put customer data or credentials into the source edition.

Scaffolded workspaces include `scripts/first_run_preflight.py`. After following the edition README and creating a local `.dbt/profiles.yml`, run `python scripts/first_run_preflight.py bigquery --workspace .` for BigQuery or `python scripts/first_run_preflight.py snowflake --workspace .` for Snowflake. This local check helps diagnose missing Python/dbt packages, lock files, profiles, or profile-path isolation; it does not authenticate to a warehouse.

## Build a distributable archive

```sh
python scripts/package_edition.py bigquery /path/outside/repo/de-starter-kit-bigquery.zip
python scripts/package_edition.py snowflake /path/outside/repo/de-starter-kit-snowflake.zip
cd /path/outside/repo && sha256sum -c de-starter-kit-bigquery.zip.sha256
unzip -l /path/outside/repo/de-starter-kit-bigquery.zip
```

The archiver rejects an existing output or checksum and writes outside the repository. It excludes `.ci/`, local profiles, `.env` files, common credential directories, key/certificate files, `.venv`, dbt targets/packages/logs, `.git`, and symlinks. It scans other included files and refuses to create an archive if it finds obvious credential payloads. It retains `profiles.example.yml`, edition docs, seeds, SQL, tests, and hash locks. Each archive contains a `MANIFEST.sha256` for payload files and has a companion ZIP checksum. Inspect the contents and verify the manifest before giving it to anyone.

The same detection limits and false-positive handling described for the scaffolder apply. Archive creation does not select a software license, establish data redistribution rights, or publish a release.
