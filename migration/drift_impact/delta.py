"""Normalize reviewed old/new schema snapshots into stable, fingerprint-bound field deltas.

Scope: offline and advisory only. Inputs are the
verified reviewed-schema records produced by ``schema_drift.reviewed_schema_from_record`` /
``schema_drift.load_reviewed_schema``; each is re-verified here against its own recorded
fingerprints before any delta is derived. Nothing here reads row values, touches files,
the network, a warehouse, or dbt; nothing approves a schema, updates a baseline, or decides
severity. Severity, key, metric, and mapping impact belong to later drift_impact modules.

Delta model
-----------
Each delta has exactly one ``kind`` (``added``, ``removed``, ``retyped``, ``reordered``) and
one ``scope`` (``table`` or ``column``). Every delta carries the same keys; a key that does
not apply is ``None``. For column deltas, ``old_*`` fields are populated if and only if the
column exists in the old snapshot, and ``new_*`` fields if and only if it exists in the new
snapshot.

* Tables and columns are matched by exact name. A rename, including a case-only rename, is
  reported conservatively as ``removed`` plus ``added``; no rename is ever inferred.
* Repeated column names are matched by (name, occurrence) and never collapse.
* An added or removed table also yields one ``column`` delta per column it holds, flagged
  ``via_table_change``, so field-level consumers need no special case for table changes.
* ``reordered`` reports only a change in relative order among shared columns, using the same
  minimal moved set as ``schema_drift`` (one longest increasing subsequence stays put). Shifts
  caused solely by added/removed neighbours are not reorders.
* A column that both moved and changed type yields one ``reordered`` and one ``retyped`` delta.
* ``unrecognized_type_family`` marks deltas touching an ``other:*`` family so downstream
  classification can keep them unknown rather than low.

Output ordering is total and deterministic: table (case-folded, then exact), table-scope
before column-scope, then kind (removed, added, retyped, reordered), then position, column,
and occurrence. ``delta_id`` is the SHA-256 of the delta's canonical content, and
``delta_set_fingerprint`` binds the ordered delta IDs to both snapshots' fingerprints.

Errors raise :class:`DeltaInputError` with fixed, value-free messages: no table or column
names, type labels, or fingerprints appear in exception text or reprs, and no underlying
exception is chained as ``__cause__`` or ``__context__``. Inputs are shape-checked using exact
built-in types (``dict``, ``list``, ``str``; subclasses are rejected) before any membership,
set, hashing, or schema_drift helper call, so caller-supplied hooks never run.

Each record's values are read once and its snapshot is copied into fresh plain
``list``/``dict``/``str`` structures in the same pass that shape-checks it. Validation,
fingerprint verification, and every delta are computed from that private copy, so a caller
(or another thread) mutating its own objects during or after the call can neither break the
fingerprint binding nor surface an identifier-bearing exception, and the result never aliases
caller input.

Privacy and handling
--------------------
The result is private, owner-only evidence. It contains raw table and column names, type
family labels, and unsalted SHA-256 fingerprints (``delta_id``, ``delta_set_fingerprint``,
and the copied header fingerprints). Unsalted hashes of short identifiers are confirmable by
anyone who can guess the names, so treat them as sensitive as the names themselves. Do not
log or print the result, attach it to tickets or chat, or commit it. Persist it only through
the owner-only writer in ``_private_json`` (``create_private_json``/``update_private_json``).
Shareable summaries must carry counts only (for example ``delta_count`` and ``counts``), never
deltas, identifiers, or fingerprints.

Residual risk for callers: an escaping :class:`DeltaInputError` is raised fresh from
:func:`normalize_deltas` itself, so its traceback holds no frames from the internal helpers.
The traceback does still reference ``normalize_deltas``'s own frame, whose locals are only the
caller's ``old``/``new`` arguments. Tools that render frame locals (``traceback`` with
``capture_locals=True``, rich/IPython debuggers, crash reporters) can therefore print the
caller's own records; disable local capture or redact before reporting such errors.
"""

from __future__ import annotations

import json
import re
from hashlib import sha256

from schema_drift import (
    SNAPSHOT_ALGORITHM,
    _MAX_COLUMNS_PER_TABLE,
    _MAX_NAME_LENGTH,
    _MAX_TABLES,
    SchemaBaselineError,
    _occurrence_keys,
    _stable_indices,
    schema_confirmation_fingerprint,
    snapshot_name_fingerprint,
    snapshot_typed_fingerprint,
    validate_snapshot,
)


__all__ = [
    "DELTA_ALGORITHM",
    "DELTA_KINDS",
    "DELTA_SCOPES",
    "DeltaInputError",
    "normalize_deltas",
]

DELTA_ALGORITHM = "drift-impact-normalized-schema-delta-v1"
DELTA_KINDS = ("removed", "added", "retyped", "reordered")
DELTA_SCOPES = ("table", "column")

_REVIEWED_KEYS = frozenset({
    "kind",
    "snapshot",
    "source_schema_fingerprint",
    "typed_schema_fingerprint",
    "source_mapping_fingerprint",
    "schema_confirmation_fingerprint",
})
_REVIEWED_KINDS = frozenset({"dry_run", "run_manifest"})
_FINGERPRINT_KEYS = (
    "source_schema_fingerprint",
    "typed_schema_fingerprint",
    "source_mapping_fingerprint",
    "schema_confirmation_fingerprint",
)
_TABLE_KEYS = frozenset({"table", "columns"})
_COLUMN_KEYS = frozenset({"name", "type_family"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
# Lone surrogates cannot be UTF-8 encoded; hashing them would raise an exception whose
# ``object`` attribute carries the full identifier.
_SURROGATE = re.compile("[\ud800-\udfff]")
_UNRECOGNIZED_FAMILY_PREFIX = "other:"
_KIND_RANK = {kind: rank for rank, kind in enumerate(DELTA_KINDS)}
_SCOPE_RANK = {scope: rank for rank, scope in enumerate(DELTA_SCOPES)}
_DELTA_FIELDS = (
    "kind",
    "scope",
    "table",
    "column",
    "occurrence",
    "old_position",
    "new_position",
    "old_type_family",
    "new_type_family",
    "via_table_change",
    "unrecognized_type_family",
)


class DeltaInputError(ValueError):
    """A supplied snapshot record cannot be used as validated, fingerprint-bound input.

    Messages are fixed strings and never include identifiers, type labels, or fingerprints.
    """


def _canonical_digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(payload.encode("utf-8")).hexdigest()


def _is_text(value: object, max_length: int) -> bool:
    """Exact ``str`` (no subclass hooks), bounded, and UTF-8 encodable (no lone surrogates)."""
    return type(value) is str and len(value) <= max_length and not _SURROGATE.search(value)


def _is_plain_record(value: object, keys: frozenset[str]) -> bool:
    """Exact ``dict`` whose keys are exact ``str`` and equal ``keys``.

    Key types are checked before any set operation so no caller-supplied ``__hash__`` or
    ``__eq__`` ever runs.
    """
    return type(value) is dict and all(type(key) is str for key in value) and value.keys() == keys


def _plain_snapshot_copy(value: object) -> list[dict[str, object]] | None:
    """Shape-check and copy a snapshot in one pass, using only exact built-in types.

    Returns fresh ``list``/``dict`` containers holding exact ``str`` values (immutable, so
    shared safely), or ``None`` if any part is not bounded plain schema metadata. Every value
    is read once and checked before it is stored, so later caller mutation cannot reach the
    copy. ``validate_snapshot`` still enforces non-empty and case-insensitively unique table
    names on the copy.
    """
    if type(value) is not list or len(value) > _MAX_TABLES:
        return None
    tables: list[dict[str, object]] = []
    for entry in value:
        if not _is_plain_record(entry, _TABLE_KEYS):
            return None
        table, columns = entry["table"], entry["columns"]
        if not _is_text(table, _MAX_NAME_LENGTH):
            return None
        if type(columns) is not list or len(columns) > _MAX_COLUMNS_PER_TABLE:
            return None
        copied_columns: list[dict[str, object]] = []
        for column in columns:
            if not _is_plain_record(column, _COLUMN_KEYS):
                return None
            name, family = column["name"], column["type_family"]
            if not (_is_text(name, _MAX_NAME_LENGTH) and _is_text(family, _MAX_NAME_LENGTH)):
                return None
            copied_columns.append({"name": name, "type_family": family})
        tables.append({"table": table, "columns": copied_columns})
    return tables


def _checked_side(record: object, side: str) -> tuple[list[dict[str, object]], dict[str, str]]:
    if not _is_plain_record(record, _REVIEWED_KEYS):
        raise DeltaInputError(f"The {side} input must be a verified reviewed-schema record.")
    # Read every field exactly once; all later steps use these locals and the snapshot copy.
    kind = record["kind"]
    fingerprints = {key: record[key] for key in _FINGERPRINT_KEYS}
    raw_snapshot = record["snapshot"]
    if type(kind) is not str or kind not in _REVIEWED_KINDS:
        raise DeltaInputError(f"The {side} input has an unsupported reviewed-schema kind.")
    for value in fingerprints.values():
        # Length is checked first so the regex never scans an oversized string.
        if type(value) is not str or len(value) != 64 or not _SHA256.fullmatch(value):
            raise DeltaInputError(f"The {side} input is missing a SHA-256 fingerprint.")
    copied = _plain_snapshot_copy(raw_snapshot)
    del raw_snapshot
    if copied is None:
        raise DeltaInputError(f"The {side} input snapshot is not bounded schema metadata.")
    try:
        snapshot = validate_snapshot(copied)
    except SchemaBaselineError:
        snapshot = None
    if snapshot is None:
        # Raised outside the handler so no exception context is chained at all.
        raise DeltaInputError(f"The {side} input snapshot is not bounded schema metadata.")
    name_fingerprint = snapshot_name_fingerprint(snapshot)
    typed_fingerprint = snapshot_typed_fingerprint(snapshot)
    if (
        name_fingerprint != fingerprints["source_schema_fingerprint"]
        or typed_fingerprint != fingerprints["typed_schema_fingerprint"]
        or schema_confirmation_fingerprint(
            name_fingerprint, typed_fingerprint, fingerprints["source_mapping_fingerprint"]
        ) != fingerprints["schema_confirmation_fingerprint"]
    ):
        raise DeltaInputError(f"The {side} input snapshot does not match its recorded fingerprints.")
    header = {"kind": kind, **fingerprints}
    return snapshot, header


def _verified_side(record: object, side: str) -> tuple[list[dict[str, object]], dict[str, str]]:
    """Re-verify one reviewed-schema record; return its private snapshot copy and header.

    Any non-:class:`DeltaInputError` exception from a helper is replaced by a generic
    :class:`DeltaInputError`; :func:`normalize_deltas` re-raises every error fresh.
    """
    try:
        return _checked_side(record, side)
    except DeltaInputError:
        raise
    except Exception:
        pass
    raise DeltaInputError(f"The {side} input is not a usable reviewed-schema record.")


def _delta(kind: str, scope: str, table: str, **fields: object) -> dict[str, object]:
    record: dict[str, object] = {
        "kind": kind,
        "scope": scope,
        "table": table,
        "column": None,
        "occurrence": None,
        "old_position": None,
        "new_position": None,
        "old_type_family": None,
        "new_type_family": None,
        "via_table_change": False,
    }
    record.update(fields)
    record["unrecognized_type_family"] = any(
        isinstance(record[key], str) and record[key].startswith(_UNRECOGNIZED_FAMILY_PREFIX)
        for key in ("old_type_family", "new_type_family")
    )
    return {"delta_id": _canonical_digest([DELTA_ALGORITHM, [record[key] for key in _DELTA_FIELDS]]), **record}


def _old_side(column: dict[str, object], position: int) -> dict[str, object]:
    return {"old_position": position, "old_type_family": column["type_family"]}


def _new_side(column: dict[str, object], position: int) -> dict[str, object]:
    return {"new_position": position, "new_type_family": column["type_family"]}


def _whole_table(kind: str, table: str, columns: list[dict[str, object]]) -> list[dict[str, object]]:
    """A table that exists on one side only: one table delta plus one delta per column."""
    side = _old_side if kind == "removed" else _new_side
    deltas = [_delta(kind, "table", table)]
    for index, (column, (name, occurrence)) in enumerate(zip(columns, _occurrence_keys(columns))):
        deltas.append(_delta(
            kind, "column", table, column=name, occurrence=occurrence,
            via_table_change=True, **side(column, index + 1),
        ))
    return deltas


def _shared_table(
    table: str, old_columns: list[dict[str, object]], new_columns: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Column deltas for a table present in both snapshots under the exact same name."""
    old_keys = _occurrence_keys(old_columns)
    new_keys = _occurrence_keys(new_columns)
    old_at = {key: index for index, key in enumerate(old_keys)}
    new_at = {key: index for index, key in enumerate(new_keys)}
    deltas = []
    for index, key in enumerate(old_keys):
        if key not in new_at:
            deltas.append(_delta(
                "removed", "column", table, column=key[0], occurrence=key[1],
                **_old_side(old_columns[index], index + 1),
            ))
    for index, key in enumerate(new_keys):
        if key not in old_at:
            deltas.append(_delta(
                "added", "column", table, column=key[0], occurrence=key[1],
                **_new_side(new_columns[index], index + 1),
            ))
    common = [key for key in old_keys if key in new_at]
    stable = _stable_indices([new_at[key] for key in common])
    for common_index, key in enumerate(common):
        old_index, new_index = old_at[key], new_at[key]
        both_sides = {
            "column": key[0],
            "occurrence": key[1],
            **_old_side(old_columns[old_index], old_index + 1),
            **_new_side(new_columns[new_index], new_index + 1),
        }
        if old_columns[old_index]["type_family"] != new_columns[new_index]["type_family"]:
            deltas.append(_delta("retyped", "column", table, **both_sides))
        if common_index not in stable:
            deltas.append(_delta("reordered", "column", table, **both_sides))
    return deltas


def _sort_key(delta: dict[str, object]) -> tuple:
    position = delta["new_position"] if delta["kind"] == "added" else delta["old_position"]
    return (
        delta["table"].casefold(),
        delta["table"],
        _SCOPE_RANK[delta["scope"]],
        _KIND_RANK[delta["kind"]],
        position or 0,
        delta["column"] or "",
        delta["occurrence"] or 0,
    )


_BUILD_FAILED = "The reviewed-schema inputs could not be normalized."
# Every message DeltaInputError may carry out of normalize_deltas; anything else is replaced.
_FIXED_MESSAGES = frozenset({_BUILD_FAILED} | {
    f"The {side} input {suffix}"
    for side in ("old", "new")
    for suffix in (
        "must be a verified reviewed-schema record.",
        "has an unsupported reviewed-schema kind.",
        "is missing a SHA-256 fingerprint.",
        "snapshot is not bounded schema metadata.",
        "snapshot does not match its recorded fingerprints.",
        "is not a usable reviewed-schema record.",
    )
})


def normalize_deltas(old: object, new: object) -> dict[str, object]:
    """Return stable added/removed/retyped/reordered deltas between two reviewed snapshots.

    ``old`` and ``new`` must each be a verified reviewed-schema record exactly as returned by
    ``schema_drift.reviewed_schema_from_record``. Inputs are not mutated, and the result shares
    no mutable object with them. The result is advisory, private evidence (raw identifiers and
    unsalted fingerprints; see the module docstring) and is never schema approval or cutover
    readiness.

    Only :class:`DeltaInputError` with a fixed message escapes. It is raised fresh from this
    frame, outside any handler, so it has no ``__cause__``/``__context__`` and its traceback
    holds no internal helper frames or their locals.
    """
    result: dict[str, object] | None = None
    message = _BUILD_FAILED
    try:
        result = _normalize(old, new)
    except DeltaInputError as error:
        # Keep only a known fixed message, never the exception object or its frames.
        args = error.args
        if len(args) == 1 and type(args[0]) is str and args[0] in _FIXED_MESSAGES:
            message = args[0]
        del args
    except Exception:
        pass
    if result is None:
        raise DeltaInputError(message)
    return result


def _normalize(old: object, new: object) -> dict[str, object]:
    old_snapshot, old_header = _verified_side(old, "old")
    new_snapshot, new_header = _verified_side(new, "new")
    # validate_snapshot guarantees case-insensitively unique (hence exactly unique) table names.
    old_tables = {entry["table"]: entry["columns"] for entry in old_snapshot}
    new_tables = {entry["table"]: entry["columns"] for entry in new_snapshot}
    deltas: list[dict[str, object]] = []
    for table, columns in old_tables.items():
        if table not in new_tables:
            deltas.extend(_whole_table("removed", table, columns))
    for table, columns in new_tables.items():
        if table not in old_tables:
            deltas.extend(_whole_table("added", table, columns))
        else:
            deltas.extend(_shared_table(table, old_tables[table], columns))
    deltas.sort(key=_sort_key)

    counts = {
        scope: {kind: 0 for kind in DELTA_KINDS if scope == "column" or kind in ("removed", "added")}
        for scope in DELTA_SCOPES
    }
    for delta in deltas:
        counts[delta["scope"]][delta["kind"]] += 1

    schema_changed = (
        old_header["source_schema_fingerprint"] != new_header["source_schema_fingerprint"]
        or old_header["typed_schema_fingerprint"] != new_header["typed_schema_fingerprint"]
    )
    delta_ids = [delta["delta_id"] for delta in deltas]
    return {
        "algorithm": DELTA_ALGORITHM,
        "snapshot_algorithm": SNAPSHOT_ALGORITHM,
        "row_values_inspected": False,
        "advisory_only": True,
        "approves_schema": False,
        "updates_baseline": False,
        "cutover_ready": False,
        "old": old_header,
        "new": new_header,
        "schema_changed": schema_changed,
        # Snapshot fingerprints changed but no field delta explains it (for example, a
        # differently ordered table list). Consumers must treat this as unexplained, not as
        # "no change".
        "fingerprint_change_without_delta": schema_changed and not deltas,
        # Mapping changes are bound here but not normalized; mapping impact is a separate module.
        "mapping_fingerprint_changed": (
            old_header["source_mapping_fingerprint"] != new_header["source_mapping_fingerprint"]
        ),
        "delta_count": len(deltas),
        "counts": counts,
        "deltas": deltas,
        "delta_set_fingerprint": _canonical_digest([
            DELTA_ALGORITHM,
            old_header["typed_schema_fingerprint"],
            new_header["typed_schema_fingerprint"],
            delta_ids,
        ]),
    }
