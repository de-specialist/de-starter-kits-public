#!/usr/bin/env bash
# Stage Markdown + credential-free dbt docs, then build the MkDocs Material site.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTENT="${ROOT}/website/content"
SITE="${ROOT}/website/site"
# Venv locations can be overridden (CI points them at restored cached venvs).
WEBSITE_VENV="${WEBSITE_VENV:-${ROOT}/website/.venv}"
DBT_BIGQUERY_VENV="${DBT_BIGQUERY_VENV:-${ROOT}/website/.venv-dbt-bigquery}"
DBT_SNOWFLAKE_VENV="${DBT_SNOWFLAKE_VENV:-${ROOT}/website/.venv-dbt-snowflake}"
PYTHON_BIN="${PYTHON_BIN:-}"
# DBT_DOCS=skip stages a placeholder instead of running dbt (CI pull requests
# that change no dbt docs input); any other value generates the dbt docs.
DBT_DOCS="${DBT_DOCS:-generate}"

log() {
  printf '==> %s\n' "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

resolve_python() {
  if [[ -n "${PYTHON_BIN}" ]]; then
    [[ -x "${PYTHON_BIN}" ]] || command -v "${PYTHON_BIN}" >/dev/null 2>&1 \
      || die "PYTHON_BIN=${PYTHON_BIN} not found"
    return
  fi
  if command -v python3.11 >/dev/null 2>&1; then
    PYTHON_BIN=python3.11
  elif command -v uv >/dev/null 2>&1; then
    PYTHON_BIN="$(uv python find 3.11 2>/dev/null || true)"
    [[ -n "${PYTHON_BIN}" ]] || die "Python 3.11 is required (install with: uv python install 3.11)"
  else
    die "Python 3.11 is required"
  fi
}

lock_digest() {
  sha256sum "$1" | cut -d' ' -f1
}

# A venv records the digest of the lock it was installed from, so it is reused
# only while the lock is unchanged (CI's cached venvs carry the same stamp).
venv_matches_lock() {
  local venv_dir="$1"
  local req_lock="$2"
  [[ -x "${venv_dir}/bin/python" && -f "${venv_dir}/.requirements-lock.sha256" ]] \
    && [[ "$(cat "${venv_dir}/.requirements-lock.sha256")" == "$(lock_digest "${req_lock}")" ]]
}

pip_install_hashes() {
  local venv_dir="$1"
  local req_lock="$2"
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "${venv_dir}/bin/python" --require-hashes -r "${req_lock}"
  else
    "${venv_dir}/bin/python" -m pip install --disable-pip-version-check --require-hashes -r "${req_lock}"
  fi
  lock_digest "${req_lock}" > "${venv_dir}/.requirements-lock.sha256"
}

ensure_website_venv() {
  local req_lock="${ROOT}/website/requirements-lock.txt"
  if venv_matches_lock "${WEBSITE_VENV}" "${req_lock}"; then
    log "Reusing website virtualenv (lock unchanged)"
    return
  fi
  if [[ ! -x "${WEBSITE_VENV}/bin/python" ]]; then
    log "Creating website virtualenv"
    if command -v uv >/dev/null 2>&1; then
      uv venv --python "${PYTHON_BIN}" "${WEBSITE_VENV}"
    else
      "${PYTHON_BIN}" -m venv "${WEBSITE_VENV}"
    fi
  fi
  log "Installing website dependencies (hash-locked)"
  pip_install_hashes "${WEBSITE_VENV}" "${req_lock}"
}

# Private pages (website/docs-visibility.toml or front matter) are never staged.
# Newline-separated repository-relative paths, filled by load_private_pages.
PRIVATE_PAGES=""

load_private_pages() {
  PRIVATE_PAGES="$("${PYTHON_BIN}" "${ROOT}/scripts/docs_visibility.py" --root "${ROOT}" list-private)" \
    || die "Could not determine private documentation pages"
  log "Skipping $(printf '%s' "${PRIVATE_PAGES}" | grep -c . || true) private page(s)"
}

# stage_md SRC DEST: copy one Markdown file unless it is private.
stage_md() {
  local src="$1" dest="$2"
  local rel="${src#"${ROOT}/"}"
  if printf '%s\n' "${PRIVATE_PAGES}" | grep -Fxq -- "${rel}"; then
    log "Not staging private page ${rel}"
    return 0
  fi
  cp "${src}" "${dest}"
}

# stage_md_dir SRC_DIR DEST_DIR: copy top-level *.md files that are not private.
stage_md_dir() {
  local src_dir="$1" dest_dir="$2" src
  while IFS= read -r -d '' src; do
    stage_md "${src}" "${dest_dir}/"
  done < <(find "${src_dir}" -maxdepth 1 -type f -name '*.md' -print0 | sort -z)
}

stage_markdown() {
  log "Staging Markdown into website/content"
  load_private_pages
  rm -rf "${CONTENT}"
  mkdir -p "${CONTENT}/docs" \
    "${CONTENT}/editions/Bigquery/docs" \
    "${CONTENT}/editions/Snowflake/docs"

  # Home page: keep README body; MkDocs expects index.md at docs_dir root.
  stage_md "${ROOT}/README.md" "${CONTENT}/index.md"
  stage_md "${ROOT}/CHANGELOG.md" "${CONTENT}/CHANGELOG.md"
  # The public contributor guide; AGENTS.md holds agent-facing conventions and
  # is not published.
  stage_md "${ROOT}/CONTRIBUTING.md" "${CONTENT}/CONTRIBUTING.md"

  # Shared guides (preserve docs/ relative links to editions/).
  stage_md_dir "${ROOT}/docs" "${CONTENT}/docs"
  # The root README is staged as index.md; point shared-guide links at it.
  find "${CONTENT}/docs" -maxdepth 1 -type f -name '*.md' \
    -exec sed -i -e 's|](\.\./README\.md|](../index.md|g' {} +

  # Migration overview linked from README and shared guides.
  if [[ -f "${ROOT}/migration/README.md" ]]; then
    mkdir -p "${CONTENT}/migration/mappings"
    stage_md "${ROOT}/migration/README.md" "${CONTENT}/migration/README.md"
    if [[ -d "${ROOT}/migration/mappings" ]]; then
      find "${ROOT}/migration/mappings" -maxdepth 1 -type f -name '*.json' \
        -exec cp {} "${CONTENT}/migration/mappings/" \;
      stage_md_dir "${ROOT}/migration/mappings" "${CONTENT}/migration/mappings"
    fi
  fi

  # README uses directory links; point them at staged edition overviews for MkDocs.
  sed -i \
    -e 's|](editions/Bigquery/)|](editions/Bigquery/README.md)|g' \
    -e 's|](editions/Snowflake/)|](editions/Snowflake/README.md)|g' \
    -e 's|](`editions/Bigquery/`)|](editions/Bigquery/README.md)|g' \
    -e 's|](`editions/Snowflake/`)|](editions/Snowflake/README.md)|g' \
    "${CONTENT}/index.md"

  # Edition guides and READMEs.
  stage_md "${ROOT}/editions/Bigquery/README.md" "${CONTENT}/editions/Bigquery/README.md"
  stage_md_dir "${ROOT}/editions/Bigquery/docs" "${CONTENT}/editions/Bigquery/docs"

  stage_md "${ROOT}/editions/Snowflake/README.md" "${CONTENT}/editions/Snowflake/README.md"
  stage_md_dir "${ROOT}/editions/Snowflake/docs" "${CONTENT}/editions/Snowflake/docs"

  # Optional Terraform READMEs so edition overview links resolve when present.
  if [[ -f "${ROOT}/editions/Bigquery/infra/terraform/README.md" ]]; then
    mkdir -p "${CONTENT}/editions/Bigquery/infra/terraform"
    stage_md "${ROOT}/editions/Bigquery/infra/terraform/README.md" \
      "${CONTENT}/editions/Bigquery/infra/terraform/README.md"
  fi
  if [[ -f "${ROOT}/editions/Snowflake/infra/terraform/README.md" ]]; then
    mkdir -p "${CONTENT}/editions/Snowflake/infra/terraform"
    stage_md "${ROOT}/editions/Snowflake/infra/terraform/README.md" \
      "${CONTENT}/editions/Snowflake/infra/terraform/README.md"
  fi

  stage_legal_pages
  write_dbt_catalog_pages

  "${PYTHON_BIN}" "${ROOT}/scripts/docs_visibility.py" --root "${ROOT}" check-staged "${CONTENT}" \
    || die "A private page was staged into website/content"
}

# Licence, notice, support, security, and decision records are linked from the
# README and shared guides with repository-relative paths. Stage them so those
# links resolve on the public site instead of pointing at the private repo.
stage_legal_pages() {
  mkdir -p "${CONTENT}/legal" "${CONTENT}/docs/decisions"
  local name
  for name in LICENSE NOTICE; do
    local dest="${CONTENT}/legal/$(printf '%s' "${name}" | tr '[:upper:]' '[:lower:]').md"
    {
      printf '# %s\n\n```text\n' "${name}"
      cat "${ROOT}/${name}"
      printf '```\n'
    } > "${dest}"
  done
  stage_md "${ROOT}/SUPPORT.md" "${CONTENT}/SUPPORT.md"
  stage_md "${ROOT}/SECURITY.md" "${CONTENT}/SECURITY.md"
  stage_md_dir "${ROOT}/docs/decisions" "${CONTENT}/docs/decisions"

  # LICENSE and NOTICE have no .md extension in the repository; point links at
  # the staged pages at any relative depth (LICENSE, ../LICENSE, ../../LICENSE).
  find "${CONTENT}" -type f -name '*.md' -exec sed -i \
    -e 's#](\(\(\.\./\)*\)LICENSE)#](\1legal/license.md)#g' \
    -e 's#](\(\(\.\./\)*\)NOTICE)#](\1legal/notice.md)#g' {} +
}

write_dbt_catalog_pages() {
  # Raw HTML is not rewritten by MkDocs: with directory URLs each catalog page is
  # served from <edition>/dbt-docs-catalog/, so the iframe climbs one level.
  cat > "${CONTENT}/editions/Bigquery/dbt-docs-catalog.md" <<'EOF'
# BigQuery dbt docs

Interactive dbt documentation for the BigQuery edition (project `retail_analytics`).

!!! note "Empty catalog in CI builds"
    Published builds run `dbt docs generate --empty-catalog --no-compile --static`
    with the placeholder `.ci` profile. The DAG and model docs come from the
    embedded manifest; warehouse column metadata is empty until you regenerate
    locally against a real profile:

    ```sh
    cd editions/Bigquery
    dbt docs generate --profiles-dir .dbt
    ```

<iframe
  src="../dbt-docs/index.html"
  title="BigQuery dbt docs"
  style="width:100%;height:80vh;border:1px solid var(--md-default-fg-color--lightest);border-radius:4px;"
></iframe>

[Open BigQuery dbt docs in a full page](dbt-docs/index.html){ target=_blank }
EOF

  cat > "${CONTENT}/editions/Snowflake/dbt-docs-catalog.md" <<'EOF'
# Snowflake dbt docs

Interactive dbt documentation for the Snowflake edition (project `reliable_pipeline`).

!!! note "Empty catalog in CI builds"
    Published builds run `dbt docs generate --empty-catalog --no-compile --static`
    with the placeholder `.ci` profile. The DAG and model docs come from the
    embedded manifest; warehouse column metadata is empty until you regenerate
    locally against a real profile (see `editions/Snowflake/docs/setup.md`).

<iframe
  src="../dbt-docs/index.html"
  title="Snowflake dbt docs"
  style="width:100%;height:80vh;border:1px solid var(--md-default-fg-color--lightest);border-radius:4px;"
></iframe>

[Open Snowflake dbt docs in a full page](dbt-docs/index.html){ target=_blank }
EOF
}

install_edition_dbt() {
  local req_lock="$1"
  local venv_dir="$2"
  if venv_matches_lock "${venv_dir}" "${req_lock}"; then
    log "Reusing dbt venv at ${venv_dir} (lock unchanged)"
    return
  fi
  if [[ ! -x "${venv_dir}/bin/python" ]]; then
    log "Creating dbt venv at ${venv_dir}"
    if command -v uv >/dev/null 2>&1; then
      uv venv --python "${PYTHON_BIN}" "${venv_dir}"
    else
      "${PYTHON_BIN}" -m venv "${venv_dir}"
    fi
  fi
  log "Installing dbt dependencies into ${venv_dir} (hash-locked)"
  pip_install_hashes "${venv_dir}" "${req_lock}"
}

generate_dbt_docs() {
  local project_dir="$1"
  local req_lock="$2"
  local venv_dir="$3"
  local dest_dir="$4"
  local profiles_dir="${project_dir}/.ci"

  [[ -f "${profiles_dir}/profiles.yml" ]] || die "Missing ${profiles_dir}/profiles.yml"
  install_edition_dbt "${req_lock}" "${venv_dir}"

  log "Generating dbt docs in ${project_dir}"
  (
    cd "${project_dir}"
    # Match CI placeholders so migration models that require env vars still parse.
    export BQ_PROJECT="${BQ_PROJECT:-ci-placeholder}"
    export BQ_DATASET="${BQ_DATASET:-ci_placeholder}"
    export MIGRATION_BRONZE_NAMESPACE="${MIGRATION_BRONZE_NAMESPACE:-starterkit_migration_bronze}"
    # The only full parse: it writes target/manifest.json, which docs generate
    # --no-compile does not write itself, and docs generate then reuses its
    # partial-parse state. --empty-catalog avoids warehouse credentials.
    "${venv_dir}/bin/dbt" parse --profiles-dir .ci
    # --no-compile avoids warehouse auth; --empty-catalog skips information_schema;
    # --static embeds manifest/catalog into static_index.html for portable hosting.
    "${venv_dir}/bin/dbt" docs generate --profiles-dir .ci --empty-catalog --no-compile --static
  )

  mkdir -p "${dest_dir}"
  for name in manifest.json catalog.json; do
    [[ -f "${project_dir}/target/${name}" ]] || die "Missing ${project_dir}/target/${name} after docs generate"
    cp "${project_dir}/target/${name}" "${dest_dir}/${name}"
  done
  if [[ -f "${project_dir}/target/static_index.html" ]]; then
    cp "${project_dir}/target/static_index.html" "${dest_dir}/index.html"
  elif [[ -f "${project_dir}/target/index.html" ]]; then
    cp "${project_dir}/target/index.html" "${dest_dir}/index.html"
  else
    die "Missing dbt docs HTML in ${project_dir}/target"
  fi

  # dbt's static bundle can retain a webpack favicon expression instead of a
  # real asset path. The bundle is embedded without its asset tree, so remove
  # that invalid reference to avoid a browser-side 404. No-op when absent.
  "${PYTHON_BIN}" - "${dest_dir}/index.html" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
html = path.read_text(encoding="utf-8")
invalid_favicon = '<link rel="shortcut icon" href="${require(\'./assets/favicons/favicon.ico\')}"/>'
path.write_text(html.replace(invalid_favicon, ""), encoding="utf-8")
PY
}

# Pull request builds that change no dbt input skip dbt entirely; the catalog
# page still links a valid file so the strict MkDocs build passes. Never
# published: only push and manual builds upload the site, and they generate.
stage_dbt_docs_placeholder() {
  local dest_dir="$1"
  log "Skipping dbt docs (DBT_DOCS=skip); staging a placeholder in ${dest_dir}"
  mkdir -p "${dest_dir}"
  cat > "${dest_dir}/index.html" <<'EOF'
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>dbt docs not built</title></head>
<body><p>dbt docs were not generated for this build because no dbt input changed.</p></body></html>
EOF
}

build_mkdocs() {
  log "Building MkDocs site"
  rm -rf "${SITE}"
  (
    cd "${ROOT}/website"
    NO_MKDOCS_2_WARNING=1 "${WEBSITE_VENV}/bin/mkdocs" build --strict
  )
}

main() {
  resolve_python
  log "Using ${PYTHON_BIN}"
  stage_markdown
  if [[ "${DOCS_STAGE_ONLY:-}" == "1" ]]; then
    log "DOCS_STAGE_ONLY=1: staged Markdown only, skipping dbt docs and MkDocs"
    return 0
  fi
  ensure_website_venv

  # Ephemeral venvs under website/ (gitignored via website/.venv; use sibling dirs).
  local bq_venv="${DBT_BIGQUERY_VENV}"
  local sf_venv="${DBT_SNOWFLAKE_VENV}"

  if [[ "${DBT_DOCS}" == skip ]]; then
    stage_dbt_docs_placeholder "${CONTENT}/editions/Bigquery/dbt-docs"
    stage_dbt_docs_placeholder "${CONTENT}/editions/Snowflake/dbt-docs"
  else
    generate_dbt_docs \
      "${ROOT}/editions/Bigquery" \
      "${ROOT}/editions/Bigquery/requirements-lock.txt" \
      "${bq_venv}" \
      "${CONTENT}/editions/Bigquery/dbt-docs"

    generate_dbt_docs \
      "${ROOT}/editions/Snowflake/kit" \
      "${ROOT}/editions/Snowflake/requirements-lock.txt" \
      "${sf_venv}" \
      "${CONTENT}/editions/Snowflake/dbt-docs"
  fi

  build_mkdocs
  log "Site written to ${SITE}"
}

main "$@"
