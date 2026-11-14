#!/usr/bin/env python3
"""Reject private configuration and generated state from an extracted release archive."""

from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from credential_safety import is_sensitive_path


FORBIDDEN_DIRECTORIES = {".ci", ".dbt", ".venv", "dbt_packages", "logs", "target"}
GITLEAKS_CONFIG_NAMES = {".gitleaks.toml", ".gitleaksignore"}
DEFAULT_GITLEAKS_ALLOWLIST = Path(__file__).resolve().with_name("release_archive_gitleaks_allowlist.txt")


def load_gitleaks_allowlist(path: Path) -> set[str]:
    """Return approved archive paths (top-level directory included), casefolded."""
    entries = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if entry and not entry.startswith("#"):
            entries.add(entry.strip("/").casefold())
    return entries


def is_unapproved_gitleaks_file(archive_path: str, allowlist: set[str]) -> bool:
    """Return True for a Gitleaks config or ignore file that the allowlist does not approve.

    ``archive_path`` is the member path including the archive's top-level directory.
    Names are compared case-insensitively so case-insensitive file systems cannot
    smuggle in a variant spelling.
    """
    normalized = archive_path.strip("/")
    name = normalized.rsplit("/", 1)[-1].casefold()
    return name in GITLEAKS_CONFIG_NAMES and normalized.casefold() not in allowlist


GITLEAKS_REFUSAL = (
    "Release archive contains a .gitleaks.toml or .gitleaksignore that is not approved in "
    "scripts/release_archive_gitleaks_allowlist.txt."
)


def is_forbidden_archive_path(relative_path: Path) -> bool:
    """Return True for private configuration or generated artifacts."""
    parts = tuple(part.lower() for part in relative_path.parts)
    return is_sensitive_path(relative_path) or any(
        part in FORBIDDEN_DIRECTORIES for part in parts
    )


def unapproved_gitleaks_members(archive: Path, allowlist: set[str]) -> list[str]:
    with ZipFile(archive) as bundle:
        names = bundle.namelist()
    return [name for name in names if is_unapproved_gitleaks_file(name, allowlist)]


def validate_zip_members(archive: Path, expected_root: str) -> bool:
    prefix = expected_root.rstrip("/") + "/"
    try:
        with ZipFile(archive) as bundle:
            members = bundle.infolist()
    except (OSError, BadZipFile):
        return False
    if not members:
        return False
    seen = set()
    for info in members:
        member = info.filename
        if "\\" in member or member.startswith("/") or not member.startswith(prefix):
            return False
        if stat.S_ISLNK(info.external_attr >> 16):
            return False
        relative = member[len(prefix):]
        if not relative and member == prefix:
            continue
        if relative.endswith("/"):
            relative = relative[:-1]
        parts = relative.split("/")
        if not parts or any(part in {"", ".", ".."} for part in parts):
            return False
        canonical = "/".join(parts).casefold()
        if canonical in seen:
            return False
        seen.add(canonical)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, nargs="?", help="extracted edition root to inspect")
    parser.add_argument("--archive", type=Path, help="ZIP archive to validate before extraction")
    parser.add_argument("--expected-root", help="required ZIP top-level directory when using --archive")
    parser.add_argument(
        "--gitleaks-allowlist",
        type=Path,
        default=DEFAULT_GITLEAKS_ALLOWLIST,
        help="file listing approved .gitleaks.toml/.gitleaksignore archive paths",
    )
    args = parser.parse_args()
    try:
        allowlist = load_gitleaks_allowlist(args.gitleaks_allowlist)
    except (OSError, UnicodeDecodeError):
        print("Gitleaks allowlist could not be read.", file=sys.stderr)
        return 2
    if args.archive is not None:
        if args.root is not None or not args.expected_root:
            parser.error("--archive requires --expected-root and cannot be combined with an extracted root")
        if not validate_zip_members(args.archive, args.expected_root):
            print("Release archive contains an invalid or out-of-root member path.", file=sys.stderr)
            return 1
        if unapproved_gitleaks_members(args.archive, allowlist):
            print(GITLEAKS_REFUSAL, file=sys.stderr)
            return 1
        return 0
    if args.root is None or args.expected_root is not None:
        parser.error("provide an extracted edition root, or use --archive with --expected-root")
    root = args.root
    if root.is_symlink() or not root.is_dir():
        print("Release archive root is missing or invalid.", file=sys.stderr)
        return 2
    try:
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if path.is_symlink() or is_forbidden_archive_path(relative):
                print("Forbidden local configuration or generated artifacts found in archive.", file=sys.stderr)
                return 1
            if is_unapproved_gitleaks_file(f"{root.name}/{relative.as_posix()}", allowlist):
                print(GITLEAKS_REFUSAL, file=sys.stderr)
                return 1
    except OSError:
        print("Release archive paths could not be inspected.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
