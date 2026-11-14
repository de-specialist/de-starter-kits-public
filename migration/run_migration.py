#!/usr/bin/env python3
"""Load CSV files or selected SQL tables into an isolated Bronze namespace."""

from __future__ import annotations

import argparse
import base64
import csv
from contextlib import contextmanager
import json
import logging
import math
import os
import re
import stat
import sys
import tempfile
import threading
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from time import monotonic
from urllib.parse import quote

from _private_json import create_private_json, update_private_json
from mapping_contract import (
    MAPPING_CASTS as _MAPPING_CASTS,
    MAX_MAPPING_FILE_BYTES as _MAX_MAPPING_FILE_BYTES,
    MAX_MAPPING_SOURCE_NAME_LENGTH as _MAX_MAPPING_SOURCE_NAME_LENGTH,
    RETAIL_CONTRACT as _RETAIL_CONTRACT,
    RETAIL_TESTS as _RETAIL_TESTS,
    MappingConfigurationError,
    load_mapping_file,
    mappings_fingerprint,
    unique_json_object as _unique_json_object,
    valid_mapping_source_name as _valid_mapping_source_name,
)
from destination_type_profiles import (
    DestinationTypeBoundError,
    DestinationTypeProfile,
    check_mapped_value,
    resolve_type_profile,
)
from schema_drift import (
    SCHEMA_CONFIRMATION_ALGORITHM as _SCHEMA_CONFIRMATION_ALGORITHM,
    SNAPSHOT_ALGORITHM as _SCHEMA_SNAPSHOT_ALGORITHM,
    SchemaBaselineError,
    drift_review,
    load_reviewed_schema,
    schema_confirmation_fingerprint,
    schema_snapshot,
    snapshot_name_fingerprint,
    snapshot_typed_fingerprint,
)

# Pin dlt's project settings before importing it. dlt 1.32.0 resolves project TOML
# from DLT_PROJECT_DIR/.dlt; telemetry is disabled before runtime initialization.
MIGRATION_ROOT = Path(__file__).resolve().parent
os.environ["DLT_PROJECT_DIR"] = str(MIGRATION_ROOT)
os.environ["DLT_CONFIG_FOLDER"] = ".dlt"
os.environ["RUNTIME__DLTHUB_TELEMETRY"] = "false"
logging.disable(logging.CRITICAL)

import dlt
from sqlalchemy import MetaData, Table, create_engine, event, literal, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import NoSuchTableError
from dlt.sources.sql_database import sql_database
from dlt.common.schema import Schema
from dlt.common.schema.typing import DLT_NAME_PREFIX
from dlt.common.normalizers.naming.snake_case import NamingConvention as SnakeCaseNamingConvention


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# The target components intentionally allow underscores used in Snowflake account/database identifiers.
_SAFE_TARGET_ID = re.compile(r"^[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)?$")
# Operator-asserted source binding alias. It is a generic label only: never a credential,
# never read from env/config, and never proof of source identity. Used with fullmatch.
_SOURCE_BINDING_ID = re.compile(r"[a-z][a-z0-9_]{0,39}")
_SOURCE_BINDING_ID_ERROR = (
    "must be 1-40 characters: a lowercase ASCII letter followed by lowercase ASCII letters, digits, or underscores."
)
_RESERVED_METADATA_COLUMNS = {
    "_migration_run_id",
    "_ingested_at_utc",
    "_source_file_name",
    "_source_entity",
}
_DEFAULT_NAME_NORMALIZER = SnakeCaseNamingConvention().normalize_identifier
_DEFAULT_MAX_ROWS_PER_TABLE = 250_000
_DEFAULT_MAX_BYTES_PER_ROW = 16 * 1024 * 1024
_DEFAULT_MAX_BYTES_PER_TABLE = 256 * 1024 * 1024
_DEFAULT_MAX_ROWS_TOTAL = 1_000_000
_DEFAULT_MAX_BYTES_TOTAL = 1024 * 1024 * 1024
_SQL_CONNECT_TIMEOUT_SECONDS = 10
_SQL_STATEMENT_TIMEOUT_MS = 60_000
_SQL_LOCK_TIMEOUT_MS = 10_000
_SAFE_CLI_MESSAGES = {
    "target_confirmation_mismatch": "--confirm-target does not match the resolved target; copy the exact identity from --dry-run.",
    "table_confirmation_mismatch": "--confirm-tables does not match the resolved destination tables; copy the exact list from --dry-run.",
    "schema_confirmation_required": "A 64-character --confirm-schema-fingerprint from dry-run is required before loading.",
    "schema_confirmation_mismatch": "The current source schema or mapping differs from the reviewed fingerprint; run --dry-run again.",
    "schema_baseline_unusable": "--schema-baseline must be an owner-only dry-run report or run manifest whose schema snapshot matches its recorded fingerprints.",
    "schema_drift_requires_fresh_confirmation": "The source schema or mapping drifted from the reviewed baseline (field-level drift printed above); review it, run --dry-run again, and confirm the new fingerprint.",
    "csv_source_directory_required": "CSV source directory is unavailable; check --csv-dir.",
    "sql_source_selection_required": "SQL sources require --source-schema and an explicit --tables list.",
    "sql_source_url_unset": "The SQL source URL environment variable is unset; configure the selected source URL variable.",
    "source_inputs_missing": "Requested source inputs are missing; check the selected source tables, directory, and file glob.",
    "source_inputs_unmatched": "No source inputs matched the selected directory and file glob.",
    "unsupported_retail_contract_option": "--check-retail-contract is available only with --dry-run.",
    "unsupported_profile_option": "--profile-retail-values is available only with --dry-run.",
    "pending_packages": "This migration pipeline has pending dlt packages; resolve them before starting another replacement.",
    "load_evidence_incomplete": "The loader did not receive complete evidence for the destination load; inspect the private manifest before retrying.",
    "load_jobs_failed": "One or more destination load jobs failed; inspect the private manifest and destination job history.",
    "namespace_missing": "The confirmed cloud namespace does not exist; pre-create it before loading.",
    "namespace_probe_failed": "The confirmed cloud namespace could not be verified immediately before loading; no load was started.",
}


class SafeCliError(Exception):
    """A fixed, application-owned diagnostic that is safe to show to an operator."""

    def __init__(self, code: str):
        if code not in _SAFE_CLI_MESSAGES:
            raise ValueError("Unknown safe CLI diagnostic code.")
        self.code = code
        super().__init__()


def _raise_safe_cli(code: str) -> None:
    raise SafeCliError(code)

# Input contract (_RETAIL_CONTRACT, _RETAIL_TESTS) lives in mapping_contract.py.
_RETAIL_KEY_FIELDS = {
    tuple(value.split(".")) for value in _RETAIL_TESTS["unique_not_null"]
}
for _relationship in _RETAIL_TESTS["relationships"]:
    _child_key, _parent_key = _relationship.split(" -> ")
    _RETAIL_KEY_FIELDS.add(tuple(_child_key.split(".")))
    _RETAIL_KEY_FIELDS.add(tuple(_parent_key.split(".")))


def parse_tables(value: str | None) -> list[str] | None:
    if value is None:
        return None
    names = [item.strip() for item in value.split(",") if item.strip()]
    if not names:
        raise ValueError("At least one table name is required when --tables is supplied.")
    if any(not _SAFE_IDENTIFIER.fullmatch(name) for name in names):
        raise ValueError("Table names may contain letters, numbers, and underscores only.")
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("Table names must not repeat.")
    return names


def _csv_reader(stream):
    reader = csv.DictReader(stream)
    if not reader.fieldnames:
        raise ValueError("CSV must have a header row.")
    normalized_headers = [name.strip().casefold() for name in reader.fieldnames if name is not None]
    if len(normalized_headers) != len(reader.fieldnames) or any(not name for name in normalized_headers):
        raise ValueError("CSV headers must be non-empty text values.")
    if len(set(normalized_headers)) != len(normalized_headers):
        raise ValueError("CSV headers must be unique, ignoring case and surrounding spaces.")
    reader.fieldnames = [name.strip() for name in reader.fieldnames]
    return reader


def _validated_csv_rows(reader):
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError("Every CSV row must have exactly the same number of fields as its header.")
        yield row


@contextmanager
def open_csv_input(path: Path):
    """Open a CSV without following symlinks, then verify the opened inode is regular."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise OSError("Safe CSV opening is unavailable on this platform.")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("CSV input must be a regular file.")
        with os.fdopen(descriptor, "r", newline="", encoding="utf-8-sig") as stream:
            descriptor = -1
            yield stream
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def count_csv_rows(path: Path) -> int:
    with open_csv_input(path) as stream:
        return sum(1 for _ in _validated_csv_rows(_csv_reader(stream)))


def discover_csv_files(directory: Path, glob: str) -> list[Path]:
    """Apply the single CSV selection policy used by inventory, extraction, and identity metadata."""
    if not directory.is_dir():
        raise ValueError("CSV source directory does not exist or is not a directory.")
    pattern = Path(glob)
    if pattern.is_absolute() or ".." in pattern.parts:
        raise ValueError("CSV glob must stay within the selected source directory.")
    source_root = directory.resolve()
    candidates = sorted(directory.glob(glob))
    if any(
        component.is_symlink()
        for path in candidates
        for component in (path, *path.parents)
        if component == source_root or source_root in component.parents
    ):
        raise ValueError("CSV source files must not be symlinks.")
    files = sorted(
        (path for path in candidates
         if path.is_file() and path.suffix.lower() == ".csv" and path.resolve().is_relative_to(source_root)),
        key=lambda path: path.name.casefold(),
    )
    stems = [path.stem.casefold() for path in files]
    if len(set(stems)) != len(stems):
        raise ValueError("CSV files must not have duplicate names after conversion to lowercase table names.")
    return files


def inventory_csv_inputs(directory: Path, glob: str, table_names: list[str] | None, *, count_rows: bool = True) -> list[dict[str, object]]:
    """Describe selected files without loading them; CSV columns remain untyped text."""
    files = discover_csv_files(directory, glob)
    by_name: dict[str, Path] = {}
    for path in files:
        key = path.stem.casefold()
        by_name[key] = path

    wanted = {name.casefold() for name in table_names} if table_names is not None else set(by_name)
    selected_files = sorted((key, path) for key, path in by_name.items() if key in wanted)
    selected = [key for key, _ in selected_files]
    selected.extend(sorted(wanted - set(selected)))
    inventory: list[dict[str, object]] = []
    for name in selected:
        path = by_name.get(name)
        if path is None:
            inventory.append({"table": name, "status": "missing", "columns": [], "row_count": None})
            continue
        if not _SAFE_IDENTIFIER.fullmatch(path.stem):
            raise ValueError("CSV filenames used as table names must contain letters, numbers, and underscores only.")
        with open_csv_input(path) as stream:
            reader = _csv_reader(stream)
            columns = [{"name": column, "type": "CSV source field text; destination inference unverified"} for column in reader.fieldnames]
            row_count = sum(1 for _ in _validated_csv_rows(reader)) if count_rows else None
        inventory.append({
            "table": path.stem,
            "status": "empty" if row_count == 0 else "ready" if row_count is not None else "not_counted",
            "columns": columns,
            "row_count": row_count,
        })
    if not inventory:
        inventory.append({"table": None, "status": "no_matching_files", "columns": [], "row_count": None})
    return sorted(inventory, key=lambda item: str(item["table"]).casefold())


def make_csv_resources(
    directory: Path,
    glob: str,
    table_names: list[str] | None,
    run_id: str,
    ingested_at: str,
    allow_empty_tables: set[str],
    budget,
    client,
    expected_schema_by_table: dict[str, dict[str, object]],
    observed_schema_by_table: dict[str, dict[str, object]],
    mappings: dict[str, list[dict[str, str]]],
    *,
    type_profile: DestinationTypeProfile,
):
    _require_type_profile(type_profile)
    files = discover_csv_files(directory, glob)
    if table_names is not None:
        wanted = {name.casefold() for name in table_names}
        files = [path for path in files if path.stem.casefold() in wanted]
        missing = wanted - {path.stem.casefold() for path in files}
        if missing:
            _raise_safe_cli("source_inputs_missing")
    if not files:
        _raise_safe_cli("source_inputs_unmatched")
    resources = []
    actual_counts: dict[str, int | None] = {}
    for path in files:
        if not _SAFE_IDENTIFIER.fullmatch(path.stem):
            raise ValueError("CSV filenames used as table names must contain letters, numbers, and underscores only.")
        @dlt.resource(name=path.stem.lower(), write_disposition="replace")
        def csv_rows(file_path: Path = path):
            budget.start_table(file_path.stem.lower())
            yielded_rows = 0
            try:
                with open_csv_input(file_path) as stream:
                    reader = _csv_reader(stream)
                    header_inventory = [{
                        "table": file_path.stem,
                        "columns": [{"name": column, "type": "CSV source field text"} for column in reader.fieldnames],
                    }]
                    expected_header = expected_schema_by_table.get(file_path.stem.casefold())
                    if expected_header is None or schema_fingerprint(header_inventory) != schema_fingerprint([expected_header]):
                        budget.reject(file_path.stem.lower(), 0, "source_schema_changed_since_preflight")
                        raise ExtractionRejected("CSV source schema changed since preflight")
                    if not analyze_normalized_columns(inventory_with_mapping_columns(header_inventory, mappings), client)["safe_to_extract"]:
                        budget.reject(file_path.stem.lower(), 0, "unsafe_actual_csv_schema")
                        raise ExtractionRejected("CSV header safety check rejected")
                    observed_schema_by_table[file_path.stem.casefold()] = header_inventory[0]
                    for ordinal, row in enumerate(_validated_csv_rows(reader), start=1):
                        row = apply_mappings_with_rejects(
                            row, file_path.stem.lower(), mappings.get(file_path.stem.casefold(), []), budget, ordinal,
                            type_profile=type_profile,
                        )
                        row["_migration_run_id"] = run_id
                        row["_ingested_at_utc"] = ingested_at
                        row["_source_file_name"] = file_path.name
                        budget.consume(file_path.stem.lower(), ordinal, row)
                        yielded_rows += 1
                        yield row
            except ExtractionRejected:
                raise
            except Exception:
                budget.reject(file_path.stem.lower(), yielded_rows + 1, "malformed_or_unreadable_csv")
                raise ExtractionRejected("CSV extraction rejected") from None
            # Only publish the count after the generator was fully consumed.
            actual_counts[file_path.stem.lower()] = yielded_rows
            budget.complete_table(file_path.stem.lower())

        resource = csv_rows()
        resources.append(resource)
        actual_counts[path.stem.lower()] = None
    return resources, [path.stem.lower() for path in files], actual_counts


def create_read_only_sql_engine(connection_url: str):
    """Build a bounded, read-only engine for the deliberately supported SQL drivers."""
    url = make_url(connection_url)
    backend = url.get_backend_name()
    driver = url.get_driver_name()
    if backend == "sqlite" and driver == "pysqlite":
        if url.query:
            raise ValueError("SQLite source URLs with query overrides are unsupported; provide a plain file URL.")
        if not url.database or url.database in {":memory:", ""}:
            raise ValueError("SQLite sources must be existing database files; in-memory sources are unsupported.")
        source_path = Path(url.database).expanduser()
        if not source_path.is_absolute():
            source_path = Path.cwd() / source_path
        try:
            source_path = source_path.resolve(strict=True)
        except OSError:
            raise ValueError("SQLite source file must already exist.") from None
        if not source_path.is_file():
            raise ValueError("SQLite source must be an existing regular file.")
        # SQLite URI paths must percent-encode URI delimiters that can appear in
        # ordinary filenames. Preserve separators and drive-letter colons while
        # encoding literal percent signs so sequences such as "%2F" stay literal.
        sqlite_uri_path = quote(source_path.as_posix(), safe="/:")
        readonly_url = url.set(
            database=f"file:{sqlite_uri_path}",
            query={"mode": "ro", "immutable": "1", "uri": "true", "timeout": str(_SQL_CONNECT_TIMEOUT_SECONDS)},
        )
        engine = create_engine(readonly_url, pool_pre_ping=True)
        @event.listens_for(engine, "connect")
        def _sqlite_read_only(dbapi_connection, connection_record):
            dbapi_connection.execute("PRAGMA query_only=ON")
            state = dbapi_connection.execute("PRAGMA query_only").fetchone()
            if not state or state[0] != 1:
                raise RuntimeError("SQLite read-only mode could not be verified")
            deadline = {"at": 0.0}
            connection_record.info["migration_statement_deadline"] = deadline
            dbapi_connection.set_progress_handler(
                lambda: int(bool(deadline["at"]) and monotonic() >= deadline["at"]), 1000
            )

        @event.listens_for(engine, "before_cursor_execute")
        def _sqlite_statement_deadline(connection, cursor, statement, parameters, context, executemany):
            deadline = connection.info.get("migration_statement_deadline")
            if deadline is not None:
                deadline["at"] = monotonic() + (_SQL_STATEMENT_TIMEOUT_MS / 1000)
        return engine

    if backend == "postgresql" and driver == "psycopg":
        if any(key in url.query for key in ("options", "connect_timeout")):
            raise ValueError("PostgreSQL URLs must not override the migration's read-only and timeout settings.")
        hosts = (url.host or url.query.get("host") or "").split(",")
        host_addrs = str(url.query.get("hostaddr", "")).split(",")
        if len(hosts) > 1 or len(host_addrs) > 1:
            raise ValueError("PostgreSQL multi-host source URLs are unsupported by the bounded connection timeout.")
        connect_options = (
            "-c default_transaction_read_only=on "
            f"-c statement_timeout={_SQL_STATEMENT_TIMEOUT_MS} "
            f"-c lock_timeout={_SQL_LOCK_TIMEOUT_MS}"
        )
        engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": _SQL_CONNECT_TIMEOUT_SECONDS, "options": connect_options},
        )

        @event.listens_for(engine, "connect")
        def _postgres_read_only(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(
                    "SELECT current_setting('default_transaction_read_only'), "
                    "(SELECT setting::integer FROM pg_settings WHERE name='statement_timeout'), "
                    "(SELECT setting::integer FROM pg_settings WHERE name='lock_timeout')"
                )
                readonly, statement_timeout, lock_timeout = cursor.fetchone()
            finally:
                cursor.close()
            if str(readonly).casefold() not in {"on", "true", "1"}:
                raise RuntimeError("PostgreSQL read-only transaction mode could not be verified")
            if not (0 < int(statement_timeout) <= _SQL_STATEMENT_TIMEOUT_MS):
                raise RuntimeError("PostgreSQL statement timeout could not be verified")
            if not (0 < int(lock_timeout) <= _SQL_LOCK_TIMEOUT_MS):
                raise RuntimeError("PostgreSQL statement and lock timeouts could not be verified")
        return engine

    raise ValueError("Only read-only SQLite files and PostgreSQL via psycopg are supported SQL sources.")


def preflight_sql_rows(engine, schema: str, table_names: list[str], allow_empty_tables: set[str]) -> None:
    """Check the selected SQL tables are present and non-empty before any replace load."""
    metadata = MetaData()
    with engine.connect() as connection:
        for table_name in table_names:
            table = Table(table_name, metadata, schema=schema, autoload_with=connection)
            has_row = connection.execute(select(literal(1)).select_from(table).limit(1)).first() is not None
            if not has_row and table_name.casefold() not in allow_empty_tables:
                raise ValueError(
                    "An empty SQL source table cannot replace a Bronze table unless named in --allow-empty-tables."
                )


def inventory_sql_inputs(engine, schema: str, table_names: list[str]) -> list[dict[str, object]]:
    """Reflect selected SQL tables and probe emptiness without counting all rows."""
    if not schema or not _SAFE_IDENTIFIER.fullmatch(schema):
        raise ValueError("Source schema must contain letters, numbers, and underscores only.")
    inventory: list[dict[str, object]] = []
    metadata = MetaData()
    with engine.connect() as connection:
        for table_name in table_names:
            try:
                table = Table(table_name, metadata, schema=schema, autoload_with=connection)
            except NoSuchTableError:
                inventory.append({"table": table_name, "status": "missing", "columns": [], "row_count": None})
                continue
            has_row = connection.execute(select(literal(1)).select_from(table).limit(1)).first() is not None
            has_reserved_columns = bool(
                _RESERVED_METADATA_COLUMNS.intersection(column.name.casefold() for column in table.columns)
            )
            inventory.append({
                "table": table_name,
                "status": (
                    "blocked_reserved_metadata_columns" if has_reserved_columns
                    else "ready" if has_row
                    else "empty"
                ),
                "columns": [{"name": column.name, "type": str(column.type)} for column in table.columns],
                "row_count": None,
                "row_count_note": "Not counted; a bounded existence probe was used.",
            })
    return sorted(inventory, key=lambda item: str(item["table"]).casefold())


def schema_fingerprint(inventory: list[dict[str, object]]) -> str:
    """Hash selected table and ordered column names, without reading row values."""
    return snapshot_name_fingerprint(schema_snapshot(inventory))


def typed_schema_fingerprint(inventory: list[dict[str, object]]) -> str:
    """Hash names plus normalized type families; never inspect row values."""
    return snapshot_typed_fingerprint(schema_snapshot(inventory))


def require_schema_confirmation(confirmed: str | None, expected: str) -> None:
    if not isinstance(confirmed, str) or not re.fullmatch(r"[0-9a-f]{64}", confirmed):
        _raise_safe_cli("schema_confirmation_required")
    if confirmed != expected:
        _raise_safe_cli("schema_confirmation_mismatch")


def enforce_schema_baseline(
    baseline_path: Path | None,
    caller_cwd: Path,
    inventory: list[dict[str, object]],
    mapping_fingerprint: str,
    expected_confirmation: str,
    confirmed: str | None,
) -> dict[str, object] | None:
    """Refuse replacement when the source drifted from a reviewed baseline without fresh confirmation.

    The field-level drift (names and type families only) is printed so the operator can see
    what changed; reconfirming the stale baseline fingerprint is never sufficient.
    """
    if baseline_path is None:
        return None
    try:
        baseline = load_reviewed_schema((caller_cwd / baseline_path).expanduser())
    except SchemaBaselineError:
        _raise_safe_cli("schema_baseline_unusable")
    review = drift_review(
        baseline, schema_snapshot(inventory), mapping_fingerprint, expected_confirmation, confirmed
    )
    if not review["fresh_confirmation_verified"]:
        print(json.dumps({"schema_drift_review": review}, indent=2), file=sys.stderr)
        _raise_safe_cli("schema_drift_requires_fresh_confirmation")
    return review


_MAPPING_DLT_DATA_TYPES = {
    "string": "text",
    "integer": "bigint",
    "numeric": "decimal",
    "date": "date",
    "timestamp": "timestamp",
    "boolean": "bool",
}


class PipelineNameTooLongError(ValueError):
    """Safe configuration error for dlt's generated schema-name length limit."""


# The mapping-file parser lives in mapping_contract.py (dependency-free, shared with the scaffolder).


def validate_mappings(inventory: list[dict[str, object]], mappings: dict[str, list[dict[str, str]]]) -> dict[str, object]:
    available_column_names = {
        str(item["table"]).casefold(): [
            str(column["name"]) for column in item.get("columns", [])
            if isinstance(column, dict) and "name" in column
        ]
        for item in inventory if isinstance(item.get("table"), str)
    }
    available = {
        table: {name.casefold() for name in names}
        for table, names in available_column_names.items()
    }
    actual_table_names = {
        str(item["table"]).casefold(): str(item["table"])
        for item in inventory if isinstance(item.get("table"), str)
    }
    available_normalized = {
        table: {_DEFAULT_NAME_NORMALIZER(name).casefold() for name in names}
        for table, names in available_column_names.items()
    }
    # Compare mappings against both the source spelling and its dlt-normalized
    # destination spelling. Keep these sets separate from `available`, which is
    # used for case-insensitive source lookup and must not be normalized.
    existing_destination_names = {
        table: available[table] | available_normalized[table]
        for table in available
    }
    errors = []
    mapped = []
    normalized_contract = {
        _DEFAULT_NAME_NORMALIZER(table).casefold(): {
            _DEFAULT_NAME_NORMALIZER(column).casefold(): cast for column, cast in fields.items()
        }
        for table, fields in _RETAIL_CONTRACT.items()
    }
    for table, rules in mappings.items():
        if table not in available:
            errors.append({"table": table, "reason": "source_table_missing"})
            continue
        for rule in rules:
            if rule["source"] not in available_column_names[table]:
                errors.append({"table": table, "reason": "source_column_missing", "source": rule["source"]})
            else:
                target_normalized = _DEFAULT_NAME_NORMALIZER(rule["target"]).casefold()
                source_normalized = _DEFAULT_NAME_NORMALIZER(rule["source"]).casefold()
                target_matches = [
                    name for name in available_column_names[table]
                    if name.casefold() == rule["target"].casefold()
                    or _DEFAULT_NAME_NORMALIZER(name).casefold() == target_normalized
                ]
                replaces_own_normalized_name = (
                    source_normalized == target_normalized
                    and len(target_matches) == 1
                    and target_matches[0] == rule["source"]
                )
                target_exists = (
                    rule["target"].casefold() in existing_destination_names[table]
                    or target_normalized in existing_destination_names[table]
                )
                if target_exists and not replaces_own_normalized_name:
                    errors.append({"table": table, "reason": "target_column_already_exists", "target": rule["target"]})
                else:
                    contract_table = _DEFAULT_NAME_NORMALIZER(actual_table_names[table]).casefold()
                    contract_column = target_normalized
                    expected_cast = normalized_contract.get(contract_table, {}).get(contract_column)
                    if expected_cast is not None and rule["cast"] != expected_cast:
                        errors.append({
                            "table": table,
                            "reason": "cast_does_not_match_retail_contract",
                            "target": rule["target"],
                            "expected_cast": expected_cast,
                        })
                    else:
                        mapped.append({"table": table, **rule})
    return {"valid": not errors, "mappings": mapped, "errors": errors}


def inventory_with_mapping_columns(inventory: list[dict[str, object]], mappings: dict[str, list[dict[str, str]]]) -> list[dict[str, object]]:
    augmented = []
    for item in inventory:
        clone = {**item, "columns": [dict(column) for column in item.get("columns", []) if isinstance(column, dict)]}
        table = str(item.get("table", "")).casefold()
        clone["columns"].extend(
            {"name": rule["target"], "type": rule["cast"]}
            for rule in mappings.get(table, [])
            if _DEFAULT_NAME_NORMALIZER(rule["source"]).casefold()
            != _DEFAULT_NAME_NORMALIZER(rule["target"]).casefold()
        )
        augmented.append(clone)
    return augmented


def _cast_mapped_value(value, cast: str):
    if value is None:
        return None
    if cast == "string":
        if isinstance(value, (bytes, bytearray, memoryview)):
            raise ValueError("mapped_value_cast_failed")
        return str(value)
    try:
        if cast == "integer":
            if isinstance(value, bool):
                raise ValueError
            converted = int(value)
            if not isinstance(value, str) and value != converted:
                raise ValueError
            return converted
        if cast == "numeric":
            converted = Decimal(str(value))
            if not converted.is_finite():
                raise ValueError
            return converted
        if cast == "date":
            return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value))
        if cast == "timestamp":
            if isinstance(value, datetime):
                return value
            timestamp_text = str(value)
            if any(len(fraction) > 6 for fraction in re.findall(r"[.,](\d+)", timestamp_text)):
                raise ValueError
            return datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
        if cast == "boolean":
            if isinstance(value, bool):
                return value
            normalized = str(value).strip().casefold()
            if normalized in {"true", "1", "yes", "y"}:
                return True
            if normalized in {"false", "0", "no", "n"}:
                return False
            raise ValueError
    except Exception:
        raise ValueError("mapped_value_cast_failed") from None
    raise ValueError("unsupported_mapping_cast")


def _is_ascii_space_blank(value) -> bool:
    """Treat only empty strings and ASCII U+0020-only strings as blank."""
    return isinstance(value, str) and value.replace(" ", "") == ""


def apply_mappings(row: dict, rules: list[dict[str, str]], type_profile: DestinationTypeProfile | None = None) -> dict:
    """Cast mapped columns; with a profile, also enforce destination numeric bounds."""
    mapped = dict(row)
    for rule in rules:
        source_key = rule["source"]
        if source_key not in row:
            raise ValueError("mapped_source_column_missing")
        value = _cast_mapped_value(row[source_key], rule["cast"])
        if type_profile is not None:
            check_mapped_value(value, rule["cast"], type_profile)
        if _DEFAULT_NAME_NORMALIZER(source_key).casefold() == _DEFAULT_NAME_NORMALIZER(rule["target"]).casefold():
            mapped.pop(source_key, None)
        mapped[rule["target"]] = value
    return mapped


def _require_type_profile(type_profile: object) -> DestinationTypeProfile:
    # Load paths must never fall back to unbounded casts.
    if not isinstance(type_profile, DestinationTypeProfile):
        raise ValueError("A destination numeric type profile is required before extracting mapped rows.")
    return type_profile


def apply_mappings_with_rejects(row: dict, table: str, rules: list[dict[str, str]], budget, ordinal: int | None = None,
                                *, type_profile: DestinationTypeProfile) -> dict:
    _require_type_profile(type_profile)
    try:
        return apply_mappings(row, rules, type_profile)
    except DestinationTypeBoundError as error:
        reason = error.reason_code
    except Exception:
        reason = "mapping_cast_failed"
    if ordinal is None:
        ordinal = budget.tables.get(table, {}).get("rows", 0) + 1
    budget.reject(table, ordinal, reason)
    raise ExtractionRejected("Mapping cast rejected")


def analyze_normalized_columns(inventory: list[dict[str, object]], client) -> dict[str, object]:
    """Check source and generated fields under the exact dlt destination naming rules."""
    naming = client.schema.naming
    casefold_identifier = client.capabilities.casefold_identifier
    normalize = lambda value: casefold_identifier(naming.normalize_identifier(value))
    dlt_prefix = normalize(DLT_NAME_PREFIX)
    generated = {
        "_migration_run_id",
        "_ingested_at_utc",
        "_source_file_name",
        "_source_entity",
    }
    generated_normalized = {normalize(name): name for name in generated}
    tables: list[dict[str, object]] = []
    has_collision = False
    for item in inventory:
        table = item.get("table")
        normalized_table = normalize(str(table)) if table is not None else None
        reserved_table = bool(normalized_table and normalized_table.startswith(dlt_prefix))
        columns = [str(column["name"]) for column in item.get("columns", []) if isinstance(column, dict) and "name" in column]
        normalized: dict[str, list[str]] = {}
        reserved: list[dict[str, str]] = []
        lineage: list[dict[str, str]] = []
        for name in columns:
            output_name = normalize(name)
            normalized.setdefault(output_name, []).append(name)
            if output_name.startswith(dlt_prefix):
                reserved.append({"source": name, "destination": output_name})
            if output_name in generated_normalized:
                lineage.append({"source": name, "destination": output_name, "generated_field": generated_normalized[output_name]})
        collisions = [
            {"destination": output_name, "source_columns": source_names}
            for output_name, source_names in sorted(normalized.items())
            if len(source_names) > 1
        ]
        unsafe = bool(reserved_table or collisions or reserved or lineage)
        has_collision = has_collision or unsafe
        tables.append({
            "table": table,
            "normalized_destination_table": normalized_table,
            "status": "unsafe_normalized_identifier" if unsafe else "safe",
            "normalized_columns": [
                {"source": name, "destination": normalize(name)} for name in columns
            ],
            "collisions_after_normalization": collisions,
            "dlt_reserved_prefix_columns": reserved,
            "dlt_reserved_prefix_table": reserved_table,
            "generated_lineage_collisions": lineage,
        })
    return {
        "status": "unsafe" if has_collision else "safe",
        "safe_to_extract": not has_collision,
        "normalized_dlt_reserved_prefix": dlt_prefix,
        "generated_lineage_fields": sorted(generated_normalized),
        "tables": tables,
        "note": "Checks use dlt destination schema naming and case folding; normalized aliases appear beside their original source names.",
    }


def _sql_type_fit(source_type: str, expected: str, *, is_key: bool = False) -> str:
    """Classify reflected SQL types conservatively against the explicit dbt cast."""
    value = source_type.casefold()
    if expected == "string":
        if is_key and any(token in value for token in ("numeric", "decimal", "number", "real", "float", "double")):
            return "numeric_key_stringification_unverified"
        return "cast_by_model" if any(token in value for token in ("char", "text", "string", "uuid", "int", "numeric", "decimal")) else "review_required"
    if expected == "timestamp":
        if "timestamp" in value or "datetime" in value:
            return "compatible"
        return "review_required"
    if expected == "date":
        if value.strip() == "date" or value.startswith("date("):
            return "compatible"
        if "timestamp" in value or "datetime" in value:
            return "potentially_lossy"
        return "review_required"
    if expected == "integer":
        if any(token in value for token in ("int", "serial")) and not any(token in value for token in ("interval", "point")):
            return "compatible"
        if "number" in value:
            scale = re.search(r"number\s*\([^,]+,\s*(\d+)\s*\)", value)
            return "compatible" if scale and scale.group(1) == "0" else "potentially_lossy"
        if any(token in value for token in ("numeric", "decimal", "real", "float", "double")):
            return "potentially_lossy"
        return "review_required"
    if expected == "numeric":
        if any(token in value for token in ("numeric", "decimal", "number", "int", "serial")):
            return "compatible"
        if any(token in value for token in ("real", "float", "double")):
            return "potentially_lossy"
        return "review_required"
    return "review_required"


def _destination_name_normalizers(client):
    naming = client.schema.naming
    casefold = client.capabilities.casefold_identifier

    def column_name(value: str) -> str:
        return casefold(naming.normalize_identifier(value))

    def table_name(value: str) -> str:
        return casefold(naming.normalize_table_identifier(value))

    return column_name, table_name


def retail_contract_report(inventory: list[dict[str, object]], source_kind: str, client) -> dict[str, object]:
    """Compare only the explicitly modeled raw fields; no mapping or value coercion is inferred."""
    normalize_column, normalize_table = _destination_name_normalizers(client)
    by_table: dict[str, list[dict[str, object]]] = {}
    for item in inventory:
        if item.get("table") is not None:
            by_table.setdefault(normalize_table(str(item["table"])), []).append(item)
    table_reports: list[dict[str, object]] = []
    missing_tables: list[str] = []
    missing_columns: list[str] = []
    incompatible: list[str] = []
    potentially_lossy: list[str] = []
    review_required: list[str] = []
    ambiguous_columns: list[str] = []
    for table, fields in _RETAIL_CONTRACT.items():
        table_matches = by_table.get(normalize_table(table), [])
        item = table_matches[0] if len(table_matches) == 1 else None
        if item is None or item.get("status") == "missing":
            missing_tables.append(table)
            table_reports.append({"table": table, "status": "ambiguous_table_name" if len(table_matches) > 1 else "missing",
                                 "source_tables": [str(candidate.get("table")) for candidate in table_matches],
                                 "missing_columns": list(fields), "extra_columns": []})
            continue
        columns = item.get("columns", [])
        normalized_groups: dict[str, list[dict[str, object]]] = {}
        for column in columns:
            if isinstance(column, dict) and "name" in column:
                normalized_groups.setdefault(normalize_column(str(column["name"])), []).append(column)
        contract_names = {normalize_column(name) for name in fields}
        missing = sorted(name for name in fields if not normalized_groups.get(normalize_column(name)))
        ambiguous_fields = sorted(
            name for name in fields if len(normalized_groups.get(normalize_column(name), [])) > 1
        )
        extra = sorted(str(column["name"]) for column in columns if isinstance(column, dict) and "name" in column and normalize_column(str(column["name"])) not in contract_names)
        missing_columns.extend(f"{table}.{name}" for name in missing)
        ambiguous_columns.extend(f"{table}.{name}" for name in ambiguous_fields)
        field_results = []
        for name, expected in fields.items():
            candidates = normalized_groups.get(normalize_column(name), [])
            if len(candidates) != 1:
                if len(candidates) > 1:
                    field_results.append({"column": name, "status": "ambiguous_normalized_sources",
                                          "source_columns": [str(candidate["name"]) for candidate in candidates],
                                          "normalized_column": normalize_column(name),
                                          "model_cast_type": expected, "fit": "blocked_ambiguous"})
                continue
            column = candidates[0]
            source_type = str(column.get("type", "unknown"))
            if source_kind == "csv":
                fit = "unverified_csv_source_text"
                review_required.append(f"{table}.{name}")
            else:
                fit = _sql_type_fit(source_type, expected, is_key=(table, name) in _RETAIL_KEY_FIELDS)
                if fit in {"review_required", "potentially_lossy", "numeric_key_stringification_unverified"}:
                    review_required.append(f"{table}.{name}")
                if fit == "review_required":
                    incompatible.append(f"{table}.{name}")
                if fit == "potentially_lossy":
                    potentially_lossy.append(f"{table}.{name}")
            field_results.append({"column": name, "source_column": str(column["name"]),
                                  "normalized_source_column": normalize_column(str(column["name"])),
                                  "source_type": source_type, "model_cast_type": expected, "fit": fit})
        table_reports.append({
            "table": table,
            "status": "ambiguous_normalized_columns" if ambiguous_fields else ("missing_columns" if missing else "columns_present"),
            "missing_columns": missing,
            "ambiguous_columns": [
                {"column": name, "normalized_column": normalize_column(name),
                 "source_columns": [str(candidate["name"]) for candidate in normalized_groups[normalize_column(name)]]}
                for name in ambiguous_fields
            ],
            "extra_columns_ignored_by_models": extra,
            "field_types": field_results,
        })

    unmodeled_tables = sorted(str(items[0].get("table")) for norm, items in by_table.items() if norm not in {normalize_table(name) for name in _RETAIL_CONTRACT})
    if missing_tables or missing_columns or incompatible or ambiguous_columns:
        can_feed: bool | None = False
        overall = "ambiguous_normalized_sources" if ambiguous_columns else "incompatible_or_incomplete"
        readiness = "ambiguous_source_columns" if ambiguous_columns else "contract_incomplete_or_incompatible"
    elif source_kind == "csv" or review_required:
        can_feed = None
        overall = "manual_type_or_value_review_required"
        readiness = "values_or_source_types_need_review"
    else:
        can_feed = True
        overall = "schema_compatible_data_tests_still_required"
        readiness = "schema_compatible_dbt_data_tests_pending"
    return {
        "contract": "canonical_retail_medallion",
        "contract_sources": [
            "editions/Bigquery/models/migration/silver/",
            "editions/Bigquery/models/migration/schema.yml",
            "editions/Snowflake/kit/models/migration/silver/",
            "editions/Snowflake/kit/models/migration/schema.yml",
        ],
        "status": overall,
        "medallion_readiness": readiness,
        "can_feed_retail_models": can_feed,
        "source_types": "CSV field values enter the loader as text; dlt destination-schema inference, variant splitting, and later dbt castability are not validated by this report." if source_kind == "csv" else "Reflected SQLAlchemy source types; cast fit is conservative.",
        "missing_tables": missing_tables,
        "unmodeled_selected_tables": unmodeled_tables,
        "incompatible_columns": incompatible,
        "ambiguous_normalized_columns": ambiguous_columns,
        "potentially_lossy_columns": potentially_lossy,
        "review_required_columns": review_required,
        "required_data_tests_from_model_schema": _RETAIL_TESTS,
        "tables": table_reports,
        "note": "This report is informational. No mapping is inferred; reviewed mappings add aliases while retaining source columns, except that a same-normalized-name target replaces its source in place to avoid a destination collision.",
    }


def make_sql_source(engine, schema: str, table_names: list[str], run_id: str, ingested_at: str, budget, client, expected_schema_fingerprint: str, expected_typed_schema_fingerprint: str, mappings: dict[str, list[dict[str, str]]],
                    *, type_profile: DestinationTypeProfile):
    _require_type_profile(type_profile)
    if not schema or not _SAFE_IDENTIFIER.fullmatch(schema):
        raise ValueError("Source schema must contain letters, numbers, and underscores only.")
    # Reflect once, validate that exact schema, and pass the same MetaData to dlt.
    # sql_database reuses existing Table objects, so its SELECT cannot silently add
    # columns after the identifier-safety check.
    metadata = MetaData(schema=schema)
    reflected_inventory = []
    with engine.connect() as connection:
        for table_name in table_names:
            table = Table(table_name, metadata, schema=schema, autoload_with=connection)
            reflected_inventory.append({
                "table": table.name,
                "columns": [{"name": column.name, "type": str(column.type)} for column in table.columns],
            })
    return_inventory = sorted(reflected_inventory, key=lambda item: str(item["table"]).casefold())
    if schema_fingerprint(return_inventory) != expected_schema_fingerprint:
        raise ValueError("SQL source schema changed since preflight; refusing extraction.")
    if typed_schema_fingerprint(return_inventory) != expected_typed_schema_fingerprint:
        raise ValueError("SQL source type families changed since preflight; refusing extraction.")
    if not analyze_normalized_columns(inventory_with_mapping_columns(reflected_inventory, mappings), client)["safe_to_extract"]:
        raise ValueError("Reflected SQL resource identifiers are unsafe after dlt normalization.")
    source = sql_database(
        credentials=engine,
        schema=schema,
        metadata=metadata,
        table_names=table_names,
        chunk_size=1,
        reflection_level="full_with_precision",
    )
    for resource in source.resources.values():
        entity_name = resource.name
        rules = mappings.get(entity_name.casefold(), [])
        mapped_column_hints = [
            {"name": rule["target"], "data_type": _MAPPING_DLT_DATA_TYPES[rule["cast"]]}
            for rule in rules
        ]
        resource.apply_hints(write_disposition="replace", columns=mapped_column_hints or None)
        def lineage_mapper(entity):
            def mapper(row):
                return {
                    **row,
                    "_migration_run_id": run_id,
                    "_ingested_at_utc": ingested_at,
                    "_source_entity": entity,
                }
            return mapper

        resource.add_map(lineage_mapper(entity_name))
        if rules:
            def mapping_mapper(entity, mapping_rules):
                def mapper(row):
                    return apply_mappings_with_rejects(
                        row, entity, mapping_rules, budget, type_profile=type_profile
                    )
                return mapper

            resource.add_map(mapping_mapper(entity_name, rules))

        def budget_mapper(entity):
            def mapper(row):
                return budget.consume(entity, None, row)
            return mapper

        resource.add_map(budget_mapper(entity_name))
    resolved_names = sorted((resource.name for resource in source.resources.values()), key=str.casefold)
    if {name.casefold() for name in resolved_names} != {name.casefold() for name in table_names}:
        raise ValueError("The SQL source did not resolve exactly the explicitly selected tables.")
    return source, resolved_names, return_inventory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("csv", "sql"), required=True)
    parser.add_argument("--destination", choices=("duckdb", "bigquery", "snowflake"), required=True)
    parser.add_argument("--dataset", help="New isolated Bronze dataset/schema name; required for cloud destinations.")
    parser.add_argument("--target-id", help="Non-secret target identity: BigQuery project ID or Snowflake account/database.")
    parser.add_argument("--confirm-target", help="Type the exact destination identity before replacing selected Bronze tables.")
    parser.add_argument("--confirm-tables", help="Type the exact comma-separated resolved table list before loading.")
    parser.add_argument("--confirm-schema-fingerprint", help="Type the combined schema/mapping fingerprint from dry-run before loading.")
    parser.add_argument("--schema-baseline", type=Path, help="Owner-only earlier dry-run JSON or run manifest; report field-level schema drift against it and refuse replacement unless the current fingerprint is freshly confirmed.")
    parser.add_argument(
        "--source-binding-id", type=source_binding_id, default=None,
        help="Optional non-sensitive operator alias for the source (pattern [a-z][a-z0-9_]{0,39}); pass the same value on dry-run and load. An operator assertion only, never a credential or proof of source identity.",
    )
    parser.add_argument("--allow-empty-tables", help="Comma-separated selected tables explicitly authorized to replace with zero rows.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect selected source inputs and exit without creating pipeline state or writing data.")
    parser.add_argument("--check-retail-contract", action="store_true", help="In --dry-run, report whether inventoried inputs match the included retail Silver/Gold source contract.")
    parser.add_argument("--mapping-file", type=Path, help="Optional versioned JSON file for reviewed source-to-canonical aliases and explicit casts.")
    parser.add_argument("--profile-retail-values", action="store_true", help="In --dry-run, scan a bounded sample and report aggregate retail-field quality counts; values are never printed.")
    parser.add_argument("--profile-max-rows-per-table", type=positive_int, default=10000)
    parser.add_argument("--profile-max-bytes-per-table", type=positive_int, default=64 * 1024 * 1024)
    parser.add_argument("--tables", help="Comma-separated selected entity/table names; required for SQL sources.")
    parser.add_argument("--csv-dir", type=Path, help="Directory containing one CSV file per entity.")
    parser.add_argument("--csv-glob", default="*.csv", help="File glob for the CSV directory (default: *.csv).")
    parser.add_argument("--source-url-env", default="MIGRATION_SOURCE_URL", help="Environment variable holding the SQLAlchemy URL.")
    parser.add_argument("--source-schema", help="Source schema for the SQL database.")
    parser.add_argument("--pipeline-name", default="starterkit_migration", help="Prefix for the generated dlt pipeline/schema name (combined name limit: 64 characters; default: starterkit_migration).")
    parser.add_argument("--max-rows-per-table", type=positive_int, default=_DEFAULT_MAX_ROWS_PER_TABLE)
    parser.add_argument("--max-bytes-per-row", type=positive_int, default=_DEFAULT_MAX_BYTES_PER_ROW)
    parser.add_argument("--max-bytes-per-table", type=positive_int, default=_DEFAULT_MAX_BYTES_PER_TABLE)
    parser.add_argument("--max-rows-total", type=positive_int, default=_DEFAULT_MAX_ROWS_TOTAL)
    parser.add_argument("--max-bytes-total", type=positive_int, default=_DEFAULT_MAX_BYTES_TOTAL)
    return parser


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def source_binding_id(value: str) -> str:
    """Validate the optional operator alias with a fixed, value-free diagnostic."""
    if not isinstance(value, str) or _SOURCE_BINDING_ID.fullmatch(value) is None:
        # ArgumentTypeError text is shown verbatim by argparse; never interpolate the value.
        raise argparse.ArgumentTypeError(_SOURCE_BINDING_ID_ERROR) from None
    return value


class ExtractionRejected(Exception):
    pass


class OversizedPayload(Exception):
    pass


def _json_string_size(value: str, limit: int) -> int:
    size = 2  # surrounding quotes
    short_escapes = {'"', "\\", "\b", "\t", "\n", "\f", "\r"}
    for char in value:
        codepoint = ord(char)
        if char in short_escapes:
            size += 2
        elif codepoint < 0x20:
            size += 6
        elif 0xD800 <= codepoint <= 0xDFFF:
            raise TypeError
        else:
            size += len(char.encode("utf-8"))
        if size > limit:
            raise OversizedPayload
    return size


def _canonical_json_size(value, limit: int) -> int:
    """Measure encoded size without building a whole-row JSON or escaped string copy."""
    if limit < 0:
        raise OversizedPayload
    if value is None:
        return 4
    if value is True:
        return 4
    if value is False:
        return 5
    if isinstance(value, str):
        return _json_string_size(value, limit)
    if isinstance(value, int):
        if value.bit_length() > max(1, limit) * 4:
            raise OversizedPayload
        size = len(str(value))
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError
        size = len(json.dumps(value, allow_nan=False, separators=(",", ":")))
    elif isinstance(value, Decimal):
        if len(value.as_tuple().digits) > limit:
            raise OversizedPayload
        text = str(value)
        size = 13 + _json_string_size(text, max(0, limit - 13))
    elif isinstance(value, (datetime, date, time)):
        key = f"${type(value).__name__}"
        size = 3 + _json_string_size(key, limit) + _json_string_size(value.isoformat(), limit)
    elif isinstance(value, uuid.UUID):
        size = 3 + _json_string_size("$uuid", limit) + _json_string_size(str(value), limit)
    elif isinstance(value, (bytes, bytearray, memoryview)):
        encoded_length = ((len(value) + 2) // 3) * 4
        size = 5 + _json_string_size("$bytes_base64", limit) + encoded_length
    elif isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError
        size = 2
        for index, key in enumerate(sorted(value)):
            if index:
                size += 1
            size += _json_string_size(key, max(0, limit - size)) + 1
            size += _canonical_json_size(value[key], max(0, limit - size))
            if size > limit:
                raise OversizedPayload
    elif isinstance(value, (list, tuple)):
        size = 2
        for index, item in enumerate(value):
            if index:
                size += 1
            size += _canonical_json_size(item, max(0, limit - size))
            if size > limit:
                raise OversizedPayload
    else:
        raise TypeError
    if size > limit:
        raise OversizedPayload
    return size


def _canonical_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError
        return value
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, (datetime, date, time)):
        return {f"${type(value).__name__}": value.isoformat()}
    if isinstance(value, uuid.UUID):
        return {"$uuid": str(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$bytes_base64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise TypeError


def profile_retail_values(source_kind: str, inventory: list[dict[str, object]], csv_dir: Path | None,
                          csv_glob: str, connection_url: str | None, source_schema: str | None,
                          mappings: dict[str, list[dict[str, str]]], max_rows: int, max_bytes: int,
                          client) -> dict[str, object]:
    """Return bounded aggregate quality counts; never retain or emit source values."""
    normalize_column, normalize_table = _destination_name_normalizers(client)
    table_results = []
    sampled_keys: dict[str, set[str]] = {}
    table_sample_complete: dict[str, bool] = {}
    contract_field_presence: dict[str, dict[str, bool]] = {}
    child_key_values: dict[str, list[str] | None] = {}
    csv_paths_by_name = (
        {path.stem.casefold(): path for path in discover_csv_files(csv_dir, csv_glob)}
        if source_kind == "csv" else {}
    )
    for item in inventory:
        raw_table = str(item.get("table", ""))
        raw_table_key = raw_table.casefold()
        table = next((name for name in _RETAIL_CONTRACT if normalize_table(name) == normalize_table(raw_table)), None)
        if table is None or item.get("status") not in {"ready", "empty"}:
            continue
        contract = _RETAIL_CONTRACT[table]
        rules = mappings.get(raw_table_key, [])
        needed = {normalize_column(name) for name in contract}
        mapped_sources = {rule["source"].casefold() for rule in rules
                          if normalize_column(rule["target"]) in needed}
        if source_kind == "csv":
            path = csv_paths_by_name.get(raw_table_key)
            if path is None:
                continue
            csv_context = open_csv_input(path)
            stream = csv_context.__enter__()
            reader = _csv_reader(stream)
            rows = _validated_csv_rows(reader)
            source_columns = reader.fieldnames
            close = lambda: csv_context.__exit__(None, None, None)
        else:
            engine = create_read_only_sql_engine(connection_url)
            try:
                connection = engine.connect()
            except Exception:
                engine.dispose()
                raise
            close = lambda: (connection.close(), engine.dispose())
            try:
                metadata = MetaData()
                sql_table = Table(str(item["table"]), metadata, schema=source_schema, autoload_with=connection)
                selected = [column for column in sql_table.columns
                            if normalize_column(column.name) in needed or column.name.casefold() in mapped_sources]
                if not selected:
                    close()
                    continue
                result = connection.execute(select(*selected).select_from(sql_table).limit(max_rows + 1))
                source_columns = [column.name for column in selected]
                rows = (dict(zip(source_columns, tuple(row))) for row in result)
            except Exception:
                close()
                raise
        source_lookup = {name.casefold(): name for name in source_columns}
        normalized_groups: dict[str, list[str]] = {}
        for source_name in source_columns:
            normalized_groups.setdefault(normalize_column(source_name), []).append(source_name)
        candidates_by_field: dict[str, list[str]] = {}
        for field in contract:
            candidates = list(normalized_groups.get(normalize_column(field), []))
            for rule in rules:
                if normalize_column(rule["target"]) == normalize_column(field):
                    mapped_source = source_lookup.get(rule["source"].casefold())
                    if mapped_source is not None and mapped_source not in candidates:
                        candidates.append(mapped_source)
            candidates_by_field[field] = candidates
        field_state = {name: ("ambiguous" if len(candidates_by_field[name]) > 1 else
                              "available" if candidates_by_field[name] else "missing") for name in contract}
        field_presence = {name: field_state[name] == "available" for name in contract}
        field_counts = {
            name: ({"available": True, "status": "available", "source_column": candidates_by_field[name][0],
                    "normalized_source_column": normalize_column(candidates_by_field[name][0]),
                    "null": 0, "blank": 0, "cast_failure": 0}
                   if field_state[name] == "available" else
                   {"available": False, "status": field_state[name],
                    "ambiguous_source_columns": candidates_by_field[name] if field_state[name] == "ambiguous" else [],
                    "null": None, "blank": None, "cast_failure": None})
            for name in contract
        }
        seen_keys: dict[str, set[tuple[str, ...]]] = {}
        for key in _RETAIL_TESTS["unique_not_null"]:
            key_table, key_column = key.split(".")
            if key_table == table and field_presence.get(key_column, False):
                seen_keys[key_column] = set()
        sampled_rows = sampled_bytes = 0
        key_columns = [key.split(".")[1] for key in _RETAIL_TESTS["unique_not_null"] if key.split(".")[0] == table]
        duplicates = {key: (0 if field_presence.get(key, False) else None) for key in key_columns}
        observed_keys: dict[str, set[str]] = {key: set() for key in seen_keys}
        foreign_keys = {
            "orders.customer_id": "customers.customer_id",
            "order_items.order_id": "orders.order_id",
            "order_items.product_id": "products.product_id",
        }
        for child in foreign_keys:
            child_table, child_column = child.split(".")
            if child_table == table:
                child_key_values[child] = [] if field_presence.get(child_column, False) else None
        truncated = False
        try:
            for row in rows:
                if sampled_rows >= max_rows:
                    truncated = True
                    break
                try:
                    byte_size = _canonical_json_size(row, max_bytes - sampled_bytes)
                except OversizedPayload:
                    truncated = True
                    break
                except Exception:
                    truncated = True
                    break
                sampled_rows += 1
                sampled_bytes += byte_size
                raw_values = {str(k).casefold(): v for k, v in row.items()}
                values = {}
                for field in contract:
                    if field_state[field] == "available":
                        values[normalize_column(field)] = raw_values.get(candidates_by_field[field][0].casefold())
                for name, cast in contract.items():
                    if not field_presence[name]:
                        continue
                    value = values.get(normalize_column(name))
                    if value is None:
                        field_counts[name]["null"] += 1
                    elif _is_ascii_space_blank(value):
                        field_counts[name]["blank"] += 1
                        try:
                            _cast_mapped_value(value, cast)
                        except Exception:
                            field_counts[name]["cast_failure"] += 1
                    else:
                        try:
                            _cast_mapped_value(value, cast)
                        except Exception:
                            field_counts[name]["cast_failure"] += 1
                for key_name, seen in seen_keys.items():
                    value = values.get(normalize_column(key_name))
                    if value is None or _is_ascii_space_blank(value):
                        continue
                    key_value = (str(value),)
                    if key_value in seen:
                        duplicates[key_name] += 1
                    seen.add(key_value)
                    observed_keys[key_name].add(key_value[0])
                for child in child_key_values:
                    child_table, child_column = child.split(".")
                    if child_table == table and child_key_values[child] is not None:
                        value = values.get(normalize_column(child_column))
                        if value is not None and not _is_ascii_space_blank(value):
                            child_key_values[child].append(str(value))
        finally:
            close()
        table_results.append({"table": table, "sampled_rows": sampled_rows, "sampled_bytes": sampled_bytes,
                              "truncated": truncated, "limits": {"rows": max_rows, "bytes": max_bytes},
                              "fields": field_counts, "duplicate_key_counts": duplicates})
        contract_field_presence[table] = field_presence
        table_sample_complete[table] = not truncated
        for key_name, keys in observed_keys.items():
            sampled_keys[f"{table}.{key_name}"] = keys
    relationships = []
    for child, parent in {
        "orders.customer_id": "customers.customer_id",
        "order_items.order_id": "orders.order_id",
        "order_items.product_id": "products.product_id",
    }.items():
        child_values = child_key_values.get(child)
        parent_values = sampled_keys.get(parent)
        if child_values is None or parent_values is None:
            child_table, child_column = child.split(".")
            parent_table, parent_column = parent.split(".")
            if child_table in contract_field_presence and not contract_field_presence[child_table].get(child_column, False):
                status = "unavailable_missing_child_key"
            elif parent_table in contract_field_presence and not contract_field_presence[parent_table].get(parent_column, False):
                status = "unavailable_missing_parent_key"
            else:
                status = "unavailable_not_all_tables_profiled"
            relationships.append({"relationship": f"{child} -> {parent}", "status": status})
            continue
        orphans = sum(value not in parent_values for value in child_values)
        relationships.append({
            "relationship": f"{child} -> {parent}",
            "sampled_non_null_child_keys": len(child_values),
            "sampled_orphan_count": orphans,
            "status": "sample_complete" if table_sample_complete.get(child.split(".")[0], False) and table_sample_complete.get(parent.split(".")[0], False) else "sample_only",
        })
    return {"mode": "bounded_aggregate_only", "source_kind": source_kind,
            "cast_validation": {
                "semantics": "python_conversion_semantics",
                "warehouse_castability": "not_verified",
                "note": "Cast failure counts use Python conversion rules and do not prove that BigQuery or Snowflake casts will accept or preserve the same values.",
            },
            "tables": table_results,
            "relationship_coverage": relationships,
            "privacy": "No source values or key values are included; deterministic counts can still reveal aggregate characteristics."}


def source_identity_evidence(source_kind: str, entities: list[str], csv_dir: Path | None,
                             csv_glob: str, source_schema: str | None) -> dict[str, object]:
    if source_kind == "sql":
        return {"kind": "sql", "schema": source_schema, "tables": sorted(entities, key=str.casefold),
                "connection_identity": "redacted", "identity_scope": "descriptive_names_not_unique"}
    files = []
    selected_paths = {path.stem.casefold(): path for path in discover_csv_files(csv_dir, csv_glob)}
    for entity in sorted(entities, key=str.casefold):
        path = selected_paths.get(entity.casefold())
        if path is None:
            raise ValueError("Selected CSV source file is missing while recording its identity reference.")
        size = path.stat().st_size
        files.append({"entity": entity, "file_name": path.name, "size_bytes": size})
    return {"kind": "csv", "files": files, "identity_scope": "pre_extraction_file_metadata",
            "note": "File names and sizes are captured before extraction; they are descriptive metadata and do not prove which bytes were consumed."}


class ExtractionBudget:
    def __init__(self, args):
        self.limits = {
            "rows_per_table": args.max_rows_per_table,
            "bytes_per_row": args.max_bytes_per_row,
            "bytes_per_table": args.max_bytes_per_table,
            "rows_total": args.max_rows_total,
            "bytes_total": args.max_bytes_total,
        }
        self.tables: dict[str, dict[str, int]] = {}
        self.extraction_status: dict[str, str] = {}
        self.rows_total = self.bytes_total = 0
        self.reject_summary = None
        self.lock = threading.Lock()

    def register_tables(self, tables: list[str]) -> None:
        with self.lock:
            for table in tables:
                self.tables.setdefault(table, {"rows": 0, "bytes": 0})
                self.extraction_status.setdefault(table, "not_started")

    def start_table(self, table: str) -> None:
        with self.lock:
            self.tables.setdefault(table, {"rows": 0, "bytes": 0})
            if self.extraction_status.get(table, "not_started") == "not_started":
                self.extraction_status[table] = "incomplete"

    def complete_table(self, table: str) -> None:
        with self.lock:
            self.tables.setdefault(table, {"rows": 0, "bytes": 0})
            self.extraction_status[table] = "complete"

    def manifest_counts(self, tables: list[str]) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, int]]]:
        source_counts = {}
        accepted_progress = {}
        for table in tables:
            accepted = self.tables.get(table, {"rows": 0, "bytes": 0})
            status = self.extraction_status.get(table, "not_started")
            complete = status == "complete"
            source_counts[table] = {
                "rows": accepted["rows"] if complete else None,
                "payload_bytes": accepted["bytes"] if complete else None,
                "extraction_status": status,
                "provenance": "application_counted_canonical_json_payload",
            }
            accepted_progress[table] = {"rows": accepted["rows"], "payload_bytes": accepted["bytes"]}
        return source_counts, accepted_progress

    def consume(self, table: str, ordinal: int | None, row: dict):
        with self.lock:
            current = self.tables.setdefault(table, {"rows": 0, "bytes": 0})
            if self.extraction_status.get(table, "not_started") == "not_started":
                self.extraction_status[table] = "incomplete"
            row_number = ordinal or current["rows"] + 1
            remaining_bytes = min(
                self.limits["bytes_per_row"],
                self.limits["bytes_per_table"] - current["bytes"],
                self.limits["bytes_total"] - self.bytes_total,
            )
            if current["rows"] + 1 > self.limits["rows_per_table"] or self.rows_total + 1 > self.limits["rows_total"]:
                self._reject_locked(table, row_number, "extraction_budget_exceeded", current)
                raise ExtractionRejected("Extraction budget exceeded")
            try:
                payload_size = _canonical_json_size(row, remaining_bytes)
                payload = json.dumps(_canonical_value(row), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
                if len(payload) != payload_size:
                    raise TypeError
            except OversizedPayload:
                self._reject_locked(table, row_number, "extraction_budget_exceeded", current)
                raise ExtractionRejected("Extraction budget exceeded") from None
            except Exception:
                self._reject_locked(table, row_number, "serialization_error", current)
                raise ExtractionRejected("Row serialization rejected") from None
            if (current["rows"] + 1 > self.limits["rows_per_table"] or
                    current["bytes"] + payload_size > self.limits["bytes_per_table"] or
                    self.rows_total + 1 > self.limits["rows_total"] or
                    self.bytes_total + payload_size > self.limits["bytes_total"]):
                self._reject_locked(table, row_number, "extraction_budget_exceeded", current)
                raise ExtractionRejected("Extraction budget exceeded")
            current["rows"] += 1
            current["bytes"] += payload_size
            self.rows_total += 1
            self.bytes_total += payload_size
        return row

    def reject(self, table: str, ordinal: int | None, reason: str):
        with self.lock:
            counts = self.tables.get(table, {"rows": 0, "bytes": 0})
            if self.extraction_status.get(table, "not_started") != "complete":
                self.extraction_status[table] = "incomplete"
            self._reject_locked(table, ordinal, reason, counts)

    def _reject_locked(self, table, ordinal, reason, counts):
        self.reject_summary = {
            "table": table, "row_ordinal": ordinal, "reason_code": reason,
            "accepted_table_rows": counts["rows"], "accepted_table_bytes": counts["bytes"],
            "accepted_rows_total": self.rows_total, "accepted_bytes_total": self.bytes_total,
        }


def _normalize_snowflake_account(value: str) -> str:
    normalized = value.strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
    normalized = normalized.removesuffix(".snowflakecomputing.com")
    return normalized


def ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Migration state and manifest directories must not be symlinks.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(0o700)


def _has_symlink_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _pipeline_name(args, dataset: str, target_hint: str) -> str:
    if not _SAFE_IDENTIFIER.fullmatch(args.pipeline_name):
        raise ValueError("Pipeline name must contain letters, numbers, and underscores only.")
    target_hash = sha256(target_hint.casefold().encode("utf-8")).hexdigest()[:24]
    name = f"{args.pipeline_name}_{args.destination}_{dataset}_{target_hash}".lower()
    if len(name) > 64:
        compact_hash = sha256(name.encode("utf-8")).hexdigest()[:24]
        name = f"{args.pipeline_name}_{args.destination}_c{compact_hash}".lower()
        if len(name) > 64:
            raise PipelineNameTooLongError(
                "Generated dlt pipeline/schema name exceeds dlt's 64-character limit even after "
                "deterministic compaction; shorten --pipeline-name."
            )
    if not _SAFE_IDENTIFIER.fullmatch(name):
        raise ValueError("Combined pipeline and target name is invalid.")
    return name


def _dlt_destination(destination: str):
    """Pin DuckDB to dlt's pipeline-local file while keeping other destinations configurable."""
    if destination == "duckdb":
        return dlt.destinations.duckdb(credentials=":pipeline:")
    return destination


def _resolve_dlt_destination(
    pipeline, destination: str, target_id: str | None, dataset: str, *, dry_run: bool = False
):
    """Inspect the typed configuration dlt will use; never print config or credentials."""
    expected_type = f"dlt.destinations.{destination}"
    if pipeline.destination is None or pipeline.destination.destination_type != expected_type:
        raise ValueError("Effective dlt destination type does not match the requested destination.")
    if pipeline.staging is not None:
        raise ValueError("dlt staging destinations are unsupported by this replacement loader.")
    if not pipeline.config.use_single_dataset:
        raise ValueError("dlt multi-dataset mode is unsupported because it can redirect table namespaces.")
    if pipeline.dataset_name != dataset or pipeline.config.dataset_name_layout:
        raise ValueError("Effective dlt dataset layout does not match the confirmed Bronze namespace.")

    # dlt's public client factory resolves all providers, including project and user .dlt TOML,
    # environment values, named destination config, and typed credential objects. Constructing
    # the client does not connect; the readiness probe below is the first destination I/O.
    client = pipeline.destination_client()
    config = client.config
    replace_strategy = getattr(config, "replace_strategy", None)
    if replace_strategy not in {None, "truncate-and-insert"}:
        raise ValueError("Effective dlt replace_strategy is unsupported; only truncate-and-insert replacement is allowed.")
    if getattr(config, "staging_dataset_name_layout", "%s_staging") != "%s_staging":
        raise ValueError("Custom dlt staging namespace layouts are unsupported.")
    physical_namespace = config.normalize_dataset_name(Schema(pipeline.pipeline_name))
    if not physical_namespace or physical_namespace.casefold() != dataset.casefold():
        raise ValueError("Resolved physical destination namespace differs from --dataset.")

    if destination == "bigquery":
        actual_project = getattr(config, "project_id", None)
        if not target_id or actual_project != target_id:
            raise ValueError("Effective dlt BigQuery project_id is absent or does not match --target-id.")
        dbt_project = os.environ.get("BQ_PROJECT")
        if dbt_project != target_id:
            raise ValueError("BQ_PROJECT must match the effective BigQuery project and --target-id.")
        identity = f"{actual_project}/{physical_namespace}"
    elif destination == "snowflake":
        credentials = getattr(config, "credentials", None)
        actual_host = getattr(credentials, "host", None) or getattr(credentials, "account", None)
        actual_database = getattr(credentials, "database", None)
        credential_query = getattr(credentials, "query", None) or {}
        allowed_query_keys = {
            "warehouse", "role", "authenticator", "token", "workload_identity_provider",
            "private_key", "private_key_path", "private_key_passphrase",
        }
        if (getattr(credentials, "_snowflake_host", None) or
                not isinstance(credential_query, dict) or
                not set(credential_query).issubset(allowed_query_keys)):
            raise ValueError("Custom Snowflake endpoint overrides are unsupported.")
        parts = target_id.split("/") if target_id else []
        if len(parts) != 2 or not actual_host or not actual_database:
            raise ValueError("Effective Snowflake account and database must resolve from dlt configuration.")
        if _normalize_snowflake_account(actual_host) != _normalize_snowflake_account(parts[0]):
            raise ValueError("Effective dlt Snowflake account does not match --target-id.")
        if actual_database.casefold() != parts[1].casefold():
            raise ValueError("Effective dlt Snowflake database does not match --target-id.")
        if _normalize_snowflake_account(os.environ.get("SNOWFLAKE_ACCOUNT", "")) != _normalize_snowflake_account(parts[0]):
            raise ValueError("SNOWFLAKE_ACCOUNT must match the effective Snowflake account.")
        if os.environ.get("SNOWFLAKE_DATABASE", "").casefold() != parts[1].casefold():
            raise ValueError("SNOWFLAKE_DATABASE must match the effective Snowflake database.")
        identity = f"{parts[0]}/{parts[1]}/{physical_namespace}"
    else:
        credentials = getattr(config, "credentials", None)
        database = getattr(credentials, "database", None)
        if not database or database in {":external:", ":memory:"}:
            raise ValueError("DuckDB must resolve to a local database file.")
        if any((getattr(credentials, key, None) for key in ("statements", "extensions", "global_config", "local_config", "pragmas"))):
            raise ValueError("Custom DuckDB statements, extensions, and connection settings are unsupported.")
        db_path = Path(database)
        if not db_path.is_absolute():
            db_path = Path(config.local_dir) / db_path
        db_path = Path(os.path.abspath(db_path))
        working_dir = Path(pipeline.working_dir).resolve()
        expected_filename = f"{pipeline.pipeline_name}.duckdb"
        if db_path.parent != working_dir or db_path.name != expected_filename:
            raise ValueError("DuckDB must use dlt's default <pipeline_name>.duckdb file in this pipeline directory.")
        if dry_run:
            db_path = MIGRATION_ROOT / ".dlt" / "pipelines" / pipeline.pipeline_name / expected_filename
        elif _has_symlink_component(db_path):
            raise ValueError("Resolved DuckDB file path must not contain symlinks.")
        db_path = Path(os.path.abspath(db_path))
        required_root = (MIGRATION_ROOT / ".dlt" / "pipelines").resolve()
        if not db_path.is_relative_to(required_root):
            raise ValueError("Resolved DuckDB file must be inside migration/.dlt/pipelines.")
        identity = f"duckdb/{db_path}/{physical_namespace}"

    return client, config, identity, physical_namespace


def write_private_manifest(path: Path, record: dict[str, object], *, allow_update: bool = False) -> None:
    """Compatibility wrapper for secure manifest creation and atomic updates."""
    writer = update_private_json if allow_update else create_private_json
    writer(path.parent, path.name, record)


def record_load_evidence(record: dict[str, object], load_info) -> None:
    """Store only package identifiers and aggregate job counts, never job errors or payloads."""
    if load_info is None:
        return
    try:
        raw_load_ids = _load_package_ids(load_info)
        load_ids = [value for value in raw_load_ids if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value)]
        packages = list(getattr(load_info, "load_packages", []) or [])
        record["load_ids"] = load_ids
        record["load_package_count"] = len(packages)
        statuses = ("new_jobs", "started_jobs", "completed_jobs", "failed_jobs")
        record["load_job_counts"] = {
            status: sum(len(package.jobs.get(status, [])) for package in packages)
            for status in statuses
        }
    except Exception:
        record["load_evidence_incomplete"] = True


def _step_evidence(step_info) -> dict[str, object] | None:
    if step_info is None:
        return None
    try:
        raw_ids = _load_package_ids(step_info)
        load_ids = [value for value in raw_ids if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value)]
        packages = list(getattr(step_info, "load_packages", []) or [])
        statuses = ("new_jobs", "started_jobs", "completed_jobs", "failed_jobs")
        job_counts = {
            status: sum(len(package.jobs.get(status, [])) for package in packages)
            for status in statuses
        }
        return {
            "load_ids": load_ids,
            "load_package_count": len(packages),
            "load_job_counts": job_counts,
        }
    except Exception:
        return {"evidence_incomplete": True}


def _load_package_ids(info) -> list[str]:
    """Return dlt package IDs, preferring its public plural field with legacy fallback."""
    values = getattr(info, "loads_ids", None)
    if values is None:
        values = getattr(info, "load_ids", [])
    return list(values or [])


def record_dlt_failure_evidence(record: dict[str, object], error: BaseException, pipeline) -> None:
    """Persist only safe dlt step/package evidence, never exception text or traces."""
    evidence: dict[str, object] = {}
    step = getattr(error, "step", None)
    if isinstance(step, str) and step in {"extract", "normalize", "load", "sync", "run"}:
        evidence["failed_step"] = step
    load_id = getattr(error, "load_id", None)
    if isinstance(load_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", load_id):
        evidence["failed_load_id"] = load_id
    for key in ("has_pending_data", "is_package_partially_loaded"):
        value = getattr(error, key, None)
        if isinstance(value, bool):
            evidence[key] = value

    info = getattr(error, "step_info", None)
    summary = _step_evidence(info)
    if summary:
        evidence["exception_step_info"] = summary
    try:
        trace = pipeline.last_trace
        if trace is not None:
            trace_evidence = {}
            for stage, attr in (
                ("extract", "last_extract_info"),
                ("normalize", "last_normalize_info"),
                ("load", "last_load_info"),
            ):
                step_summary = _step_evidence(getattr(trace, attr, None))
                if step_summary:
                    trace_evidence[stage] = step_summary
            if trace_evidence:
                evidence["trace_step_evidence"] = trace_evidence
    except Exception:
        evidence["trace_evidence_incomplete"] = True
    if evidence:
        record["dlt_failure_evidence"] = evidence


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    manifest_path: Path | None = None
    record: dict[str, object] | None = None
    load_info = None
    pipeline = None
    budget = None
    source_engine = None
    try:
        caller_cwd = Path.cwd()
        tables = parse_tables(args.tables)
        allowed_empty = parse_tables(args.allow_empty_tables) or []
        allowed_empty_names = {name.casefold() for name in allowed_empty}
        mappings = load_mapping_file(args.mapping_file)
        mapping_fingerprint = mappings_fingerprint(mappings)
        if args.check_retail_contract and not args.dry_run:
            _raise_safe_cli("unsupported_retail_contract_option")
        if args.profile_retail_values and not args.dry_run:
            _raise_safe_cli("unsupported_profile_option")
        if args.source == "csv" and args.csv_dir is None:
            _raise_safe_cli("csv_source_directory_required")
        csv_dir = (
            (caller_cwd / args.csv_dir).expanduser().resolve()
            if args.csv_dir is not None else None
        )
        if args.source == "csv" and not csv_dir.is_dir():
            _raise_safe_cli("csv_source_directory_required")
        if args.source == "sql" and (tables is None or not args.source_schema):
            _raise_safe_cli("sql_source_selection_required")

        dataset = args.dataset or ("starterkit_migration_bronze" if args.destination == "duckdb" else None)
        if not dataset:
            raise ValueError("Cloud destinations require an explicit --dataset isolated Bronze namespace.")
        if not _SAFE_IDENTIFIER.fullmatch(dataset) or not dataset.lower().startswith("starterkit_migration_"):
            raise ValueError("Dataset/schema names must use the isolated starterkit_migration_ prefix.")
        if args.dry_run:
            if args.source == "csv":
                inventory = inventory_csv_inputs(csv_dir.expanduser().resolve(), args.csv_glob, tables)
            else:
                connection_url = os.environ.get(args.source_url_env)
                if not connection_url:
                    _raise_safe_cli("sql_source_url_unset")
                source_engine = create_read_only_sql_engine(connection_url)
                try:
                    inventory = inventory_sql_inputs(source_engine, args.source_schema, tables)
                finally:
                    source_engine.dispose()
                    source_engine = None
            mapping_report = validate_mappings(inventory, mappings)
            active_mappings = mappings if mapping_report["valid"] else {}
            mapped_inventory = inventory_with_mapping_columns(inventory, active_mappings)
            compatibility_report = None
            value_profile = None
            if args.profile_retail_values:
                if not mapping_report["valid"]:
                    raise ValueError("Value profiling requires a valid mapping file.")
            selected_entities = [
                str(item["table"]) for item in inventory
                if item.get("status") in {"ready", "empty"} and item.get("table") is not None
            ]
            if len({name.casefold() for name in selected_entities}) != len(selected_entities):
                raise ValueError("Resolved source tables are not unique ignoring case.")
            for item in inventory:
                if item.get("status") == "empty":
                    item["empty_replacement_authorized"] = str(item["table"]).casefold() in allowed_empty_names
            selected_keys = {name.casefold() for name in selected_entities}
            blockers: list[str] = []
            if any(item.get("status") in {"missing", "no_matching_files"} for item in inventory):
                blockers.append("missing_inputs")
            if any(item.get("status") == "blocked_reserved_metadata_columns" for item in inventory):
                blockers.append("reserved_metadata_columns")
            if any(
                item.get("status") == "empty"
                and str(item.get("table", "")).casefold() not in allowed_empty_names
                for item in inventory
            ):
                blockers.append("empty_inputs_not_authorized")
            if not allowed_empty_names.issubset(selected_keys):
                blockers.append("allow_empty_names_not_selected")
            if not mapping_report["valid"]:
                blockers.append("source_mapping_invalid")

            # Resolve dlt's effective configuration in a disposable state directory. A dry run
            # never opens a destination connection and maps dlt's :pipeline: DuckDB location to
            # the corresponding persistent path used by an actual run.
            target_identity = None
            target_verified = False
            destination_tables = list(selected_entities)
            column_safety_report = None
            dry_client = None
            hint = f"{args.target_id or 'duckdb'}/{dataset}"
            pipeline_name = _pipeline_name(args, dataset, hint)
            try:
                with tempfile.TemporaryDirectory(prefix="dlt-migration-dryrun-") as temp_dir:
                    dry_pipeline = dlt.pipeline(
                        pipeline_name=pipeline_name,
                        pipelines_dir=temp_dir,
                        destination=_dlt_destination(args.destination),
                        dataset_name=dataset,
                        dev_mode=False,
                    )
                    dry_client = dry_pipeline.destination_client()
                    column_safety_report = analyze_normalized_columns(mapped_inventory, dry_client)
                    destination_tables = [
                        dry_client.capabilities.casefold_identifier(
                            dry_client.schema.naming.normalize_table_identifier(name)
                        ) for name in selected_entities
                    ]
                    if len({name.casefold() for name in destination_tables}) != len(destination_tables):
                        blockers.append("normalized_destination_table_name_collision")
                    if args.destination == "duckdb" or (args.target_id and _SAFE_TARGET_ID.fullmatch(args.target_id)):
                        _, _, target_identity, _ = _resolve_dlt_destination(
                            dry_pipeline, args.destination, args.target_id, dataset, dry_run=True
                        )
                        target_verified = args.confirm_target == target_identity
            except Exception:
                target_verified = False
            if dry_client is not None:
                compatibility_report = (
                    retail_contract_report(mapped_inventory, args.source, dry_client)
                    if args.check_retail_contract else None
                )
                if args.profile_retail_values:
                    connection_url = os.environ.get(args.source_url_env) if args.source == "sql" else None
                    value_profile = profile_retail_values(
                        args.source, inventory, csv_dir, args.csv_glob, connection_url,
                        args.source_schema, mappings, args.profile_max_rows_per_table,
                        args.profile_max_bytes_per_table, dry_client,
                    )
            elif args.check_retail_contract:
                blockers.append("retail_contract_normalizer_unavailable")
            elif args.profile_retail_values:
                blockers.append("retail_profile_normalizer_unavailable")
            if not target_verified:
                blockers.append("target_not_verified")
            if column_safety_report is None:
                blockers.append("column_safety_unverified")
            elif not column_safety_report["safe_to_extract"]:
                blockers.append("normalized_column_collision_or_reserved_name")
            namespace_verified = False
            if args.destination in {"bigquery", "snowflake"}:
                blockers.append("namespace_not_verified_by_config_resolving_dry_run")
            exact_table_list = ",".join(destination_tables)
            table_confirmation_verified = args.confirm_tables == exact_table_list
            if not table_confirmation_verified:
                blockers.append("table_list_not_confirmed")
            current_schema_confirmation = schema_confirmation_fingerprint(
                schema_fingerprint(inventory), typed_schema_fingerprint(inventory), mapping_fingerprint
            )
            schema_drift_review = None
            if args.schema_baseline is not None:
                try:
                    reviewed_schema = load_reviewed_schema((caller_cwd / args.schema_baseline).expanduser())
                except SchemaBaselineError:
                    _raise_safe_cli("schema_baseline_unusable")
                schema_drift_review = drift_review(
                    reviewed_schema, schema_snapshot(inventory), mapping_fingerprint,
                    current_schema_confirmation, args.confirm_schema_fingerprint,
                )
                if not schema_drift_review["fresh_confirmation_verified"]:
                    blockers.append("schema_drift_not_confirmed")
            dry_run_report = {
                "mode": "dry_run",
                "source": args.source,
                "destination": args.destination,
                "source_schema_fingerprint": schema_fingerprint(inventory),
                "source_schema_fingerprint_algorithm": "sha256-table-and-ordered-column-names-v1",
                "typed_schema_fingerprint": typed_schema_fingerprint(inventory),
                "typed_schema_fingerprint_algorithm": "sha256-table-column-type-families-v1",
                "source_schema_snapshot": schema_snapshot(inventory),
                "source_schema_snapshot_algorithm": _SCHEMA_SNAPSHOT_ALGORITHM,
                "schema_drift_review": schema_drift_review,
                "schema_confirmation_fingerprint": current_schema_confirmation,
                "schema_confirmation_algorithm": _SCHEMA_CONFIRMATION_ALGORITHM,
                "source_mapping_fingerprint": mapping_fingerprint,
                "source_mapping_report": mapping_report,
                "destination_type_profile": resolve_type_profile(args.destination).describe(),
                "bronze_namespace": dataset,
                "inventory": inventory,
                "normalized_identifier_safety": column_safety_report,
                "retail_contract_compatibility": compatibility_report,
                "retail_value_profile": value_profile,
                "resolved_tables": selected_entities,
                "destination_tables": destination_tables,
                "table_confirmation": exact_table_list,
                "target_identity": target_identity,
                "target_verified": target_verified,
                "namespace_verified": namespace_verified,
                "table_confirmation_verified": table_confirmation_verified,
                "preflight_ready": not blockers,
                "ready_to_load": False,
                "extraction_limits_enforced": False,
                "readiness_note": "Inventory, identifiers, and local target configuration are checked here; extraction limits are enforced only while rows are read during a real load.",
                "blockers": blockers,
                "extraction_limits": {
                    "rows_per_table": args.max_rows_per_table,
                    "bytes_per_row": args.max_bytes_per_row,
                    "bytes_per_table": args.max_bytes_per_table,
                    "rows_total": args.max_rows_total,
                    "bytes_total": args.max_bytes_total,
                },
                "writes_performed": False,
            }
            # Operator assertion only; omitted when not supplied so legacy behavior is unchanged.
            if args.source_binding_id is not None:
                dry_run_report["source_binding_id"] = args.source_binding_id
            print(json.dumps(dry_run_report, indent=2))
            return 0
        if args.destination != "duckdb" and (not args.target_id or not _SAFE_TARGET_ID.fullmatch(args.target_id)):
            raise ValueError("Cloud migrations require a non-secret --target-id (project or account/database).")
        proposed_identity = f"{args.target_id or 'duckdb'}/{dataset}"
        pipeline_name = _pipeline_name(args, dataset, proposed_identity)
        # Keep caller-relative source paths stable; dlt resolves project TOML via the
        # pre-import DLT_PROJECT_DIR environment rather than process CWD.
        if os.name == "posix":
            os.umask(0o077)
        ensure_private_directory(MIGRATION_ROOT / ".dlt")
        pipelines_root = MIGRATION_ROOT / ".dlt" / "pipelines"
        if _has_symlink_component(pipelines_root):
            raise ValueError("Migration pipeline state paths must not contain symlinks.")
        ensure_private_directory(pipelines_root)
        if _has_symlink_component(pipelines_root / pipeline_name):
            raise ValueError("Existing dlt pipeline work directory must not contain symlinks.")
        pipeline = dlt.pipeline(
            pipeline_name=pipeline_name,
            pipelines_dir=str(pipelines_root),
            destination=_dlt_destination(args.destination),
            dataset_name=dataset,
            dev_mode=False,
        )
        if pipeline.has_pending_data:
            _raise_safe_cli("pending_packages")
        client, _, target_identity, physical_namespace = _resolve_dlt_destination(
            pipeline, args.destination, args.target_id, dataset
        )
        if args.confirm_target != target_identity:
            _raise_safe_cli("target_confirmation_mismatch")

        run_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc).isoformat()
        budget = ExtractionBudget(args)
        # Resolve before any source rows are read; unknown destinations fail closed.
        type_profile = resolve_type_profile(args.destination)
        observed_schema_by_table: dict[str, dict[str, object]] = {}
        extraction_schema_inventory: list[dict[str, object]] | None = None
        if args.source == "csv":
            source_inventory = inventory_csv_inputs(csv_dir.expanduser().resolve(), args.csv_glob, tables, count_rows=False)
            if any(item.get("status") in {"missing", "no_matching_files"} for item in source_inventory):
                _raise_safe_cli("source_inputs_missing")
            expected_schema_fingerprint = schema_fingerprint(source_inventory)
            expected_typed_schema_fingerprint = typed_schema_fingerprint(source_inventory)
            expected_schema_confirmation_fingerprint = schema_confirmation_fingerprint(
                expected_schema_fingerprint, expected_typed_schema_fingerprint, mapping_fingerprint
            )
            schema_drift_review = enforce_schema_baseline(
                args.schema_baseline, caller_cwd, source_inventory, mapping_fingerprint,
                expected_schema_confirmation_fingerprint, args.confirm_schema_fingerprint,
            )
            require_schema_confirmation(args.confirm_schema_fingerprint, expected_schema_confirmation_fingerprint)
            expected_schema_by_table = {
                str(item["table"]).casefold(): item
                for item in source_inventory
                if isinstance(item.get("table"), str)
            }
            mapping_report = validate_mappings(source_inventory, mappings)
            if not mapping_report["valid"]:
                raise ValueError("Source mapping does not match the selected source schema.")
            column_safety_report = analyze_normalized_columns(inventory_with_mapping_columns(source_inventory, mappings), client)
            if not column_safety_report["safe_to_extract"]:
                raise ValueError("Source column names collide after dlt normalization or use reserved/generated names.")
            data, entities, csv_actual_counts = make_csv_resources(
                csv_dir, args.csv_glob, tables, run_id, started_at, allowed_empty_names, budget, client,
                expected_schema_by_table, observed_schema_by_table, mappings,
                type_profile=type_profile,
            )
        else:
            connection_url = os.environ.get(args.source_url_env)
            if not connection_url:
                _raise_safe_cli("sql_source_url_unset")
            source_engine = create_read_only_sql_engine(connection_url)
            source_inventory = inventory_sql_inputs(source_engine, args.source_schema, tables)
            if any(item.get("status") == "missing" for item in source_inventory):
                _raise_safe_cli("source_inputs_missing")
            expected_schema_fingerprint = schema_fingerprint(source_inventory)
            expected_typed_schema_fingerprint = typed_schema_fingerprint(source_inventory)
            expected_schema_confirmation_fingerprint = schema_confirmation_fingerprint(
                expected_schema_fingerprint, expected_typed_schema_fingerprint, mapping_fingerprint
            )
            schema_drift_review = enforce_schema_baseline(
                args.schema_baseline, caller_cwd, source_inventory, mapping_fingerprint,
                expected_schema_confirmation_fingerprint, args.confirm_schema_fingerprint,
            )
            require_schema_confirmation(args.confirm_schema_fingerprint, expected_schema_confirmation_fingerprint)
            mapping_report = validate_mappings(source_inventory, mappings)
            if not mapping_report["valid"]:
                raise ValueError("Source mapping does not match the selected source schema.")
            column_safety_report = analyze_normalized_columns(inventory_with_mapping_columns(source_inventory, mappings), client)
            if not column_safety_report["safe_to_extract"]:
                raise ValueError("Source column names collide after dlt normalization or use reserved/generated names.")
            preflight_sql_rows(source_engine, args.source_schema, tables, allowed_empty_names)
            data, entities, extraction_schema_inventory = make_sql_source(
                source_engine, args.source_schema, tables, run_id, started_at, budget, client,
                expected_schema_fingerprint, expected_typed_schema_fingerprint, mappings,
                type_profile=type_profile,
            )
            csv_actual_counts = {}

        entity_keys = {name.casefold() for name in entities}
        if not allowed_empty_names.issubset(entity_keys):
            raise ValueError("--allow-empty-tables may name only tables selected for this run.")
        budget.register_tables(entities)
        casefold_identifier = client.capabilities.casefold_identifier
        destination_tables = [
            casefold_identifier(client.schema.naming.normalize_table_identifier(name)) for name in entities
        ]
        if len({name.casefold() for name in destination_tables}) != len(destination_tables):
            raise ValueError("Selected source names collide after dlt destination normalization.")
        exact_table_list = ",".join(destination_tables)
        print(f"Resolved destination tables: {exact_table_list}")
        if args.confirm_tables != exact_table_list:
            _raise_safe_cli("table_confirmation_mismatch")
        if allowed_empty:
            print(f"Empty-table replacement explicitly authorized for: {','.join(allowed_empty)}")

        if not _SAFE_IDENTIFIER.fullmatch(args.pipeline_name):
            raise ValueError("Pipeline name must contain letters, numbers, and underscores only.")

        if args.destination != "duckdb":
            with client:
                if not client.is_storage_initialized():
                    _raise_safe_cli("namespace_missing")

        record = {
            "run_id": run_id,
            "pipeline_name": pipeline_name,
            "source_kind": args.source,
            "entities": entities,
            "source_identity": source_identity_evidence(
                args.source, entities, csv_dir, args.csv_glob, args.source_schema
            ),
            "destination_tables": destination_tables,
            "allow_empty_tables": sorted(allowed_empty_names),
            "destination": args.destination,
            "target_identity": target_identity,
            "bronze_namespace": dataset,
            "write_disposition": "replace",
            "source_row_counts": budget.manifest_counts(entities)[0],
            "accepted_extraction_progress": budget.manifest_counts(entities)[1],
            "started_at_utc": started_at,
            "status": "running",
            "destination_write_may_be_partial": False,
            "extraction_limits": budget.limits,
            "source_schema_fingerprint": expected_schema_fingerprint,
            "source_schema_fingerprint_algorithm": "sha256-table-and-ordered-column-names-v1",
            "source_schema_fingerprint_status": "preflight_only",
            "typed_schema_fingerprint": expected_typed_schema_fingerprint,
            "typed_schema_fingerprint_algorithm": "sha256-table-column-type-families-v1",
            "schema_confirmation_fingerprint": expected_schema_confirmation_fingerprint,
            "schema_confirmation_algorithm": _SCHEMA_CONFIRMATION_ALGORITHM,
            "typed_schema_fingerprint_status": "preflight_only",
            "source_mapping_fingerprint": mapping_fingerprint,
            "source_mapping_rule_count": sum(len(rules) for rules in mappings.values()),
            "destination_type_profile": type_profile.describe(),
            # Names and type families only, so later runs can diff against this manifest.
            "source_schema_snapshot": schema_snapshot(source_inventory),
            "source_schema_snapshot_algorithm": _SCHEMA_SNAPSHOT_ALGORITHM,
        }
        if schema_drift_review is not None:
            record["schema_drift_review"] = schema_drift_review
        # Operator assertion only; omitted when not supplied so legacy behavior is unchanged.
        if args.source_binding_id is not None:
            record["source_binding_id"] = args.source_binding_id
        manifest_dir = MIGRATION_ROOT / "run-manifests"
        ensure_private_directory(manifest_dir)
        manifest_path = manifest_dir / f"{run_id}.json"
        write_private_manifest(manifest_path, record)
        if pipeline.has_pending_data:
            raise ValueError("This dlt pipeline gained pending packages before load; refusing to process them.")
        _, _, current_identity, current_namespace = _resolve_dlt_destination(
            pipeline, args.destination, args.target_id, dataset
        )
        if current_identity != target_identity or current_namespace.casefold() != physical_namespace.casefold():
            raise ValueError("Effective destination identity changed after confirmation; refusing to load.")
        # Extraction and normalization are staged separately. Any malformed row or budget
        # rejection therefore stops before the destination load step begins.
        pipeline.extract(data, workers=1)
        # Successful exhaustion proves every explicitly selected resource completed,
        # including an authorized empty table that yielded no rows.
        for entity in entities:
            budget.complete_table(entity)
        if source_engine is not None:
            source_engine.dispose()
            source_engine = None
        if args.source == "csv":
            extraction_schema_inventory = [
                observed_schema_by_table[name.casefold()]
                for name in entities
                if name.casefold() in observed_schema_by_table
            ]
        observed_fingerprint = schema_fingerprint(extraction_schema_inventory or [])
        record["source_schema_fingerprint_observed"] = observed_fingerprint
        observed_typed_fingerprint = typed_schema_fingerprint(extraction_schema_inventory or [])
        record["typed_schema_fingerprint_observed"] = observed_typed_fingerprint
        if observed_fingerprint != expected_schema_fingerprint:
            record["source_schema_fingerprint_status"] = "mismatch"
            raise ValueError("Source schema fingerprint changed during extraction; refusing to load.")
        record["source_schema_fingerprint_status"] = "matched_at_extraction"
        if observed_typed_fingerprint != expected_typed_schema_fingerprint:
            record["typed_schema_fingerprint_status"] = "mismatch"
            raise ValueError("Source type families changed during extraction; refusing to load.")
        record["typed_schema_fingerprint_status"] = "matched_at_extraction"
        write_private_manifest(manifest_path, record, allow_update=True)
        for entity in entities:
            if budget.tables.get(entity, {}).get("rows", 0) == 0 and entity.casefold() not in allowed_empty_names:
                budget.reject(entity, 0, "empty_source_not_authorized")
                raise ExtractionRejected("Empty source table is not authorized")
        pipeline.normalize(workers=1)
        trace = pipeline.last_trace
        normalize_info = trace.last_normalize_info if trace is not None else None
        normalized_counts = getattr(normalize_info, "row_counts", {}) if normalize_info is not None else {}
        if not isinstance(normalized_counts, dict):
            normalized_counts = {}
        normalized_by_fold = {str(name).casefold(): count for name, count in normalized_counts.items()}
        record["dlt_prepared_row_counts"] = {
            table: {"rows": normalized_by_fold.get(table.casefold()), "provenance": "dlt_1_32_0_last_normalize_info_row_counts"}
            for table in destination_tables
        }
        record["source_row_counts"], record["accepted_extraction_progress"] = budget.manifest_counts(entities)
        if args.destination in {"bigquery", "snowflake"}:
            # Repeat the read-only metadata readiness probe after all source work and
            # immediately before load. If it errors or returns false, fail closed here.
            try:
                with client:
                    namespace_exists = client.is_storage_initialized()
            except Exception:
                _raise_safe_cli("namespace_probe_failed")
            if not namespace_exists:
                _raise_safe_cli("namespace_missing")
        # This flag is set before the first destination load operation: a failed replacement
        # may have committed some jobs and is never treated as rollback.
        record["destination_write_may_be_partial"] = True
        write_private_manifest(manifest_path, record, allow_update=True)
        load_info = pipeline.load(raise_on_failed_jobs=True)
        record_load_evidence(record, load_info)
        load_info.raise_on_failed_jobs()
        load_ids = _load_package_ids(load_info)
        if pipeline.has_pending_data:
            _raise_safe_cli("pending_packages")
        if not load_ids or not load_info.load_packages:
            _raise_safe_cli("load_evidence_incomplete")
        if any(package.jobs.get("failed_jobs") for package in load_info.load_packages):
            _raise_safe_cli("load_jobs_failed")
        record["destination_write_may_be_partial"] = False
        finished_at = datetime.now(timezone.utc).isoformat()
        record["finished_at_utc"] = finished_at
        record["status"] = "completed"
        write_private_manifest(manifest_path, record, allow_update=True)
        print(f"Migration load completed for {len(entities)} selected entities.")
        print(f"Destination: {args.destination}; Bronze namespace: {physical_namespace}.")
        print(f"Run manifest: {manifest_path.relative_to(MIGRATION_ROOT)}")
        print("Review the Bronze reconciliation report before dbt transformations.")
        del load_info
        if source_engine is not None:
            source_engine.dispose()
            source_engine = None
        return 0
    except Exception as error:  # Keep exception strings suppressed because drivers may embed connection details.
        if source_engine is not None:
            try:
                source_engine.dispose()
            except Exception:
                pass
        if manifest_path is not None and record is not None:
            record_load_evidence(record, load_info)
            if pipeline is not None:
                record_dlt_failure_evidence(record, error, pipeline)
            if budget is not None:
                record["source_row_counts"], record["accepted_extraction_progress"] = budget.manifest_counts(
                    record.get("entities", [])
                )
            if budget is not None and budget.reject_summary is not None:
                record["extraction_reject_summary"] = budget.reject_summary
            record["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
            record["status"] = "failed"
            if record.get("destination_write_may_be_partial") is not True:
                record["destination_write_may_be_partial"] = False
            try:
                write_private_manifest(manifest_path, record, allow_update=True)
            except Exception:
                pass
        if isinstance(error, SafeCliError):
            print(f"Migration stopped: {_SAFE_CLI_MESSAGES[error.code]}", file=sys.stderr)
        elif isinstance(error, (MappingConfigurationError, PipelineNameTooLongError)):
            print(f"Migration stopped: {error}", file=sys.stderr)
        else:
            print(f"Migration stopped ({type(error).__name__}). Review the command and local configuration privately.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
