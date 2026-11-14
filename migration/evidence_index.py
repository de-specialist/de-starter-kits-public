#!/usr/bin/env python3
"""Create a private, allowlisted evidence index from one migration run manifest."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from _private_json import create_private_json, read_private_json


ROOT = Path(__file__).resolve().parent
MANIFEST_DIR = ROOT / "run-manifests"
_RUN_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_LOAD_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_JOB_STATUSES = ("new_jobs", "started_jobs", "completed_jobs", "failed_jobs")


def _read_manifest(filename: str) -> dict[str, object]:
    return read_private_json(MANIFEST_DIR, filename)


def _nonnegative_int(value: object, label: str, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Manifest field {label} must be a nonnegative integer.")
    return value


def _sha256_value(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _HEX_SHA256.fullmatch(value):
        raise ValueError(f"Manifest field {label} is not a SHA-256 fingerprint.")
    return value


def _safe_source_identity(value: object, source_kind: str, entities: list[str]) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("kind") != source_kind:
        raise ValueError("Manifest source identity is malformed.")
    if source_kind == "sql":
        schema = value.get("schema")
        tables = value.get("tables")
        if not isinstance(schema, str) or not _SAFE_IDENTIFIER.fullmatch(schema):
            raise ValueError("Manifest SQL source schema is malformed.")
        if not isinstance(tables, list) or len(tables) != len(entities) or not all(
            isinstance(table, str) and _SAFE_IDENTIFIER.fullmatch(table) for table in tables
        ) or len({table.casefold() for table in tables}) != len(tables) or {table.casefold() for table in tables} != {entity.casefold() for entity in entities}:
            raise ValueError("Manifest SQL source table list is malformed.")
        if value.get("connection_identity") != "redacted":
            raise ValueError("Manifest SQL connection identity is not redacted.")
        if value.get("identity_scope") != "descriptive_names_not_unique":
            raise ValueError("Manifest SQL identity scope is malformed.")
        return {
            "kind": "sql",
            "schema": schema,
            "tables": sorted(tables, key=str.casefold),
            "connection_identity": "redacted",
            "identity_scope": "descriptive_names_not_unique",
        }

    files = value.get("files")
    if not isinstance(files, list) or len(files) != len(entities):
        raise ValueError("Manifest CSV source file list is malformed.")
    safe_files: list[dict[str, object]] = []
    legacy_entries = 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Manifest CSV source file entry is malformed.")
        entity = item.get("entity")
        if not isinstance(entity, str) or not _SAFE_IDENTIFIER.fullmatch(entity):
            raise ValueError("Manifest CSV entity name is malformed.")
        size_bytes = _nonnegative_int(item.get("size_bytes"), "source_identity.files.size_bytes")
        file_name = item.get("file_name")
        if file_name is not None:
            if (
                not isinstance(file_name, str)
                or not file_name
                or len(file_name) > 255
                or Path(file_name).name != file_name
                or file_name in {".", ".."}
                or "/" in file_name
                or "\\" in file_name
                or any(ord(char) < 32 or ord(char) == 127 for char in file_name)
                or not file_name.casefold().endswith(".csv")
                or Path(file_name).stem.casefold() != entity.casefold()
            ):
                raise ValueError("Manifest CSV source file name is malformed.")
        else:
            # Accept older manifests, but never propagate their deterministic
            # content hashes into the private evidence-index output.
            digest = _sha256_value(item.get("sha256"), "source_identity.files.sha256")
            if digest is None:
                raise ValueError("Legacy manifest CSV entry is missing its SHA-256 fingerprint.")
            legacy_entries += 1
        safe_item: dict[str, object] = {"entity": entity, "size_bytes": size_bytes}
        if file_name is not None:
            safe_item["file_name"] = file_name
        safe_files.append(safe_item)
    if {item["entity"].casefold() for item in safe_files} != {entity.casefold() for entity in entities}:
        raise ValueError("Manifest CSV source entities do not match the selected entities.")
    if legacy_entries not in {0, len(files)}:
        raise ValueError("Manifest CSV entries mix current and legacy identity formats.")
    expected_scope = "pre_extraction_file_reference" if legacy_entries else "pre_extraction_file_metadata"
    if value.get("identity_scope") != expected_scope:
        raise ValueError("Manifest CSV identity scope is malformed.")
    if legacy_entries:
        safe_files = [{key: value for key, value in item.items() if key != "file_name"} for item in safe_files]
    return {
        "kind": "csv",
        "files": sorted(safe_files, key=lambda item: str(item["entity"]).casefold()),
        "identity_scope": expected_scope,
        "note": (
            "Legacy content hashes were validated but omitted from this index; their file sizes do not prove consumed bytes."
            if legacy_entries
            else "File names and sizes are pre-extraction metadata and do not prove which bytes were consumed."
        ),
    }


def _safe_target_identity(destination: str, value: object, namespace: str, pipeline_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Manifest target identity is malformed.")
    if destination == "bigquery":
        parts = value.split("/")
        if len(parts) != 2 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", parts[0]) or parts[1].casefold() != namespace.casefold():
            raise ValueError("Manifest BigQuery target identity is malformed.")
    elif destination == "snowflake":
        parts = value.split("/")
        if len(parts) != 3 or any(not re.fullmatch(r"[A-Za-z0-9_.:-]+", part) for part in parts[:2]) or parts[2].casefold() != namespace.casefold():
            raise ValueError("Manifest Snowflake target identity is malformed.")
    else:
        prefix = "duckdb/"
        if not value.startswith(prefix):
            raise ValueError("Manifest DuckDB target identity is malformed.")
        file_path, separator, actual_namespace = value[len(prefix):].rpartition("/")
        expected = ROOT / ".dlt" / "pipelines" / pipeline_name / f"{pipeline_name}.duckdb"
        if not separator or actual_namespace.casefold() != namespace.casefold() or Path(file_path) != expected:
            raise ValueError("Manifest DuckDB target identity is outside the migration-local pipeline path.")
    return value


def _validate_manifest(manifest: dict[str, object], filename_run_id: str) -> dict[str, object]:
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or run_id != filename_run_id:
        raise ValueError("Manifest run ID does not match its canonical filename.")
    status = manifest.get("status")
    if status not in {"completed", "failed"}:
        raise ValueError("Evidence indexes require a completed or failed run manifest.")
    source_kind = manifest.get("source_kind")
    if source_kind not in {"csv", "sql"}:
        raise ValueError("Manifest source kind is invalid.")
    destination = manifest.get("destination")
    if destination not in {"duckdb", "bigquery", "snowflake"}:
        raise ValueError("Manifest destination is invalid.")
    namespace = manifest.get("bronze_namespace")
    if not isinstance(namespace, str) or not _SAFE_IDENTIFIER.fullmatch(namespace) or not namespace.lower().startswith("starterkit_migration_"):
        raise ValueError("Manifest Bronze namespace is invalid.")
    pipeline_name = manifest.get("pipeline_name")
    if not isinstance(pipeline_name, str) or not re.fullmatch(r"[a-z0-9_]{1,120}", pipeline_name):
        raise ValueError("Manifest pipeline name is invalid.")
    entities = manifest.get("entities")
    if not isinstance(entities, list) or not entities or len(entities) > 1000 or not all(
        isinstance(entity, str) and _SAFE_IDENTIFIER.fullmatch(entity) for entity in entities
    ) or len({entity.casefold() for entity in entities}) != len(entities):
        raise ValueError("Manifest entity list is malformed.")
    if manifest.get("write_disposition") != "replace":
        raise ValueError("Manifest write disposition is invalid.")
    partial = manifest.get("destination_write_may_be_partial")
    if not isinstance(partial, bool):
        raise ValueError("Manifest partial-write flag is malformed.")
    identity = _safe_source_identity(manifest.get("source_identity"), source_kind, entities)
    target_identity = _safe_target_identity(destination, manifest.get("target_identity"), namespace, pipeline_name)
    source_schema_fingerprint = _sha256_value(manifest.get("source_schema_fingerprint"), "source_schema_fingerprint")
    typed_schema_fingerprint = _sha256_value(manifest.get("typed_schema_fingerprint"), "typed_schema_fingerprint")
    source_mapping_fingerprint = _sha256_value(manifest.get("source_mapping_fingerprint"), "source_mapping_fingerprint")
    if not all((source_schema_fingerprint, typed_schema_fingerprint, source_mapping_fingerprint)):
        raise ValueError("Manifest is missing a required schema or mapping fingerprint.")

    source_counts = manifest.get("source_row_counts", {})
    if not isinstance(source_counts, dict) or not set(source_counts).issubset(set(entities)):
        raise ValueError("Manifest source row-count map is malformed.")
    raw_progress = manifest.get("accepted_extraction_progress")
    current_count_format = raw_progress is not None or any(
        isinstance(value, dict) and "extraction_status" in value for value in source_counts.values()
    )
    if current_count_format and set(source_counts) != set(entities):
        raise ValueError("Current manifest source row-count map must cover every entity.")
    safe_counts: dict[str, dict[str, object]] = {}
    legacy_progress: dict[str, dict[str, int]] = {}
    for entity, count_record in source_counts.items():
        if not isinstance(count_record, dict):
            raise ValueError("Manifest source row-count entry is malformed.")
        rows = _nonnegative_int(count_record.get("rows"), "source_row_counts.rows", optional=True)
        payload_bytes = _nonnegative_int(count_record.get("payload_bytes"), "source_row_counts.payload_bytes", optional=True)
        if (rows is None) != (payload_bytes is None):
            raise ValueError("Source row and payload-byte totals must both be present or both be null.")
        provenance = count_record.get("provenance")
        allowed_provenance = {
            "application_counted_canonical_json_payload",
            "rows_yielded_from_csv_resource",
            "not_independently_counted_sql_source",
        }
        if provenance not in allowed_provenance:
            raise ValueError("Manifest row-count provenance is not recognized.")
        if current_count_format:
            extraction_status = count_record.get("extraction_status")
            if extraction_status not in {"not_started", "incomplete", "complete"}:
                raise ValueError("Manifest extraction status is not recognized.")
            if (extraction_status == "complete") != (rows is not None):
                raise ValueError("Verified source totals must be present only for completed extraction.")
        else:
            # Older failed manifests used zero as the default for entities that may never
            # have started. Preserve that uncertainty; do not relabel those rows as verified.
            extraction_status = "complete" if status == "completed" and rows is not None else "unverified_legacy"
            if extraction_status == "unverified_legacy":
                if rows is not None and payload_bytes is not None:
                    legacy_progress[entity] = {"rows": rows, "payload_bytes": payload_bytes}
                rows = payload_bytes = None
        safe_counts[entity] = {
            "rows": rows, "payload_bytes": payload_bytes,
            "extraction_status": extraction_status, "provenance": provenance,
        }

    if raw_progress is None:
        raw_progress = legacy_progress
    if not isinstance(raw_progress, dict) or not set(raw_progress).issubset(set(entities)):
        raise ValueError("Manifest accepted extraction progress map is malformed.")
    if current_count_format and set(raw_progress) != set(entities):
        raise ValueError("Current manifest accepted progress map must cover every entity.")
    safe_progress: dict[str, dict[str, int]] = {}
    for entity, progress in raw_progress.items():
        if not isinstance(progress, dict):
            raise ValueError("Manifest accepted extraction progress entry is malformed.")
        safe_progress[entity] = {
            "rows": _nonnegative_int(progress.get("rows"), "accepted_extraction_progress.rows"),
            "payload_bytes": _nonnegative_int(progress.get("payload_bytes"), "accepted_extraction_progress.payload_bytes"),
        }
    if current_count_format:
        for entity in entities:
            source = safe_counts[entity]
            progress = safe_progress[entity]
            if source["extraction_status"] == "complete":
                if source["rows"] != progress["rows"] or source["payload_bytes"] != progress["payload_bytes"]:
                    raise ValueError("Completed source totals must match accepted extraction progress.")
            elif source["extraction_status"] == "not_started" and progress != {"rows": 0, "payload_bytes": 0}:
                raise ValueError("Not-started entities cannot report accepted extraction progress.")

    load_ids = manifest.get("load_ids", [])
    if not isinstance(load_ids, list) or len(load_ids) > 10000 or not all(
        isinstance(load_id, str) and _SAFE_LOAD_ID.fullmatch(load_id) for load_id in load_ids
    ):
        raise ValueError("Manifest load ID list is malformed.")
    package_count = manifest.get("load_package_count")
    package_count = _nonnegative_int(package_count, "load_package_count", optional=True)
    raw_jobs = manifest.get("load_job_counts", {})
    if not isinstance(raw_jobs, dict) or not set(raw_jobs).issubset(_JOB_STATUSES):
        raise ValueError("Manifest load-job counts are malformed.")
    jobs = {status_name: _nonnegative_int(raw_jobs.get(status_name, 0), f"load_job_counts.{status_name}") for status_name in _JOB_STATUSES}
    evidence_incomplete = manifest.get("load_evidence_incomplete", False)
    if not isinstance(evidence_incomplete, bool):
        raise ValueError("Manifest load-evidence flag is malformed.")

    return {
        "run_id": run_id,
        "status": status,
        "source_kind": source_kind,
        "source_identity": identity,
        "destination": destination,
        "target_identity": target_identity,
        "bronze_namespace": namespace,
        "entities": entities,
        "source_schema_fingerprint": source_schema_fingerprint,
        "typed_schema_fingerprint": typed_schema_fingerprint,
        "source_mapping_fingerprint": source_mapping_fingerprint,
        "source_row_counts": safe_counts,
        "accepted_extraction_progress": safe_progress,
        "load_ids": load_ids,
        "load_package_count": package_count,
        "load_job_counts": jobs,
        "load_evidence_incomplete": evidence_incomplete,
        "destination_write_may_be_partial": partial,
    }


def build_index(manifest: dict[str, object]) -> dict[str, object]:
    return {
        "evidence_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_id": manifest.get("run_id"),
        "run_status": manifest.get("status"),
        "source_kind": manifest.get("source_kind"),
        "source_identity": manifest.get("source_identity"),
        "destination": manifest.get("destination"),
        "target_identity": manifest.get("target_identity"),
        "bronze_namespace": manifest.get("bronze_namespace"),
        "entities": manifest.get("entities", []),
        "source_schema_fingerprint": manifest.get("source_schema_fingerprint"),
        "typed_schema_fingerprint": manifest.get("typed_schema_fingerprint"),
        "source_mapping_fingerprint": manifest.get("source_mapping_fingerprint"),
        "source_row_counts": manifest.get("source_row_counts", {}),
        "accepted_extraction_progress": manifest.get("accepted_extraction_progress", {}),
        "load_ids": manifest.get("load_ids", []),
        "load_package_count": manifest.get("load_package_count"),
        "load_job_counts": manifest.get("load_job_counts", {}),
        "load_evidence_incomplete": manifest.get("load_evidence_incomplete", False),
        "destination_write_may_be_partial": manifest.get("destination_write_may_be_partial"),
        "validation_limitations": [
            "The index summarizes recorded run evidence; it does not independently verify warehouse contents.",
            "No dbt model tests, Silver/Gold reconciliation, cost validation, or cutover approval are implied.",
            "Fingerprints and target/entity identities can reveal equality or customer environment details; keep this file private.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="Canonical run manifest filename under migration/run-manifests/.")
    args = parser.parse_args()
    try:
        if not isinstance(args.manifest, str) or Path(args.manifest).name != args.manifest:
            raise ValueError("Pass only a manifest filename, not an absolute or nested path.")
        filename_match = re.fullmatch(r"([0-9a-f-]{36})\.json", args.manifest)
        if not filename_match or not _RUN_ID.fullmatch(filename_match.group(1)):
            raise ValueError("Manifest filename must be a canonical run UUID followed by .json.")
        filename_run_id = filename_match.group(1)
        manifest = _validate_manifest(_read_manifest(args.manifest), filename_run_id)
        output_name = f"evidence-index-{filename_run_id}.json"
        create_private_json(MANIFEST_DIR, output_name, build_index(manifest))
        print(f"Private evidence index created: {output_name}")
        return 0
    except Exception as error:
        print(f"Evidence index stopped ({type(error).__name__}); no existing file was overwritten.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
