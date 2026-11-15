#!/usr/bin/env python3
"""Reference runner for the idempotent incremental-ingest template.

Standard library only. It reads headed UTF-8 CSV batch files from a local
directory and merges them into a local SQLite file that holds both the target
tables and the ingest state. It is a pattern reference, not a warehouse
connector: it opens no network connection and reads no credentials.

See README.md in this directory for the behaviour contract.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import hashlib
import io
import json
import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Optional

MANIFEST_VERSION = 1
MODES = ("incremental", "replay", "backfill")
COLUMN_TYPES = ("string", "integer", "decimal", "date", "timestamp")
NEW_COLUMN_POLICIES = ("fail", "add", "ignore")
LATE_POLICIES = ("quarantine", "apply")
LATE_REASON = "late_beyond_lookback"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_META_COLUMNS = ("_cursor_value", "_row_hash", "_source_file", "_applied_run_id")
_COUNT_KEYS = (
    "rows_read",
    "inserted",
    "updated",
    "unchanged",
    "stale_ignored",
    "late_within_lookback",
    "late_beyond_lookback",
    "outside_backfill_window",
    "rejected",
)


class IngestError(Exception):
    """Base class for failures that stop a run. Messages never include row values."""


class ManifestError(IngestError):
    pass


class SchemaDriftError(IngestError):
    pass


class SourceChangedError(IngestError):
    pass


class RejectThresholdError(IngestError):
    pass


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool


@dataclass(frozen=True)
class Stream:
    name: str
    file_glob: str
    primary_key: tuple
    cursor_column: str
    lookback_seconds: int
    late_arrival_policy: str
    on_new_column: str
    max_rejected_rows_per_file: int
    max_future_skew_seconds: int
    columns: tuple

    @property
    def table(self) -> str:
        return f"tgt_{self.name}"


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ManifestError(message)


def _non_negative_int(raw: dict, key: str, default: int, where: str) -> int:
    value = raw.get(key, default)
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0,
        f"{where}: {key} must be a non-negative integer",
    )
    return value


def parse_manifest(document: object) -> list:
    _require(isinstance(document, dict), "manifest must be a JSON object")
    _require(
        document.get("manifest_version") == MANIFEST_VERSION,
        f"manifest_version must be {MANIFEST_VERSION}",
    )
    raw_streams = document.get("streams")
    _require(isinstance(raw_streams, list) and raw_streams, "streams must be a non-empty list")
    streams = []
    seen = set()
    for raw in raw_streams:
        _require(isinstance(raw, dict), "each stream must be an object")
        name = raw.get("name")
        _require(isinstance(name, str) and bool(_IDENTIFIER.match(name)), "stream name must be lowercase snake_case")
        where = f"stream {name}"
        _require(name not in seen, f"{where}: duplicate stream name")
        seen.add(name)

        file_glob = raw.get("file_glob")
        _require(isinstance(file_glob, str) and bool(file_glob), f"{where}: file_glob is required")
        _require(
            "/" not in file_glob and "\\" not in file_glob and ".." not in file_glob,
            f"{where}: file_glob must be a bare file name pattern",
        )

        raw_columns = raw.get("columns")
        _require(isinstance(raw_columns, list) and raw_columns, f"{where}: columns must be a non-empty list")
        columns = []
        for raw_column in raw_columns:
            _require(isinstance(raw_column, dict), f"{where}: each column must be an object")
            column_name = raw_column.get("name")
            _require(
                isinstance(column_name, str) and bool(_IDENTIFIER.match(column_name)),
                f"{where}: column names must be lowercase snake_case",
            )
            _require(column_name not in {c.name for c in columns}, f"{where}: duplicate column {column_name}")
            column_type = raw_column.get("type")
            _require(column_type in COLUMN_TYPES, f"{where}: column {column_name} has an unsupported type")
            nullable = raw_column.get("nullable", False)
            _require(isinstance(nullable, bool), f"{where}: column {column_name} nullable must be true or false")
            columns.append(Column(column_name, column_type, nullable))
        by_name = {c.name: c for c in columns}

        primary_key = raw.get("primary_key")
        _require(
            isinstance(primary_key, list) and primary_key and all(isinstance(k, str) for k in primary_key),
            f"{where}: primary_key must be a non-empty list of column names",
        )
        _require(len(set(primary_key)) == len(primary_key), f"{where}: primary_key repeats a column")
        for key in primary_key:
            _require(key in by_name, f"{where}: primary_key column {key} is not declared")
            _require(not by_name[key].nullable, f"{where}: primary_key column {key} must not be nullable")

        cursor_column = raw.get("cursor_column")
        _require(cursor_column in by_name, f"{where}: cursor_column must be a declared column")
        _require(by_name[cursor_column].type == "timestamp", f"{where}: cursor_column must have type timestamp")
        _require(not by_name[cursor_column].nullable, f"{where}: cursor_column must not be nullable")

        late_policy = raw.get("late_arrival_policy", "quarantine")
        _require(late_policy in LATE_POLICIES, f"{where}: late_arrival_policy must be one of {LATE_POLICIES}")
        drift = raw.get("schema_drift", {})
        _require(isinstance(drift, dict), f"{where}: schema_drift must be an object")
        on_new_column = drift.get("on_new_column", "fail")
        _require(on_new_column in NEW_COLUMN_POLICIES, f"{where}: on_new_column must be one of {NEW_COLUMN_POLICIES}")

        streams.append(
            Stream(
                name=name,
                file_glob=file_glob,
                primary_key=tuple(primary_key),
                cursor_column=cursor_column,
                lookback_seconds=_non_negative_int(raw, "lookback_seconds", 0, where),
                late_arrival_policy=late_policy,
                on_new_column=on_new_column,
                max_rejected_rows_per_file=_non_negative_int(raw, "max_rejected_rows_per_file", 0, where),
                max_future_skew_seconds=_non_negative_int(raw, "max_future_skew_seconds", 300, where),
                columns=tuple(columns),
            )
        )
    return streams


def load_manifest(path: Path) -> list:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ManifestError(f"cannot read manifest: {type(error).__name__}") from None
    return parse_manifest(document)


# --------------------------------------------------------------------------
# Value canonicalisation
# --------------------------------------------------------------------------


def canonical_timestamp(text: str) -> str:
    """Return a fixed-width UTC string whose text order equals time order."""
    value = text.strip()
    if value[-1:] in ("Z", "z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no UTC offset")
    return parsed.astimezone(timezone.utc).strftime(_TS_FORMAT)


def _parse_canonical(value: str) -> datetime:
    return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=timezone.utc)


def _canonical_value(column_type: str, text: str) -> str:
    if column_type == "string":
        return text
    if column_type == "integer":
        if not re.fullmatch(r"[+-]?[0-9]+", text):
            raise ValueError("not an integer")
        return str(int(text))
    if column_type == "decimal":
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise ValueError("not a decimal") from None
        if not number.is_finite():
            raise ValueError("not a finite decimal")
        rendered = format(number, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return "0" if rendered in ("-0", "") else rendered
    if column_type == "date":
        return date.fromisoformat(text).isoformat()
    return canonical_timestamp(text)


def row_hash(values: dict) -> str:
    """Hash non-null values only, so adding a nullable column keeps old hashes stable."""
    payload = {key: value for key, value in values.items() if value is not None}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# State and target storage
# --------------------------------------------------------------------------

_STATE_DDL = (
    """CREATE TABLE IF NOT EXISTS _ingest_runs (
        run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, started_at TEXT NOT NULL,
        finished_at TEXT, status TEXT NOT NULL, error_type TEXT, error_message TEXT)""",
    """CREATE TABLE IF NOT EXISTS _ingest_checkpoint (
        stream TEXT PRIMARY KEY, high_watermark TEXT NOT NULL, updated_run_id TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS _ingest_files (
        stream TEXT NOT NULL, file_name TEXT NOT NULL, sha256 TEXT NOT NULL,
        rows_read INTEGER NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY (stream, file_name))""",
    """CREATE TABLE IF NOT EXISTS _ingest_schema_history (
        stream TEXT NOT NULL, column_name TEXT NOT NULL, event TEXT NOT NULL,
        first_file_name TEXT NOT NULL, run_id TEXT NOT NULL,
        PRIMARY KEY (stream, column_name, event))""",
    """CREATE TABLE IF NOT EXISTS _ingest_quarantine (
        stream TEXT NOT NULL, file_name TEXT NOT NULL, line_number INTEGER NOT NULL,
        reason TEXT NOT NULL, row_hash TEXT, cursor_value TEXT, raw_json TEXT NOT NULL,
        run_id TEXT NOT NULL, PRIMARY KEY (stream, file_name, line_number, reason))""",
)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _connect(state_db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(state_db), isolation_level=None, timeout=30)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> list:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({_quote(table)})")]


def _ensure_target(connection: sqlite3.Connection, stream: Stream) -> None:
    definitions = [f"{_quote(c.name)} TEXT" for c in stream.columns]
    definitions += [f"{_quote(name)} TEXT NOT NULL" for name in _META_COLUMNS]
    keys = ", ".join(_quote(k) for k in stream.primary_key)
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {_quote(stream.table)} ({', '.join(definitions)}, PRIMARY KEY ({keys}))"
    )
    existing = set(_table_columns(connection, stream.table))
    for column in stream.columns:
        # A column newly declared in the manifest is an additive, nullable change.
        if column.name not in existing:
            connection.execute(f"ALTER TABLE {_quote(stream.table)} ADD COLUMN {_quote(column.name)} TEXT")


def _added_columns(connection: sqlite3.Connection, stream: Stream) -> list:
    declared = {c.name for c in stream.columns}
    rows = connection.execute(
        "SELECT column_name FROM _ingest_schema_history WHERE stream = ? AND event = 'added' ORDER BY rowid",
        (stream.name,),
    )
    return [row[0] for row in rows if row[0] not in declared]


def _record_drift(connection, stream: Stream, column: str, event: str, file_name: str, run_id: str) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO _ingest_schema_history VALUES (?, ?, ?, ?, ?)",
        (stream.name, column, event, file_name, run_id),
    )


def _watermark(connection: sqlite3.Connection, stream: Stream) -> Optional[str]:
    row = connection.execute(
        "SELECT high_watermark FROM _ingest_checkpoint WHERE stream = ?", (stream.name,)
    ).fetchone()
    return row[0] if row else None


# --------------------------------------------------------------------------
# File processing
# --------------------------------------------------------------------------


def _resolve_header(connection, stream: Stream, header: list, file_name: str, run_id: str) -> list:
    """Apply the schema-drift policy and return the active target columns (name, type, nullable)."""
    if not header or any(not name for name in header):
        raise SchemaDriftError(f"{file_name}: header has an empty column name")
    if len(set(header)) != len(header):
        raise SchemaDriftError(f"{file_name}: header repeats a column name")

    declared = {c.name: c for c in stream.columns}
    added = _added_columns(connection, stream)
    for name in header:
        if name in declared or name in added:
            continue
        if stream.on_new_column == "fail":
            raise SchemaDriftError(f"{file_name}: undeclared column {name!r} (on_new_column=fail)")
        if stream.on_new_column == "ignore":
            _record_drift(connection, stream, name, "ignored", file_name, run_id)
            continue
        if not _IDENTIFIER.match(name):
            raise SchemaDriftError(f"{file_name}: new column {name!r} is not a lowercase snake_case name")
        connection.execute(f"ALTER TABLE {_quote(stream.table)} ADD COLUMN {_quote(name)} TEXT")
        _record_drift(connection, stream, name, "added", file_name, run_id)
        added.append(name)

    for column in stream.columns:
        if column.name in header:
            continue
        if not column.nullable:
            raise SchemaDriftError(f"{file_name}: required column {column.name!r} is missing")
        _record_drift(connection, stream, column.name, "missing_nullable", file_name, run_id)

    return list(stream.columns) + [Column(name, "string", True) for name in added]


def _merge(connection, stream: Stream, values: dict, cursor: str, digest: str, file_name: str, run_id: str) -> str:
    """Key-wise newest-wins upsert. Order-independent, so replays converge to one state."""
    table = _quote(stream.table)
    where = " AND ".join(f"{_quote(k)} = ?" for k in stream.primary_key)
    key = [values[k] for k in stream.primary_key]
    existing = connection.execute(f"SELECT _cursor_value, _row_hash FROM {table} WHERE {where}", key).fetchone()
    payload = dict(values)
    payload.update(_cursor_value=cursor, _row_hash=digest, _source_file=file_name, _applied_run_id=run_id)
    names = list(payload)
    if existing is None:
        placeholders = ", ".join("?" for _ in names)
        connection.execute(
            f"INSERT INTO {table} ({', '.join(_quote(n) for n in names)}) VALUES ({placeholders})",
            [payload[n] for n in names],
        )
        return "inserted"
    if (cursor, digest) == tuple(existing):
        return "unchanged"
    # Equal cursors with different content are settled by the row hash, not by arrival order.
    if (cursor, digest) < tuple(existing):
        return "stale_ignored"
    assignments = ", ".join(f"{_quote(n)} = ?" for n in names)
    connection.execute(f"UPDATE {table} SET {assignments} WHERE {where}", [payload[n] for n in names] + key)
    return "updated"


def _process_file(
    connection,
    stream: Stream,
    path: Path,
    mode: str,
    window: Optional[tuple],
    run_id: str,
    now: datetime,
    fault_injector: Optional[Callable],
) -> dict:
    file_name = path.name
    data = path.read_bytes()
    digest_of_file = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise IngestError(f"{file_name}: file is not valid UTF-8") from None

    counts = dict.fromkeys(_COUNT_KEYS, 0)
    rejected = []
    # One transaction per file: rows, quarantine, ledger and watermark commit together or not at all.
    connection.execute("BEGIN IMMEDIATE")
    try:
        ledger = connection.execute(
            "SELECT sha256 FROM _ingest_files WHERE stream = ? AND file_name = ?", (stream.name, file_name)
        ).fetchone()
        if mode == "incremental" and ledger is not None:
            if ledger[0] != digest_of_file:
                raise SourceChangedError(
                    f"{file_name}: content changed after it was ingested; publish a new batch file or run --mode replay"
                )
            connection.execute("ROLLBACK")
            return {"file": file_name, "status": "skipped_already_applied", "counts": counts}

        reader = csv.reader(io.StringIO(text, newline=""))
        try:
            header = [name.strip() for name in next(reader)]
        except StopIteration:
            raise SchemaDriftError(f"{file_name}: file is empty") from None
        active = _resolve_header(connection, stream, header, file_name, run_id)

        watermark = _watermark(connection, stream)
        threshold = None
        if watermark is not None:
            threshold = (_parse_canonical(watermark) - timedelta(seconds=stream.lookback_seconds)).strftime(_TS_FORMAT)
        future_limit = (now + timedelta(seconds=stream.max_future_skew_seconds)).strftime(_TS_FORMAT)
        max_cursor = watermark

        def quarantine(line: int, reason: str, record: list, digest=None, cursor=None) -> None:
            connection.execute(
                "INSERT OR IGNORE INTO _ingest_quarantine VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (stream.name, file_name, line, reason, digest, cursor, json.dumps(record), run_id),
            )

        for record in reader:
            line = reader.line_num
            if not record:
                continue
            counts["rows_read"] += 1
            reason = None
            values = {}
            if len(record) != len(header):
                reason = "field_count_mismatch"
            else:
                raw = dict(zip(header, record))
                for column in active:
                    cell = raw.get(column.name, "")
                    if cell == "":
                        if not column.nullable:
                            reason = f"null_not_allowed:{column.name}"
                            break
                        values[column.name] = None
                        continue
                    try:
                        values[column.name] = _canonical_value(column.type, cell)
                    except ValueError:
                        reason = f"invalid_{column.type}:{column.name}"
                        break
                if reason is None and values[stream.cursor_column] > future_limit:
                    reason = "future_cursor"
            if reason is not None:
                counts["rejected"] += 1
                rejected.append((line, reason))
                if len(rejected) > stream.max_rejected_rows_per_file:
                    shown = ", ".join(f"line {n}: {why}" for n, why in rejected[:5])
                    raise RejectThresholdError(
                        f"{file_name}: {len(rejected)} rejected row(s) exceed "
                        f"max_rejected_rows_per_file={stream.max_rejected_rows_per_file} ({shown})"
                    )
                quarantine(line, reason, record)
                continue

            cursor = values[stream.cursor_column]
            digest = row_hash(values)
            if max_cursor is None or cursor > max_cursor:
                max_cursor = cursor

            if mode == "backfill":
                if not (window[0] <= cursor < window[1]):
                    counts["outside_backfill_window"] += 1
                    continue
            elif mode == "incremental" and watermark is not None and cursor < watermark:
                if cursor < threshold:
                    counts["late_beyond_lookback"] += 1
                    if stream.late_arrival_policy == "quarantine":
                        quarantine(line, LATE_REASON, record, digest, cursor)
                        continue
                else:
                    counts["late_within_lookback"] += 1

            counts[_merge(connection, stream, values, cursor, digest, file_name, run_id)] += 1
            if mode != "incremental":
                # A replay or backfill that covers a quarantined late row resolves it.
                connection.execute(
                    "DELETE FROM _ingest_quarantine WHERE stream = ? AND reason = ? AND row_hash = ?",
                    (stream.name, LATE_REASON, digest),
                )
            if fault_injector is not None:
                fault_injector(stream.name, file_name, counts["rows_read"])

        if mode != "backfill":
            connection.execute(
                "INSERT OR REPLACE INTO _ingest_files VALUES (?, ?, ?, ?, ?)",
                (stream.name, file_name, digest_of_file, counts["rows_read"], run_id),
            )
            if max_cursor is not None and max_cursor != watermark:
                # The watermark only ever moves forward.
                connection.execute(
                    "INSERT OR REPLACE INTO _ingest_checkpoint VALUES (?, ?, ?)", (stream.name, max_cursor, run_id)
                )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return {"file": file_name, "status": "applied", "counts": counts}


# --------------------------------------------------------------------------
# Run orchestration
# --------------------------------------------------------------------------


def _stream_files(stream: Stream, input_dir: Path) -> list:
    return sorted(
        (p for p in input_dir.iterdir() if p.is_file() and fnmatch.fnmatchcase(p.name, stream.file_glob)),
        key=lambda p: p.name,
    )


def ingest(
    streams: list,
    input_dir: Path,
    state_db: Path,
    mode: str = "incremental",
    backfill_from: Optional[str] = None,
    backfill_to: Optional[str] = None,
    now: Optional[datetime] = None,
    fault_injector: Optional[Callable] = None,
) -> dict:
    """Run one ingest pass and return a summary. Raises IngestError on a stopped run."""
    if mode not in MODES:
        raise IngestError(f"mode must be one of {MODES}")
    window = None
    if mode == "backfill":
        if not backfill_from or not backfill_to:
            raise IngestError("backfill requires both a from and a to timestamp")
        try:
            window = (canonical_timestamp(backfill_from), canonical_timestamp(backfill_to))
        except ValueError:
            raise IngestError("backfill bounds must be ISO-8601 timestamps with a UTC offset") from None
        if window[0] >= window[1]:
            raise IngestError("backfill from must be earlier than to")
    elif backfill_from or backfill_to:
        raise IngestError("backfill bounds are only valid with mode backfill")
    input_dir = Path(input_dir)
    if not input_dir.is_dir():
        raise IngestError("input directory does not exist")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    run_id = str(uuid.uuid4())
    summary = {"run_id": run_id, "mode": mode, "status": "running", "streams": {}}
    connection = _connect(Path(state_db))
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in _STATE_DDL:
            connection.execute(statement)
        for stream in streams:
            _ensure_target(connection, stream)
        connection.execute(
            "INSERT INTO _ingest_runs (run_id, mode, started_at, status) VALUES (?, ?, ?, 'running')",
            (run_id, mode, now.strftime(_TS_FORMAT)),
        )
        connection.execute("COMMIT")

        try:
            for stream in streams:
                entry = {"watermark_before": _watermark(connection, stream), "files": []}
                summary["streams"][stream.name] = entry
                for path in _stream_files(stream, input_dir):
                    entry["files"].append(
                        _process_file(connection, stream, path, mode, window, run_id, now, fault_injector)
                    )
                    entry["watermark_after"] = _watermark(connection, stream)
                entry["watermark_after"] = _watermark(connection, stream)
        except BaseException as error:
            # Files committed before the failure stay committed; the failed file left no trace.
            message = str(error) if isinstance(error, IngestError) else ""
            connection.execute(
                "UPDATE _ingest_runs SET status = 'failed', finished_at = ?, error_type = ?, error_message = ? "
                "WHERE run_id = ?",
                (datetime.now(timezone.utc).strftime(_TS_FORMAT), type(error).__name__, message, run_id),
            )
            raise
        connection.execute(
            "UPDATE _ingest_runs SET status = 'succeeded', finished_at = ? WHERE run_id = ?",
            (datetime.now(timezone.utc).strftime(_TS_FORMAT), run_id),
        )
        summary["status"] = "succeeded"
        return summary
    finally:
        connection.close()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Idempotent incremental CSV ingest into a local SQLite file.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--state-db", type=Path)
    parser.add_argument("--mode", choices=MODES, default="incremental")
    parser.add_argument("--from", dest="backfill_from", help="inclusive backfill start, ISO-8601 with offset")
    parser.add_argument("--to", dest="backfill_to", help="exclusive backfill end, ISO-8601 with offset")
    parser.add_argument("--validate-only", action="store_true", help="check the manifest and exit")
    args = parser.parse_args(argv)
    try:
        streams = load_manifest(args.manifest)
        if args.validate_only:
            print(json.dumps({"status": "manifest_valid", "streams": [s.name for s in streams]}))
            return 0
        if args.input_dir is None or args.state_db is None:
            parser.error("--input-dir and --state-db are required unless --validate-only is set")
        summary = ingest(streams, args.input_dir, args.state_db, args.mode, args.backfill_from, args.backfill_to)
    except IngestError as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__, "error": str(error)}), file=sys.stderr)
        return 2 if isinstance(error, ManifestError) else 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
