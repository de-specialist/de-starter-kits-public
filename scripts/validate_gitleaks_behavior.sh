#!/usr/bin/env bash

set -euo pipefail

image='ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:b109bc5f8f76a38196a3e413704fc5b9e3c32360bce4e4b603bd6f45b3721dbb'
command -v docker >/dev/null 2>&1 || { echo 'Docker is required for the Gitleaks smoke check.' >&2; exit 2; }

# Pull up front so registry, network, or daemon failures are reported as such
# instead of being mistaken for scanner findings later.
if ! docker pull --quiet "$image" >/dev/null; then
  echo 'Could not pull the pinned Gitleaks image; check Docker and registry access.' >&2
  exit 2
fi

fixture_dir="$(mktemp -d)"
trap 'rm -rf "$fixture_dir"' EXIT
mkdir -p "$fixture_dir/safe" "$fixture_dir/suspect" "$fixture_dir/history"
cat >"$fixture_dir/safe/example.env" <<'EOF'
API_TOKEN=${API_TOKEN}
password=replace_me
EOF

# Synthetic GitHub-PAT-shaped fixtures. They must look random enough to be
# reported: the github-pat rule requires Shannon entropy >= 3 (a repeated letter
# is ignored), and Gitleaks' default filtering also ignores obvious sequential
# alphabet runs such as a plain slice of the alphabet below. So each fixture
# walks the fixed alphabet with stride 17 (coprime with 62), which is
# deterministic, high-entropy, and non-sequential. Built at runtime so this
# source file never contains a token-shaped literal. Generated only in a
# temporary directory; never echo or persist them.
alnum='abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
synthetic_pat() {
  local offset="$1" body='' i
  for ((i = 0; i < 36; i++)); do
    body+="${alnum:$(((offset + i * 17) % ${#alnum})):1}"
  done
  printf 'ghp_%s' "$body"
}
synthetic_token="$(synthetic_pat 0)"
printf 'token=%s\n' "$synthetic_token" >"$fixture_dir/suspect/fixture.txt"
unset synthetic_token

scan_log="$fixture_dir/scan.log"

# Runs Gitleaks and prints its exit status: 0 = clean, 1 = leaks found,
# anything else = Docker or scanner error (125-127 come from Docker itself).
run_gitleaks() {
  local status=0
  docker run --rm --volume "$fixture_dir:/fixture:ro" "$image" \
    "$@" --redact --no-banner --exit-code 1 >/dev/null 2>"$scan_log" || status=$?
  echo "$status"
}

# Fails with a distinct message when the scanner itself did not run cleanly.
require_scanner_ran() {
  local status="$1" label="$2"
  if [[ "$status" -ne 0 && "$status" -ne 1 ]]; then
    echo "Gitleaks $label scan did not complete (exit code $status); Docker or scanner error:" >&2
    sed 's/^/  /' "$scan_log" >&2
    exit 2
  fi
}

expect_clean() {
  local label="$1" status
  shift
  status="$(run_gitleaks "$@")"
  require_scanner_ran "$status" "$label"
  if [[ "$status" -ne 0 ]]; then
    echo "Gitleaks flagged the known-safe $label fixture; review scanner behavior." >&2
    exit 1
  fi
}

expect_leak() {
  local label="$1" status
  shift
  status="$(run_gitleaks "$@")"
  require_scanner_ran "$status" "$label"
  if [[ "$status" -ne 1 ]]; then
    echo "Gitleaks did not flag the synthetic token in the $label fixture." >&2
    exit 1
  fi
}

expect_clean 'placeholder' dir /fixture/safe
expect_leak 'file' dir /fixture/suspect

# Put a synthetic token in a committed revision, then remove it from the working tree.
# The Git-history scan must still find it while the directory scan remains clean.
git -C "$fixture_dir/history" init -q
git -C "$fixture_dir/history" config user.name 'Gitleaks Smoke Test'
git -C "$fixture_dir/history" config user.email 'gitleaks-smoke@example.invalid'
synthetic_token="$(synthetic_pat 31)"
printf 'token=%s\n' "$synthetic_token" >"$fixture_dir/history/committed.txt"
unset synthetic_token
git -C "$fixture_dir/history" add committed.txt
git -C "$fixture_dir/history" commit -q -m 'Add synthetic secret fixture'
rm "$fixture_dir/history/committed.txt"

expect_clean 'Git-history working tree' dir /fixture/history
expect_leak 'Git-history' git /fixture/history

# Release archive scan flags (keep in sync with scripts/validate_release_archives.sh).
# An edition root inside the scanned directory ships a permissive .gitleaks.toml,
# a matching .gitleaksignore, and an inline gitleaks:allow marker; none of them
# may hide the synthetic token when the repository config is passed explicitly.
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
release_edition="$fixture_dir/release/de-starter-kit-fixture"
mkdir -p "$release_edition" "$fixture_dir/scan-config"
cp "$repo_root/scripts/release_archive_gitleaks.toml" "$fixture_dir/scan-config/gitleaks.toml"
: >"$fixture_dir/scan-config/gitleaksignore"
printf '[extend]\nuseDefault = true\n\n[[allowlists]]\npaths = ['"'"'.*'"'"']\n' >"$release_edition/.gitleaks.toml"
synthetic_token="$(synthetic_pat 7)"
printf 'token=%s # gitleaks:allow\n' "$synthetic_token" >"$release_edition/allowed.txt"
unset synthetic_token
for path in allowed.txt de-starter-kit-fixture/allowed.txt /fixture/release/de-starter-kit-fixture/allowed.txt; do
  printf '%s:github-pat:1\n' "$path"
done >"$release_edition/.gitleaksignore"
release_flags=(
  --config /fixture/scan-config/gitleaks.toml
  --gitleaks-ignore-path /fixture/scan-config/gitleaksignore
  --ignore-gitleaks-allow
)

expect_clean 'release inline-allow without strict flag' dir "${release_flags[@]:0:4}" /fixture/release
expect_leak 'release archive' dir "${release_flags[@]}" /fixture/release

# Defence in depth: even a permissive .gitleaks.toml at the scan root (which the
# archive member check keeps out) must lose to the explicit --config. Gitleaks
# also reads a .gitleaksignore at the scan root regardless of
# --gitleaks-ignore-path, so that file is kept out only by the archive check.
mkdir -p "$fixture_dir/release-root"
cp "$release_edition/.gitleaks.toml" "$fixture_dir/release-root/.gitleaks.toml"
synthetic_token="$(synthetic_pat 11)"
printf 'token=%s\n' "$synthetic_token" >"$fixture_dir/release-root/token.txt"
unset synthetic_token
expect_clean 'scan-root config without --config' dir /fixture/release-root
expect_leak 'scan-root config with --config' dir "${release_flags[@]}" /fixture/release-root

echo 'Gitleaks smoke check passed: safe placeholders passed and synthetic tokens were detected in files, Git history, and a release tree that tries to weaken its own scan.'
