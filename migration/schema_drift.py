#!/usr/bin/env python3
"""Field-level schema drift review between a reviewed baseline and a current source schema.

The snapshot compared here is metadata only: table names, ordered column names, and
normalized type families. Row values, row counts, and raw mapping rules are never read
or emitted. The snapshot is the exact payload hashed by the typed schema fingerprint, so a
baseline snapshot is verified against its recorded fingerprints before it is trusted.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from bisect import bisect_left
from hashlib import sha256
from pathlib import Path

from _private_json import read_private_json


SNAPSHOT_ALGORITHM = "table-ordered-column-name-type-family-v1"
DRIFT_ALGORITHM = "schema-drift-added-removed-reordered-retyped-v1"
SCHEMA_CONFIRMATION_ALGORITHM = "sha256-schema-and-mapping-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_TABLES = 1_000
_MAX_COLUMNS_PER_TABLE = 4_096
_MAX_NAME_LENGTH = 1_024


class SchemaBaselineError(ValueError):
    """The supplied baseline cannot be used as reviewed schema evidence."""


def type_family(value: object) -> str:
    """Normalize common SQLAlchemy type labels; unknown dialect types stay explicit."""
    label = str(value or "").strip().casefold()
    if label.startswith("csv source field text"):
        return "csv_untyped_text"
    if "bool" in label:
        return "boolean"
    if "timestamp" in label or "datetime" in label:
        return "timestamp"
    if label == "date" or label.startswith("date("):
        return "date"
    if label == "time" or label.startswith("time("):
        return "time"
    if re.search(r"(^|[^a-z])(tinyint|smallint|bigint|integer|int)([^a-z]|$)", label):
        return "integer"
    if any(token in label for token in ("decimal", "numeric", "number")):
        return "numeric"
    if any(token in label for token in ("double", "float", "real")):
        return "floating"
    if any(token in label for token in ("binary", "blob", "bytea", "varbinary")):
        return "binary"
    if "json" in label:
        return "json"
    if any(token in label for token in ("char", "text", "string", "clob")):
        return "text"
    return "other:" + (re.sub(r"\s+", "_", label)[:64] or "unknown")


def schema_snapshot(inventory: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return table/ordered-column/type-family metadata for an inventory; never row values.

    Live inventories may carry ``str`` subclasses (SQLAlchemy reflects names as
    ``quoted_name``). They are converted to exact ``str`` here so the snapshot passes
    the exact-type checks in ``validate_snapshot``; the text, and so every
    fingerprint, is unchanged. Deserialized baselines are not converted.
    """
    snapshot = []
    for item in inventory:
        table = item.get("table")
        if not isinstance(table, str):
            continue
        columns = [
            {"name": str(column["name"]), "type_family": type_family(column.get("type"))}
            for column in item.get("columns", [])
            if isinstance(column, dict) and isinstance(column.get("name"), str)
        ]
        snapshot.append({"table": str(table), "columns": columns})
    snapshot.sort(key=lambda entry: entry["table"].casefold())
    return snapshot


_UNENCODABLE_TEXT = "Schema metadata contains text that is not valid Unicode (for example a lone surrogate)."


def _is_utf8_text(value: str) -> bool:
    """True when the text encodes as UTF-8; lone surrogates from JSON escapes do not."""
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _digest(value: object) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeError:
        encoded = None
    if encoded is None:
        # Raised outside the handler so neither the message, cause, nor context carries the
        # offending (possibly private) schema name. Live-inventory fingerprints reach this
        # backstop too; the shared message says "Schema metadata" for that reason.
        raise SchemaBaselineError(_UNENCODABLE_TEXT)
    return sha256(encoded).hexdigest()


def snapshot_name_fingerprint(snapshot: list[dict[str, object]]) -> str:
    """Hash table and ordered column names (algorithm sha256-table-and-ordered-column-names-v1)."""
    return _digest([
        {"table": entry["table"], "columns": [column["name"] for column in entry["columns"]]}
        for entry in snapshot
    ])


def snapshot_typed_fingerprint(snapshot: list[dict[str, object]]) -> str:
    """Hash names plus type families (algorithm sha256-table-column-type-families-v1)."""
    return _digest(snapshot)


def schema_confirmation_fingerprint(name_fingerprint: str, typed_fingerprint: str, mapping_fingerprint: str) -> str:
    """Bind ordered names, available source type families, and mappings into one review value."""
    payload = (
        f"{SCHEMA_CONFIRMATION_ALGORITHM}\0{name_fingerprint}\0"
        f"{typed_fingerprint}\0{mapping_fingerprint}"
    ).encode("ascii")
    return sha256(payload).hexdigest()


def _is_bounded_text(value: object, *, allow_empty: bool = True) -> bool:
    return (
        type(value) is str
        and (allow_empty or bool(value))
        and len(value) <= _MAX_NAME_LENGTH
        and _is_utf8_text(value)
    )


def validate_snapshot(value: object) -> list[dict[str, object]]:
    """Check a snapshot's exact shape and bounds; reject anything that is not schema metadata.

    Containers and text must be the exact JSON types (no subclasses), and text must be valid
    Unicode so fingerprinting cannot fail with an error that echoes a schema name.
    """
    if type(value) is not list or len(value) > _MAX_TABLES:
        raise SchemaBaselineError("Schema snapshot must be a bounded list of tables.")
    seen_tables: set[str] = set()
    for entry in value:
        if type(entry) is not dict or set(entry) != {"table", "columns"}:
            raise SchemaBaselineError("Schema snapshot tables must contain only table and columns.")
        table, columns = entry["table"], entry["columns"]
        if not _is_bounded_text(table, allow_empty=False):
            raise SchemaBaselineError("Schema snapshot table names must be bounded text.")
        if table.casefold() in seen_tables:
            raise SchemaBaselineError("Schema snapshot table names must be unique ignoring case.")
        seen_tables.add(table.casefold())
        if type(columns) is not list or len(columns) > _MAX_COLUMNS_PER_TABLE:
            raise SchemaBaselineError("Schema snapshot columns must be a bounded list.")
        for column in columns:
            if (
                type(column) is not dict
                or set(column) != {"name", "type_family"}
                or not all(_is_bounded_text(column[key]) for key in column)
            ):
                raise SchemaBaselineError("Schema snapshot columns must contain only bounded name and type_family text.")
    return value


def _occurrence_keys(columns: list[dict[str, object]]) -> list[tuple[str, int]]:
    """Key columns by exact name plus occurrence so repeated names never collapse."""
    counts: dict[str, int] = {}
    keys = []
    for column in columns:
        name = column["name"]
        counts[name] = counts.get(name, 0) + 1
        keys.append((name, counts[name]))
    return keys


def _stable_indices(sequence: list[int]) -> set[int]:
    """Indices of one longest increasing subsequence: the columns that kept their relative order."""
    tails: list[int] = []
    tail_indices: list[int] = []
    previous: list[int] = [-1] * len(sequence)
    for index, value in enumerate(sequence):
        position = bisect_left(tails, value)
        if position == len(tails):
            tails.append(value)
            tail_indices.append(index)
        else:
            tails[position] = value
            tail_indices[position] = index
        previous[index] = tail_indices[position - 1] if position else -1
    stable: set[int] = set()
    cursor = tail_indices[-1] if tail_indices else -1
    while cursor >= 0:
        stable.add(cursor)
        cursor = previous[cursor]
    return stable


def _compare_columns(baseline: list[dict[str, object]], current: list[dict[str, object]]) -> dict[str, list]:
    baseline_keys = _occurrence_keys(baseline)
    current_keys = _occurrence_keys(current)
    baseline_at = {key: index for index, key in enumerate(baseline_keys)}
    current_at = {key: index for index, key in enumerate(current_keys)}

    added = [
        {"name": current[index]["name"], "position": index + 1, "type_family": current[index]["type_family"]}
        for index, key in enumerate(current_keys) if key not in baseline_at
    ]
    removed = [
        {"name": baseline[index]["name"], "position": index + 1, "type_family": baseline[index]["type_family"]}
        for index, key in enumerate(baseline_keys) if key not in current_at
    ]
    common = [key for key in baseline_keys if key in current_at]
    stable = _stable_indices([current_at[key] for key in common])
    # Added or removed neighbours shift absolute positions without being reorders; only a
    # change in relative order among shared columns is reported, using a minimal moved set.
    reordered = [
        {
            "name": key[0],
            "baseline_position": baseline_at[key] + 1,
            "current_position": current_at[key] + 1,
        }
        for index, key in enumerate(common) if index not in stable
    ]
    retyped = [
        {
            "name": key[0],
            "baseline_type_family": baseline[baseline_at[key]]["type_family"],
            "current_type_family": current[current_at[key]]["type_family"],
        }
        for key in common
        if baseline[baseline_at[key]]["type_family"] != current[current_at[key]]["type_family"]
    ]
    return {
        "added_columns": added,
        "removed_columns": removed,
        "reordered_columns": reordered,
        "retyped_columns": retyped,
    }


def compare_snapshots(baseline: list[dict[str, object]], current: list[dict[str, object]]) -> dict[str, object]:
    """Report added/removed tables and added/removed/reordered/retyped columns between snapshots."""
    validate_snapshot(baseline)
    validate_snapshot(current)
    baseline_by_key = {entry["table"].casefold(): entry for entry in baseline}
    current_by_key = {entry["table"].casefold(): entry for entry in current}
    tables_added = sorted(
        (entry["table"] for key, entry in current_by_key.items() if key not in baseline_by_key), key=str.casefold
    )
    tables_removed = sorted(
        (entry["table"] for key, entry in baseline_by_key.items() if key not in current_by_key), key=str.casefold
    )
    table_changes = []
    for key in sorted(set(baseline_by_key) & set(current_by_key)):
        changes = _compare_columns(baseline_by_key[key]["columns"], current_by_key[key]["columns"])
        if any(changes.values()):
            table_changes.append({"table": current_by_key[key]["table"], **changes})
    summary = {
        "tables_added": len(tables_added),
        "tables_removed": len(tables_removed),
        "tables_changed": len(table_changes),
        **{
            kind: sum(len(change[kind]) for change in table_changes)
            for kind in ("added_columns", "removed_columns", "reordered_columns", "retyped_columns")
        },
    }
    return {
        "algorithm": DRIFT_ALGORITHM,
        "snapshot_algorithm": SNAPSHOT_ALGORITHM,
        "row_values_inspected": False,
        "drift_detected": bool(tables_added or tables_removed or table_changes),
        "summary": summary,
        "tables_added": tables_added,
        "tables_removed": tables_removed,
        "table_changes": table_changes,
    }


def _sha256_field(record: dict[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise SchemaBaselineError("Schema baseline is missing a SHA-256 fingerprint.")
    return value


def reviewed_schema_from_record(record: dict[str, object]) -> dict[str, object]:
    """Extract and verify reviewed schema evidence from a dry-run report or run manifest."""
    if record.get("source_schema_snapshot_algorithm") != SNAPSHOT_ALGORITHM:
        raise SchemaBaselineError("Schema baseline has no supported field-level schema snapshot.")
    if record.get("schema_confirmation_algorithm") != SCHEMA_CONFIRMATION_ALGORITHM:
        raise SchemaBaselineError("Schema baseline uses an unsupported confirmation algorithm.")
    snapshot = validate_snapshot(record.get("source_schema_snapshot"))
    name_fingerprint = _sha256_field(record, "source_schema_fingerprint")
    typed_fingerprint = _sha256_field(record, "typed_schema_fingerprint")
    mapping_fingerprint = _sha256_field(record, "source_mapping_fingerprint")
    confirmation = _sha256_field(record, "schema_confirmation_fingerprint")
    if (
        snapshot_name_fingerprint(snapshot) != name_fingerprint
        or snapshot_typed_fingerprint(snapshot) != typed_fingerprint
        or schema_confirmation_fingerprint(name_fingerprint, typed_fingerprint, mapping_fingerprint) != confirmation
    ):
        raise SchemaBaselineError("Schema baseline snapshot does not match its recorded fingerprints.")
    if record.get("mode") == "dry_run":
        kind = "dry_run"
    elif isinstance(record.get("run_id"), str):
        if record.get("status") != "completed":
            raise SchemaBaselineError("Schema baseline run manifest must be completed.")
        if (
            record.get("source_schema_fingerprint_status") != "matched_at_extraction"
            or record.get("typed_schema_fingerprint_status") != "matched_at_extraction"
            or record.get("source_schema_fingerprint_observed") != name_fingerprint
            or record.get("typed_schema_fingerprint_observed") != typed_fingerprint
        ):
            raise SchemaBaselineError("Schema baseline run manifest must confirm the reviewed schema at extraction.")
        kind = "run_manifest"
    else:
        raise SchemaBaselineError("Schema baseline must be a dry-run report or a run manifest.")
    return {
        "kind": kind,
        "snapshot": snapshot,
        "source_schema_fingerprint": name_fingerprint,
        "typed_schema_fingerprint": typed_fingerprint,
        "source_mapping_fingerprint": mapping_fingerprint,
        "schema_confirmation_fingerprint": confirmation,
    }


def load_reviewed_schema(path: Path) -> dict[str, object]:
    """Read an owner-only baseline file without following links, then verify its snapshot."""
    try:
        record = read_private_json(path.parent, path.name)
    except (OSError, ValueError, UnicodeDecodeError):
        raise SchemaBaselineError("Schema baseline must be a readable owner-only JSON file.") from None
    return reviewed_schema_from_record(record)


def drift_review(
    baseline: dict[str, object],
    current_snapshot: list[dict[str, object]],
    current_mapping_fingerprint: str,
    current_confirmation: str,
    confirmed: str | None,
) -> dict[str, object]:
    """Compare reviewed and current schema; a change needs the *current* fingerprint confirmed."""
    drift = compare_snapshots(baseline["snapshot"], current_snapshot)
    mapping_changed = baseline["source_mapping_fingerprint"] != current_mapping_fingerprint
    changed = baseline["schema_confirmation_fingerprint"] != current_confirmation
    return {
        "baseline_kind": baseline["kind"],
        "baseline_schema_confirmation_fingerprint": baseline["schema_confirmation_fingerprint"],
        "current_schema_confirmation_fingerprint": current_confirmation,
        "schema_or_mapping_changed": changed,
        "mapping_fingerprint_changed": mapping_changed,
        "drift": drift,
        "fresh_confirmation_required": changed,
        "fresh_confirmation_verified": (not changed) or confirmed == current_confirmation,
        "stale_baseline_confirmation_supplied": changed and confirmed == baseline["schema_confirmation_fingerprint"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare two reviewed schema artifacts field by field, offline.")
    parser.add_argument("--baseline", type=Path, required=True,
                        help="Owner-only dry-run JSON or run manifest holding the previously reviewed schema.")
    parser.add_argument("--current", type=Path, required=True,
                        help="Owner-only dry-run JSON or run manifest holding the later schema.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Exit 0 when schema and mapping are unchanged, 1 when drift needs review, 2 when unusable."""
    args = build_parser().parse_args(argv)
    caller_cwd = Path.cwd()
    try:
        baseline = load_reviewed_schema((caller_cwd / args.baseline).expanduser())
        current = load_reviewed_schema((caller_cwd / args.current).expanduser())
        review = drift_review(
            baseline, current["snapshot"], current["source_mapping_fingerprint"],
            current["schema_confirmation_fingerprint"], None,
        )
    except SchemaBaselineError as error:
        print(f"Schema drift review stopped: {error}", file=sys.stderr)
        return 2
    review.pop("fresh_confirmation_verified")
    review.pop("stale_baseline_confirmation_supplied")
    review["current_kind"] = current["kind"]
    print(json.dumps(review, indent=2))
    return 1 if review["schema_or_mapping_changed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
