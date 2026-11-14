#!/usr/bin/env python3
"""Print a read-only metadata inventory of migration pipeline state and manifests."""

from __future__ import annotations

import json
import argparse
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path


MIGRATION_ROOT = Path(__file__).resolve().parent
INVENTORY_ROOTS = (
    ("pipeline_state", MIGRATION_ROOT / ".dlt" / "pipelines"),
    ("run_manifests", MIGRATION_ROOT / "run-manifests"),
)


def _metadata(path: Path, relative_path: str, kind: str, info, now: datetime, *, size_bytes: int | None = None) -> dict[str, object]:
    modified = datetime.fromtimestamp(info.st_mtime, timezone.utc)
    warnings: list[str] = []
    owner_uid: int | None = None
    owner_matches: bool | None = None
    mode: str | None = None
    owner_only: bool | None = None
    if os.name == "posix" and hasattr(os, "geteuid"):
        owner_uid = info.st_uid
        owner_matches = info.st_uid == os.geteuid()
        mode_bits = stat.S_IMODE(info.st_mode)
        mode = f"{mode_bits:04o}"
        owner_only = (mode_bits & 0o077) == 0
        if not owner_matches:
            warnings.append("not_owned_by_current_user")
        if not owner_only:
            warnings.append("group_or_other_permissions_present")
    else:
        warnings.append("ownership_and_permission_check_unavailable")
    if kind == "symlink":
        warnings.append("symlink_not_followed")
    return {
        "path": relative_path,
        "kind": kind,
        "size_bytes": size_bytes,
        "modified_at_utc": modified.isoformat(),
        "age_seconds": max(0, int((now - modified).total_seconds())),
        "mode_octal": mode,
        "owner_uid": owner_uid,
        "owner_matches_current_user": owner_matches,
        "owner_only_permissions": owner_only,
        "warnings": warnings,
        "file_contents_read": False,
    }


def _walk(path: Path, relative_path: str, now: datetime) -> tuple[list[dict[str, object]], int | None]:
    entries: list[dict[str, object]] = []
    # Each directory aggregate tracks the newest observed mtime and total regular-file
    # bytes. Explicit enter/exit frames keep deeply nested state trees off Python's stack.
    aggregates: dict[int, dict[str, object]] = {}
    stack: list[tuple[Path, str, int | None, bool]] = [(path, relative_path, None, False)]

    def propagate(parent_index: int | None, modified: float | None, size: int, complete: bool = True) -> None:
        if parent_index is not None:
            aggregate = aggregates[parent_index]
            if modified is not None:
                aggregate["mtime"] = max(float(aggregate["mtime"]), modified)
            aggregate["size"] = int(aggregate["size"]) + size
            aggregate["complete"] = bool(aggregate["complete"]) and complete

    while stack:
        current_path, current_relative, parent_index, exiting = stack.pop()
        if exiting:
            index = parent_index
            assert index is not None
            aggregate = aggregates.pop(index)
            modified = float(aggregate["mtime"])
            complete = bool(aggregate["complete"])
            entries[index]["modified_at_utc"] = datetime.fromtimestamp(modified, timezone.utc).isoformat() if complete else None
            entries[index]["age_seconds"] = max(0, int(now.timestamp() - modified)) if complete else None
            partial_size = int(aggregate["size"])
            entries[index]["size_bytes"] = partial_size if complete else None
            parent = aggregate.get("parent")
            propagate(parent if isinstance(parent, int) and parent >= 0 else None, modified, partial_size, complete)
            continue

        try:
            info = current_path.lstat()
        except OSError as error:
            entries.append({
                "path": current_relative,
                "kind": "unavailable",
                "size_bytes": None,
                "modified_at_utc": None,
                "age_seconds": None,
                "mode_octal": None,
                "owner_uid": None,
                "owner_matches_current_user": None,
                "owner_only_permissions": None,
                "warnings": [f"metadata_unavailable:{type(error).__name__}"],
                "file_contents_read": False,
            })
            # An unknown child prevents a trustworthy directory age from being established.
            propagate(parent_index, None, 0, complete=False)
            continue

        if stat.S_ISLNK(info.st_mode):
            entries.append(_metadata(current_path, current_relative, "symlink", info, now))
            propagate(parent_index, info.st_mtime, 0)
        elif stat.S_ISREG(info.st_mode):
            entries.append(_metadata(current_path, current_relative, "file", info, now, size_bytes=info.st_size))
            propagate(parent_index, info.st_mtime, info.st_size)
        elif not stat.S_ISDIR(info.st_mode):
            entries.append(_metadata(current_path, current_relative, "special", info, now))
            propagate(parent_index, info.st_mtime, 0)
        else:
            index = len(entries)
            entries.append(_metadata(current_path, current_relative, "directory", info, now, size_bytes=0))
            aggregates[index] = {"mtime": info.st_mtime, "size": 0, "parent": parent_index, "complete": True}
            try:
                with os.scandir(current_path) as children:
                    child_paths = sorted(
                        (Path(child.path) for child in children),
                        key=lambda child: (child.name.casefold(), child.name),
                    )
            except OSError as error:
                entries[index]["warnings"].append(f"directory_scan_unavailable:{type(error).__name__}")
                aggregate = aggregates.pop(index)
                entries[index]["modified_at_utc"] = None
                entries[index]["age_seconds"] = None
                entries[index]["size_bytes"] = None
                propagate(parent_index, float(aggregate["mtime"]), 0, complete=False)
                continue
            stack.append((current_path, current_relative, index, True))
            for child_path in reversed(child_paths):
                child_relative = f"{current_relative}/{child_path.name}"
                stack.append((child_path, child_relative, index, False))

    if entries and entries[0].get("kind") == "directory":
        root_size = entries[0].get("size_bytes")
        total_bytes = root_size if isinstance(root_size, int) else None
    else:
        total_bytes = None
    return entries, total_bytes


def build_inventory(older_than_days: int = 30) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    entries: list[dict[str, object]] = []
    roots: list[dict[str, object]] = []
    for label, root in INVENTORY_ROOTS:
        relative_path = root.relative_to(MIGRATION_ROOT).as_posix()
        root_entries, total_bytes = _walk(root, relative_path, now)
        entries.extend(root_entries)
        roots.append({"category": label, "path": relative_path, "size_bytes": total_bytes})
    return {
        "inventory_version": 1,
        "generated_at_utc": now.isoformat(),
        "read_only": True,
        "file_contents_read": False,
        "cleanup_performed": False,
        "retention_preview": {
            "older_than_days": older_than_days,
            "candidate_paths": sorted(
                entry["path"] for entry in entries
                if entry.get("kind") in {"file", "directory"}
                and entry.get("path") not in {root.relative_to(MIGRATION_ROOT).as_posix() for _, root in INVENTORY_ROOTS}
                and isinstance(entry.get("age_seconds"), int)
                and entry["age_seconds"] >= older_than_days * 86400
            ),
            "deletion_supported": False,
        },
        "roots": roots,
        "entries": entries,
        "limitations": [
            "Sizes and timestamps come from filesystem metadata; file contents are not opened or inspected.",
            "Age is measured from the inventory timestamp and is intended only for retention planning.",
            "Permission and ownership warnings are reported only where the current platform exposes those fields.",
            "A missing or inaccessible path is reported as metadata unavailable and is not changed.",
        ],
    }


def main() -> int:
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--older-than-days", type=int, default=30,
                            help="Mark older metadata paths as retention review candidates (default: 30).")
        args = parser.parse_args()
        if args.older_than_days < 0:
            raise ValueError("--older-than-days must be zero or greater.")
        print(json.dumps(build_inventory(args.older_than_days), indent=2))
        return 0
    except Exception as error:
        print(f"State inventory stopped ({type(error).__name__}); no files were changed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
