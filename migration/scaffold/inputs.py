"""Input contract for the Silver scaffolder (docs/scaffolder-input-contract.md).

Three required artifacts are validated and bound before any plan is built:

- `mapping`: reviewed version 1 mapping file, parsed by the loader's own dependency-free
  parser (`mapping_contract.load_mapping_file`).
- `dry_run`: saved owner-only `--dry-run` JSON, read through `_private_json.read_private_json`.
- `run_manifest`: completed `<run_id>.json` run manifest, read through the same reader and
  validated by `evidence_index._validate_manifest`.

Checks run in contract order: admission limits, field tables, then binding steps 1-10. The
first failure raises `Refusal` with exactly one fixed code. Nothing here writes files, reads
environment variables, opens network connections, or imports dlt, dbt, or warehouse clients.
Fingerprints are integrity checks only (unkeyed SHA-256), never authentication.
"""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _private_json import read_private_json
from destination_type_profiles import PROFILE_ALGORITHM
from evidence_index import _validate_manifest
from mapping_contract import (
    MAPPING_INVALID_RULES,
    MAPPING_MALFORMED_JSON,
    MAPPING_UNSUPPORTED_VERSION,
    MAX_MAPPING_FILE_BYTES,
    SAFE_IDENTIFIER,
    MappingConfigurationError,
    load_mapping_file,
    mappings_fingerprint,
)
from schema_drift import (
    SCHEMA_CONFIRMATION_ALGORITHM,
    SNAPSHOT_ALGORITHM,
    SchemaBaselineError,
    _MAX_COLUMNS_PER_TABLE as MAX_SNAPSHOT_COLUMNS,
    _MAX_NAME_LENGTH as MAX_SNAPSHOT_NAME_LENGTH,
    _MAX_TABLES as MAX_SNAPSHOT_TABLES,
    reviewed_schema_from_record,
    validate_snapshot,
)

from . import reasons as R
from .plan import BronzeTable, ValidatedEvidence
from .reasons import Refusal


# Mirrors run_migration._SOURCE_BINDING_ID (run_migration imports dlt; a parity test pins it).
SOURCE_BINDING_ID = re.compile(r"[a-z][a-z0-9_]{0,39}")
NAME_FINGERPRINT_ALGORITHM = "sha256-table-and-ordered-column-names-v1"
TYPED_FINGERPRINT_ALGORITHM = "sha256-table-column-type-families-v1"
BRONZE_NAMESPACE_PREFIX = "starterkit_migration_"
DRY_RUN_SOURCES = ("csv", "sql")
DESTINATIONS = ("bigquery", "snowflake", "duckdb")
EDITION_BY_DESTINATION = {"bigquery": "bigquery", "snowflake": "snowflake"}
ALLOWED_BLOCKERS = frozenset({
    "target_not_verified",
    "table_list_not_confirmed",
    "namespace_not_verified_by_config_resolving_dry_run",
})
READY_STATUSES = frozenset({"ready", "empty"})
MAX_MANIFEST_ENTITIES = 1_000
_SHA256 = re.compile(r"[0-9a-f]{64}")

_PRIVATE_JSON_OVERSIZED = "Private JSON file exceeds the supported size limit."
_PRIVATE_JSON_MALFORMED = frozenset({
    "Private JSON object contains duplicate keys.",
    "Private JSON file must contain an object.",
})

_MANIFEST_REQUIRED = (
    "run_id", "status", "source_kind", "source_identity", "entities", "destination_tables",
    "destination", "bronze_namespace", "target_identity", "pipeline_name", "write_disposition",
    "started_at_utc", "finished_at_utc", "destination_write_may_be_partial",
    "source_schema_snapshot", "source_schema_snapshot_algorithm",
    "source_schema_fingerprint", "source_schema_fingerprint_algorithm",
    "typed_schema_fingerprint", "typed_schema_fingerprint_algorithm",
    "schema_confirmation_fingerprint", "schema_confirmation_algorithm",
    "source_schema_fingerprint_status", "typed_schema_fingerprint_status",
    "source_schema_fingerprint_observed", "typed_schema_fingerprint_observed",
    "source_mapping_fingerprint", "source_mapping_rule_count", "destination_type_profile",
)


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------

def _require(condition: bool, code: str) -> None:
    if not condition:
        raise Refusal(code)


def _field(record: dict, name: str, *, nullable: bool = False) -> object:
    if name not in record or (record[name] is None and not nullable):
        raise Refusal(R.FIELD_MISSING)
    return record[name]


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _malformed_unless(condition: bool) -> None:
    _require(condition, R.FIELD_MALFORMED)


def _algorithm(record: dict, name: str, expected: str) -> None:
    if _field(record, name) != expected:
        raise Refusal(R.INPUT_UNSUPPORTED_VERSION)


def _sha256(record: dict, name: str) -> str:
    value = _field(record, name)
    _malformed_unless(isinstance(value, str) and _SHA256.fullmatch(value) is not None)
    return value


def _utc_timestamp(value: object) -> datetime:
    _malformed_unless(isinstance(value, str) and 0 < len(value) <= 64)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise Refusal(R.FIELD_MALFORMED) from None
    _malformed_unless(parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0))
    return parsed


def _casefold_set(values) -> set[str]:
    return {value.casefold() for value in values}


# --------------------------------------------------------------------------------------------
# 1. Admission limits
# --------------------------------------------------------------------------------------------

def admit_mapping(raw_path: str | None) -> dict[str, list[dict[str, str]]]:
    if raw_path is None:
        raise Refusal(R.INPUT_MISSING)
    path = os.path.abspath(raw_path)
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        raise Refusal(R.INPUT_MISSING) from None
    except OSError:
        raise Refusal(R.INPUT_NOT_PRIVATE) from None
    if not stat.S_ISREG(info.st_mode):
        raise Refusal(R.INPUT_NOT_PRIVATE)
    if info.st_size > MAX_MAPPING_FILE_BYTES:
        raise Refusal(R.INPUT_OVERSIZED)
    try:
        mapping = load_mapping_file(Path(path))
    except MappingConfigurationError as error:
        code = {
            MAPPING_MALFORMED_JSON: R.INPUT_MALFORMED,
            MAPPING_UNSUPPORTED_VERSION: R.INPUT_UNSUPPORTED_VERSION,
            MAPPING_INVALID_RULES: R.MAPPING_INVALID,
        }.get(error.code, R.INPUT_NOT_PRIVATE)
        raise Refusal(code) from None
    if not mapping:
        raise Refusal(R.MAPPING_INVALID)
    return mapping


def admit_private_json(raw_path: str | None) -> tuple[dict, str]:
    """Read one owner-only JSON artifact; returns (record, file name)."""
    if raw_path is None:
        raise Refusal(R.INPUT_MISSING)
    path = Path(raw_path)
    try:
        record = read_private_json(path.parent, path.name)
    except (FileNotFoundError, NotADirectoryError):
        raise Refusal(R.INPUT_MISSING) from None
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise Refusal(R.INPUT_MALFORMED) from None
    except ValueError as error:
        message = str(error)
        if message == _PRIVATE_JSON_OVERSIZED:
            raise Refusal(R.INPUT_OVERSIZED) from None
        if message in _PRIVATE_JSON_MALFORMED:
            raise Refusal(R.INPUT_MALFORMED) from None
        raise Refusal(R.INPUT_NOT_PRIVATE) from None
    except OSError:
        raise Refusal(R.INPUT_NOT_PRIVATE) from None
    return record, path.name


def _check_bounds(record: dict) -> None:
    """Snapshot, entity, and name bounds (schema_drift._MAX_*, evidence_index entity limit)."""
    snapshot = record.get("source_schema_snapshot")
    if isinstance(snapshot, list):
        if len(snapshot) > MAX_SNAPSHOT_TABLES:
            raise Refusal(R.INPUT_OVERSIZED)
        for entry in snapshot:
            if not isinstance(entry, dict):
                continue
            table, columns = entry.get("table"), entry.get("columns")
            if isinstance(table, str) and len(table) > MAX_SNAPSHOT_NAME_LENGTH:
                raise Refusal(R.INPUT_OVERSIZED)
            if isinstance(columns, list):
                if len(columns) > MAX_SNAPSHOT_COLUMNS:
                    raise Refusal(R.INPUT_OVERSIZED)
                for column in columns:
                    if isinstance(column, dict) and any(
                        isinstance(value, str) and len(value) > MAX_SNAPSHOT_NAME_LENGTH
                        for value in column.values()
                    ):
                        raise Refusal(R.INPUT_OVERSIZED)
    for name in ("entities", "resolved_tables", "destination_tables", "inventory"):
        value = record.get(name)
        if isinstance(value, list) and len(value) > MAX_MANIFEST_ENTITIES:
            raise Refusal(R.INPUT_OVERSIZED)


# --------------------------------------------------------------------------------------------
# 2. Field tables
# --------------------------------------------------------------------------------------------

def _check_snapshot_fields(record: dict) -> None:
    _algorithm(record, "source_schema_snapshot_algorithm", SNAPSHOT_ALGORITHM)
    try:
        validate_snapshot(_field(record, "source_schema_snapshot"))
    except SchemaBaselineError:
        raise Refusal(R.FIELD_MALFORMED) from None
    _sha256(record, "source_schema_fingerprint")
    _algorithm(record, "source_schema_fingerprint_algorithm", NAME_FINGERPRINT_ALGORITHM)
    _sha256(record, "typed_schema_fingerprint")
    _algorithm(record, "typed_schema_fingerprint_algorithm", TYPED_FINGERPRINT_ALGORITHM)
    _sha256(record, "source_mapping_fingerprint")
    _sha256(record, "schema_confirmation_fingerprint")
    _algorithm(record, "schema_confirmation_algorithm", SCHEMA_CONFIRMATION_ALGORITHM)


def _check_profile(record: dict) -> None:
    profile = _field(record, "destination_type_profile")
    _malformed_unless(isinstance(profile, dict))
    _malformed_unless(_is_text(_field(profile, "destination")) and _is_text(_field(profile, "name")))
    _algorithm(profile, "algorithm", PROFILE_ALGORITHM)


def _check_binding_field(record: dict) -> None:
    if "source_binding_id" in record:
        value = record["source_binding_id"]
        _malformed_unless(isinstance(value, str) and SOURCE_BINDING_ID.fullmatch(value) is not None)


def check_dry_run_fields(record: dict) -> None:
    """Field table for `dry_run`, in contract table order."""
    if _field(record, "mode") != "dry_run":
        raise Refusal(R.INPUT_UNSUPPORTED_VERSION)
    _check_binding_field(record)
    _malformed_unless(_field(record, "source") in DRY_RUN_SOURCES)
    _malformed_unless(_field(record, "destination") in DESTINATIONS)
    namespace = _field(record, "bronze_namespace")
    _malformed_unless(
        isinstance(namespace, str) and SAFE_IDENTIFIER.fullmatch(namespace) is not None
        and namespace.startswith(BRONZE_NAMESPACE_PREFIX)
    )
    target_identity = _field(record, "target_identity", nullable=True)
    _malformed_unless(target_identity is None or _is_text(target_identity))

    resolved = _field(record, "resolved_tables")
    _malformed_unless(isinstance(resolved, list) and all(_is_text(name) for name in resolved))
    _malformed_unless(len(_casefold_set(resolved)) == len(resolved))
    destination_tables = _field(record, "destination_tables")
    _malformed_unless(
        isinstance(destination_tables, list)
        and all(_is_text(name) for name in destination_tables)
        and len(destination_tables) == len(resolved)
        and len(_casefold_set(destination_tables)) == len(destination_tables)
    )
    inventory = _field(record, "inventory")
    _malformed_unless(isinstance(inventory, list))
    for item in inventory:
        _malformed_unless(isinstance(item, dict))
        if "table" not in item or "status" not in item:
            raise Refusal(R.FIELD_MISSING)
        _malformed_unless((item["table"] is None or _is_text(item["table"])) and _is_text(item["status"]))

    _check_snapshot_fields(record)

    report = _field(record, "source_mapping_report")
    _malformed_unless(isinstance(report, dict))
    for name in ("valid", "errors", "mappings"):
        _field(report, name)
    _malformed_unless(
        isinstance(report["valid"], bool) and isinstance(report["errors"], list) and isinstance(report["mappings"], list)
    )
    if report["valid"] is not True or report["errors"]:
        raise Refusal(R.MAPPING_INVALID)
    for rule in report["mappings"]:
        _malformed_unless(
            isinstance(rule, dict) and set(rule) == {"table", "source", "target", "cast"}
            and all(isinstance(rule[key], str) for key in rule)
        )

    safety = _field(record, "normalized_identifier_safety", nullable=True)
    if safety is not None:
        _malformed_unless(isinstance(safety, dict))
        extract = _field(safety, "safe_to_extract", nullable=True)
        _malformed_unless(extract is None or isinstance(extract, bool))
        tables = _field(safety, "tables")
        _malformed_unless(isinstance(tables, list))
        for table in tables:
            _malformed_unless(isinstance(table, dict))
            for name in ("table", "normalized_destination_table", "normalized_columns"):
                if name not in table:
                    raise Refusal(R.FIELD_MISSING)
            _malformed_unless(
                (table["table"] is None or _is_text(table["table"]))
                and (table["normalized_destination_table"] is None or _is_text(table["normalized_destination_table"]))
                and isinstance(table["normalized_columns"], list)
                and len(table["normalized_columns"]) <= MAX_SNAPSHOT_COLUMNS * 2
            )
            for column in table["normalized_columns"]:
                _malformed_unless(
                    isinstance(column, dict) and _is_text(column.get("source")) and _is_text(column.get("destination"))
                    and len(column["source"]) <= MAX_SNAPSHOT_NAME_LENGTH
                    and len(column["destination"]) <= MAX_SNAPSHOT_NAME_LENGTH
                )
        names = [table["table"].casefold() for table in tables if table["table"] is not None]
        _malformed_unless(len(set(names)) == len(names))

    _check_profile(record)
    review = _field(record, "schema_drift_review", nullable=True)
    if review is not None:
        _malformed_unless(isinstance(review, dict))
        _malformed_unless(isinstance(_field(review, "fresh_confirmation_verified"), bool))
    blockers = _field(record, "blockers")
    _malformed_unless(isinstance(blockers, list) and all(_is_text(name) for name in blockers))
    _malformed_unless(_field(record, "writes_performed") is False)


def check_run_manifest_fields(record: dict, file_name: str) -> None:
    """Field table for `run_manifest`. Status and extraction-match failures map to stale codes."""
    for name in _MANIFEST_REQUIRED:
        _field(record, name)
    if record["status"] != "completed":
        raise Refusal(R.EVIDENCE_RUN_NOT_COMPLETED)
    if record["destination_write_may_be_partial"] is not False:
        raise Refusal(R.EVIDENCE_RUN_NOT_COMPLETED)
    if record.get("load_evidence_incomplete", False) is not False:
        raise Refusal(R.EVIDENCE_RUN_NOT_COMPLETED)
    _check_binding_field(record)
    _check_snapshot_fields(record)
    if (
        record["source_schema_fingerprint_status"] != "matched_at_extraction"
        or record["typed_schema_fingerprint_status"] != "matched_at_extraction"
        or record["source_schema_fingerprint_observed"] != record["source_schema_fingerprint"]
        or record["typed_schema_fingerprint_observed"] != record["typed_schema_fingerprint"]
    ):
        raise Refusal(R.EVIDENCE_SCHEMA_NOT_MATCHED)
    count = record["source_mapping_rule_count"]
    _malformed_unless(type(count) is int and count >= 0)
    _check_profile(record)
    _utc_timestamp(record["started_at_utc"])
    _utc_timestamp(record["finished_at_utc"])

    # Binding step 6 is checked before the shared manifest validator, which would otherwise
    # report an absent or unsafe SQL schema as a generic malformed field.
    identity = record["source_identity"]
    _malformed_unless(isinstance(identity, dict))
    if record["source_kind"] == "sql":
        schema = identity.get("schema")
        if not isinstance(schema, str) or SAFE_IDENTIFIER.fullmatch(schema) is None:
            raise Refusal(R.SOURCE_SCHEMA_UNAVAILABLE)
    elif "schema" in identity:
        raise Refusal(R.SOURCE_SCHEMA_UNAVAILABLE)

    stem, suffix = os.path.splitext(file_name)
    _malformed_unless(suffix == ".json")
    destination_tables = record["destination_tables"]
    entities = record["entities"]
    _malformed_unless(
        isinstance(destination_tables, list) and isinstance(entities, list)
        and len(destination_tables) == len(entities)
        and all(isinstance(name, str) and SAFE_IDENTIFIER.fullmatch(name) for name in destination_tables)
        and len(_casefold_set(destination_tables)) == len(destination_tables)
    )
    try:
        _validate_manifest(record, stem)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise Refusal(R.FIELD_MALFORMED) from None


# --------------------------------------------------------------------------------------------
# 3. Binding steps 1-10
# --------------------------------------------------------------------------------------------

def _rule_tuples(mapping: dict[str, list[dict[str, str]]]) -> set[tuple[str, str, str, str]]:
    return {
        (table, rule["source"], rule["target"], rule["cast"])
        for table, rules in mapping.items() for rule in rules
    }


def bind(
    *,
    edition: str,
    mapping: dict[str, list[dict[str, str]]],
    dry_run: dict,
    manifest: dict,
    source_binding_id: str | None,
    max_age_hours: int | None,
    now: datetime,
) -> ValidatedEvidence:
    # Step 1: internal integrity of each JSON artifact, separately.
    for record in (dry_run, manifest):
        try:
            reviewed_schema_from_record(record)
        except SchemaBaselineError:
            raise Refusal(R.FINGERPRINT_RECOMPUTE_MISMATCH) from None

    # Step 2: mapping fingerprint, mapping report rules, and rule count.
    fingerprint = mappings_fingerprint(mapping)
    _require(
        dry_run["source_mapping_fingerprint"] == fingerprint == manifest["source_mapping_fingerprint"],
        R.MAPPING_FINGERPRINT_MISMATCH,
    )
    report_rules = {
        (rule["table"].casefold(), rule["source"], rule["target"], rule["cast"])
        for rule in dry_run["source_mapping_report"]["mappings"]
    }
    total_rules = sum(len(rules) for rules in mapping.values())
    _require(
        report_rules == _rule_tuples(mapping)
        and len(dry_run["source_mapping_report"]["mappings"]) == total_rules
        and manifest["source_mapping_rule_count"] == total_rules,
        R.MAPPING_FINGERPRINT_MISMATCH,
    )

    # Step 3: combined reviewed state.
    _require(
        dry_run["schema_confirmation_fingerprint"] == manifest["schema_confirmation_fingerprint"],
        R.SCHEMA_CONFIRMATION_MISMATCH,
    )

    # Step 4: source kind.
    _require(
        dry_run["source"] == manifest["source_kind"] == manifest["source_identity"].get("kind"),
        R.SOURCE_KIND_MISMATCH,
    )

    # Step 5: operator source binding alias (an assertion, never authentication).
    values = (dry_run.get("source_binding_id"), manifest.get("source_binding_id"), source_binding_id)
    _require(all(value is not None for value in values), R.SOURCE_IDENTITY_UNAVAILABLE)
    _require(len(set(values)) == 1, R.SOURCE_IDENTITY_MISMATCH)

    # Step 6 ran in the manifest field table (SQL schema present and safe; absent for CSV).

    # Step 7: selected tables.
    selected = _casefold_set(dry_run["resolved_tables"])
    identity = manifest["source_identity"]
    if manifest["source_kind"] == "sql":
        identity_tables = _casefold_set(identity["tables"])
    else:
        identity_tables = {item["entity"].casefold() for item in identity["files"]}
    snapshot_tables = _casefold_set(entry["table"] for entry in dry_run["source_schema_snapshot"])
    safety = dry_run["normalized_identifier_safety"]
    sets = [_casefold_set(manifest["entities"]), identity_tables, snapshot_tables]
    if safety is not None:
        sets.append({table["table"].casefold() for table in safety["tables"] if table["table"] is not None})
    _require(all(candidate == selected for candidate in sets), R.SELECTED_TABLES_MISMATCH)
    dry_pairs = {
        (entity.casefold(), table.casefold())
        for entity, table in zip(dry_run["resolved_tables"], dry_run["destination_tables"])
    }
    manifest_pairs = {
        (entity.casefold(), table.casefold())
        for entity, table in zip(manifest["entities"], manifest["destination_tables"])
    }
    _require(dry_pairs == manifest_pairs, R.SELECTED_TABLES_MISMATCH)
    _require(set(mapping) <= selected, R.SELECTED_TABLES_MISMATCH)
    pair_by_entity = dict(dry_pairs)
    if safety is not None:
        for table in safety["tables"]:
            normalized = table["normalized_destination_table"]
            _require(
                table["table"] is not None and normalized is not None
                and normalized.casefold() == pair_by_entity.get(table["table"].casefold()),
                R.SELECTED_TABLES_MISMATCH,
            )

    # Step 8: destination, namespace, target identity, and edition.
    destination = dry_run["destination"]
    _require(
        destination == manifest["destination"]
        and dry_run["destination_type_profile"].get("destination") == destination
        and manifest["destination_type_profile"].get("destination") == destination
        and dry_run["destination_type_profile"] == manifest["destination_type_profile"],
        R.DESTINATION_MISMATCH,
    )
    namespace = dry_run["bronze_namespace"]
    _require(
        namespace.casefold() == manifest["bronze_namespace"].casefold()
        and manifest["bronze_namespace"].casefold().startswith(BRONZE_NAMESPACE_PREFIX),
        R.NAMESPACE_MISMATCH,
    )
    target_identity = dry_run["target_identity"]
    _require(
        target_identity is not None
        and target_identity == manifest["target_identity"]
        and target_identity.rsplit("/", 1)[-1].casefold() == namespace.casefold(),
        R.TARGET_IDENTITY_MISMATCH,
    )
    _require(EDITION_BY_DESTINATION.get(destination) == edition, R.DESTINATION_EDITION_UNBOUND)

    # Step 9: readiness. The snapshot alone cannot tell a missing table from a columnless one,
    # so every selected table must have its own inventory entry (contract pitfall 9).
    inventory_tables = [item["table"] for item in dry_run["inventory"]]
    _require(
        None not in inventory_tables
        and len(_casefold_set(inventory_tables)) == len(inventory_tables)
        and _casefold_set(inventory_tables) == selected,
        R.EVIDENCE_NOT_READY,
    )
    _require(
        all(item["status"] in READY_STATUSES for item in dry_run["inventory"])
        and safety is not None and safety.get("safe_to_extract") is True
        and set(dry_run["blockers"]) <= ALLOWED_BLOCKERS
        and (dry_run["schema_drift_review"] is None
             or dry_run["schema_drift_review"]["fresh_confirmation_verified"] is True),
        R.EVIDENCE_NOT_READY,
    )

    # Step 10: freshness, anchored only on the completed manifest.
    _require(max_age_hours is not None, R.EVIDENCE_FRESHNESS_UNAVAILABLE)
    started = _utc_timestamp(manifest["started_at_utc"])
    finished = _utc_timestamp(manifest["finished_at_utc"])
    _require(started <= finished <= now, R.EVIDENCE_STALE)
    _require(now - finished <= timedelta(hours=max_age_hours), R.EVIDENCE_STALE)

    bronze = {}
    tables_by_entity = {
        table["table"].casefold(): table for table in safety["tables"] if table["table"] is not None
    }
    for entity in sorted(mapping):
        table = tables_by_entity[entity]
        bronze[entity] = BronzeTable(
            entity=entity,
            bronze_table=table["normalized_destination_table"],
            normalized_columns=tuple(
                (column["source"], column["destination"]) for column in table["normalized_columns"]
            ),
        )
    return ValidatedEvidence(
        edition=edition,
        mapping={table: tuple(dict(rule) for rule in rules) for table, rules in mapping.items()},
        bronze=bronze,
        bronze_namespace=namespace,
        source_mapping_fingerprint=fingerprint,
        schema_confirmation_fingerprint=dry_run["schema_confirmation_fingerprint"],
    )


def validate_inputs(
    *,
    edition: str,
    mapping_path: str | None,
    dry_run_path: str | None,
    run_manifest_path: str | None,
    source_binding_id: str | None,
    max_age_hours: int | None,
    now: datetime | None = None,
) -> ValidatedEvidence:
    """Run the full input contract; return bound evidence or raise Refusal. Writes nothing."""
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    # 1. Admission limits for every artifact, in role order.
    mapping = admit_mapping(mapping_path)
    dry_run, _ = admit_private_json(dry_run_path)
    manifest, manifest_name = admit_private_json(run_manifest_path)
    _check_bounds(dry_run)
    _check_bounds(manifest)
    # 2. Field tables.
    check_dry_run_fields(dry_run)
    check_run_manifest_fields(manifest, manifest_name)
    # 3. Binding steps 1-10.
    return bind(
        edition=edition, mapping=mapping, dry_run=dry_run, manifest=manifest,
        source_binding_id=source_binding_id, max_age_hours=max_age_hours, now=current,
    )
