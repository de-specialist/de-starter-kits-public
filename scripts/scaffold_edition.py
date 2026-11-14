#!/usr/bin/env python3
"""Copy one dbt edition into a new client workspace outside this repository, offline.

``scaffold_edition.py {bigquery,snowflake} DESTINATION [--dry-run]``

The copy excludes local state (profiles, virtualenvs, dbt targets/packages/logs, CI profiles),
credential-like paths, symlinks, and special files. It is assembled in a private staging
directory beside the destination, scanned for credential payloads, and then published with one
atomic no-replace directory rename, so a destination is either absent or complete. ``--dry-run``
lists the planned files and writes nothing. No profile, credential variable, network, or
warehouse is read or contacted.

The source-specific Silver model scaffolder (backlog 145) is separate: ``migration/scaffold_silver.py``.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from credential_safety import credential_findings, is_sensitive_path


EDITION_DIRECTORIES = {"bigquery": "Bigquery", "snowflake": "Snowflake"}

# ---------------------------------------------------------------------------------------------
# Atomic no-replace directory rename (shared by both commands)
# ---------------------------------------------------------------------------------------------

_AT_FDCWD = -100
_LINUX_RENAME_NOREPLACE = 1
_DARWIN_RENAME_EXCL = 0x00000004


class AtomicPublishUnavailable(OSError):
    """No atomic no-replace directory rename primitive is available here."""


def _libc():
    try:
        return ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None


def atomic_publish_available() -> bool:
    """Return whether this platform exposes a no-replace rename primitive (no write performed)."""
    libc = _libc()
    if libc is None:
        return False
    if sys.platform.startswith("linux"):
        return hasattr(libc, "renameat2")
    if sys.platform == "darwin":
        return hasattr(libc, "renamex_np")
    return False


def rename_no_replace(source: Path, target: Path) -> None:
    """Rename ``source`` to ``target`` atomically, failing if ``target`` exists in any form.

    There is no fallback to a replacing rename or a copy: an unavailable primitive raises
    ``AtomicPublishUnavailable`` and other failures raise ``OSError`` with the platform errno.
    """
    libc = _libc()
    old, new = os.fsencode(source), os.fsencode(target)
    if libc is not None and sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = function(_AT_FDCWD, old, _AT_FDCWD, new, _LINUX_RENAME_NOREPLACE)
    elif libc is not None and sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        function = libc.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = function(old, new, _DARWIN_RENAME_EXCL)
    else:
        raise AtomicPublishUnavailable(errno.ENOSYS, "atomic no-replace rename is unavailable")
    if result != 0:
        code = ctypes.get_errno()
        if code in {errno.EINVAL, errno.ENOSYS, getattr(errno, "ENOTSUP", -1), getattr(errno, "EOPNOTSUPP", -1)}:
            raise AtomicPublishUnavailable(code, "atomic no-replace rename is unavailable")
        raise OSError(code, os.strerror(code))


# ---------------------------------------------------------------------------------------------
# Edition workspace copier
# ---------------------------------------------------------------------------------------------

IGNORED_NAMES = {
    ".ci",
    ".dbt",
    ".venv",
    "target",
    "dbt_packages",
    "logs",
    "__pycache__",
}


def should_exclude(relative: Path, candidate: Path) -> bool:
    if candidate.is_symlink():
        return True
    try:
        mode = candidate.lstat().st_mode
    except OSError:
        return True
    # FIFOs, sockets, and devices are never copied (reading a FIFO would block).
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        return True
    return any(part in IGNORED_NAMES for part in relative.parts) or is_sensitive_path(relative)


def planned_files(source: Path) -> list[str]:
    """Return the relative POSIX paths of every file the copier would include, sorted."""
    files: list[str] = []
    for directory, names, filenames in os.walk(source, followlinks=False):
        current = Path(directory)
        kept = []
        for name in sorted(names):
            path = current / name
            if not should_exclude(path.relative_to(source), path):
                kept.append(name)
        names[:] = kept
        for name in filenames:
            path = current / name
            relative = path.relative_to(source)
            if not should_exclude(relative, path):
                files.append(relative.as_posix())
    return sorted(files)


def find_credential_payloads(source: Path) -> list[str]:
    findings: list[str] = []
    for relative in planned_files(source):
        markers = credential_findings(source / relative)
        if markers:
            findings.append(f"{relative} ({', '.join(markers)})")
    return findings


def make_ignore(source: Path):
    def ignore(directory: str, names: list[str]) -> set[str]:
        current = Path(directory)
        ignored: set[str] = set()
        for name in names:
            path = current / name
            relative = path.relative_to(source)
            if should_exclude(relative, path):
                ignored.add(name)
        return ignored

    return ignore


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def copy_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="scaffold_edition.py",
        description="Copy one dbt edition into a new client workspace outside this repository.",
    )
    parser.add_argument("edition", choices=tuple(EDITION_DIRECTORIES))
    parser.add_argument("destination", type=Path, help="new folder outside this repository")
    parser.add_argument("--dry-run", action="store_true", help="list the planned files and write nothing")
    args = parser.parse_args(argv)

    # Resolved at call time (not import time) so the copier always uses its own checkout.
    repo_root = Path(__file__).resolve().parents[1]
    source = repo_root / "editions" / EDITION_DIRECTORIES[args.edition]
    lexical_destination = Path(os.path.abspath(args.destination.expanduser()))

    if source.is_symlink() or not source.is_dir():
        parser.error("edition source not found or is a symlink")
    if os.path.lexists(lexical_destination):
        parser.error("destination already exists; choose a new empty path")
    destination = lexical_destination.parent.resolve() / lexical_destination.name
    resolved_repo = repo_root.resolve()
    if _is_within(lexical_destination, resolved_repo) or _is_within(destination, resolved_repo):
        parser.error("destination must be outside the starter-kit repository")
    if os.path.lexists(destination):
        parser.error("destination already exists; choose a new empty path")

    credential_payloads = find_credential_payloads(source)
    if credential_payloads:
        parser.error(
            "refusing to scaffold credential-like content from included files: "
            + "; ".join(credential_payloads)
        )

    support_script = repo_root / "scripts" / "first_run_preflight.py"
    if not support_script.is_file() or support_script.is_symlink():
        parser.error("support script scripts/first_run_preflight.py is missing")

    if args.dry_run:
        plan = planned_files(source) + ["scripts/first_run_preflight.py"]
        print(json.dumps({
            "status": "plan_only",
            "edition": args.edition,
            "writes_performed": False,
            "planned_files": sorted(set(plan)),
        }, indent=2))
        return 0

    if not atomic_publish_available():
        parser.error("atomic no-replace directory rename is unavailable on this platform; nothing was written")

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging_root = Path(tempfile.mkdtemp(prefix=f".{destination.name}.scaffold-", dir=destination.parent))
    except OSError:
        parser.error("could not create a staging folder beside the destination; nothing was written")
    try:
        staged = staging_root / "workspace"
        try:
            shutil.copytree(source, staged, ignore=make_ignore(source), symlinks=True)
            support_directory = staged / "scripts"
            support_directory.mkdir(exist_ok=True)
            shutil.copy2(support_script, support_directory / support_script.name)
        except OSError:
            parser.error("could not stage the workspace copy; nothing was published")
        # Scan the exact bytes about to be published, not only the source tree.
        staged_findings = find_credential_payloads(staged)
        if staged_findings:
            parser.error(
                "refusing to scaffold credential-like content from included files: "
                + "; ".join(staged_findings)
            )
        try:
            rename_no_replace(staged, destination)
        except AtomicPublishUnavailable:
            parser.error("atomic no-replace directory rename is unavailable here; nothing was published")
        except OSError as error:
            if error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                parser.error("destination appeared while copying; it was left unchanged")
            if error.errno == errno.EXDEV:
                parser.error("staging and destination are on different filesystems; nothing was published")
            parser.error("could not publish the workspace; nothing was published")
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    print(f"Created {args.edition} edition at {destination}")
    print("Review the copied README, configure a local profile, then run scripts/first_run_preflight.py before dbt.")
    return 0


def main(argv: list[str] | None = None) -> int:
    return copy_main(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
