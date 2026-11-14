#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
command -v git >/dev/null 2>&1 || { echo 'Git is required to verify the checkout state.' >&2; exit 2; }
if ! git -C "$repo_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo 'Release archive validation requires a Git checkout.' >&2
  exit 2
fi
if ! checkout_status="$(git -C "$repo_root" status --porcelain --untracked-files=normal)"; then
  echo 'Could not inspect Git checkout state; resolve repository access and retry.' >&2
  exit 2
fi
if [[ -n "$checkout_status" ]]; then
  echo 'Release archive validation requires a clean Git checkout; commit or remove local changes, then retry.' >&2
  exit 2
fi

python_bin="${PYTHON:-python3}"
command -v "$python_bin" >/dev/null 2>&1 || { echo 'Python 3 is required.' >&2; exit 2; }
command -v unzip >/dev/null 2>&1 || { echo 'unzip is required.' >&2; exit 2; }
command -v sha256sum >/dev/null 2>&1 || { echo 'sha256sum is required.' >&2; exit 2; }
command -v docker >/dev/null 2>&1 || { echo 'Docker is required for the archive secret scan; install Docker and ensure its CLI is on PATH.' >&2; exit 2; }
gitleaks_image='ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:b109bc5f8f76a38196a3e413704fc5b9e3c32360bce4e4b603bd6f45b3721dbb'

gitleaks_config="$repo_root/scripts/release_archive_gitleaks.toml"
test -s "$gitleaks_config" || { echo 'The repository Gitleaks config scripts/release_archive_gitleaks.toml is missing.' >&2; exit 2; }

temp_dir="$(mktemp -d)"
trap 'rm -rf "$temp_dir"' EXIT

# The scanner reads its rules only from the repository's pinned config, mounted
# outside the scanned tree, and an explicit empty ignore file. The scan source is
# the extraction directory, whose only entry is the edition root (enforced by the
# --archive member check), so Gitleaks' source-level .gitleaks.toml and
# .gitleaksignore lookups cannot be fed by archive contents either. Any such file
# inside an edition fails the path check below unless explicitly allowlisted.
# --ignore-gitleaks-allow stops inline "gitleaks:allow" comments hiding findings.
# scripts/validate_gitleaks_behavior.sh checks these flags against the real image.
scan_config_dir="$temp_dir/gitleaks-config"
mkdir -p "$scan_config_dir"
cp "$gitleaks_config" "$scan_config_dir/gitleaks.toml"
: > "$scan_config_dir/gitleaksignore"

for edition in bigquery snowflake; do
  archive="$temp_dir/de-starter-kit-$edition.zip"
  "$python_bin" "$repo_root/scripts/package_edition.py" "$edition" "$archive"
  (cd "$temp_dir" && sha256sum --check "$(basename "$archive").sha256")
  "$python_bin" "$repo_root/scripts/check_release_archive_paths.py" --archive "$archive" --expected-root "de-starter-kit-$edition"
  extract_dir="$temp_dir/extracted-$edition"
  mkdir -p "$extract_dir"
  unzip -q "$archive" -d "$extract_dir"
  root="$extract_dir/de-starter-kit-$edition"
  test -s "$root/MANIFEST.sha256"
  (cd "$extract_dir" && sha256sum --check "de-starter-kit-$edition/MANIFEST.sha256")
  "$python_bin" "$repo_root/scripts/check_release_archive_paths.py" "$root"
  if ! docker run --rm --network none \
    --volume "$extract_dir:/release:ro" --volume "$scan_config_dir:/scan-config:ro" \
    --workdir /release "$gitleaks_image" \
    dir --config /scan-config/gitleaks.toml --gitleaks-ignore-path /scan-config/gitleaksignore --ignore-gitleaks-allow \
    --redact --no-banner .; then
    echo 'Archive secret scan failed; review Docker output and confirm the pinned Gitleaks image can run.' >&2
    exit 1
  fi
  test -n "$(unzip -Z1 "$archive" | grep -F "de-starter-kit-$edition/")"
  echo "Validated $edition archive contents, manifest, and companion checksum."
done
