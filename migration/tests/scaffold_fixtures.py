"""SYNTHETIC fixtures for the offline Silver scaffolder tests.

Every name, alias, and identity here is synthetic. Fixtures are built in temporary
directories outside the repository and never contact a source, warehouse, or network.
Evidence records are constructed to be internally consistent with the shared migration
fingerprint functions, so a test mutates exactly one property to trigger one reason code.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migration"
if str(MIGRATION) not in sys.path:
    sys.path.insert(0, str(MIGRATION))

from destination_type_profiles import resolve_type_profile  # noqa: E402
from mapping_contract import mappings_fingerprint  # noqa: E402
from schema_drift import (  # noqa: E402
    SCHEMA_CONFIRMATION_ALGORITHM,
    SNAPSHOT_ALGORITHM,
    schema_confirmation_fingerprint,
    schema_snapshot,
    snapshot_name_fingerprint,
    snapshot_typed_fingerprint,
)

SOURCE_ID = "synth_src"
BINDING_ID = "synthetic_retail_v1"
NAMESPACE = "starterkit_migration_synth"
NOW = datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc)
FINISHED = "2026-01-01T12:00:00+00:00"
STARTED = "2026-01-01T11:00:00+00:00"
EXTRA_COLUMN = "synthetic_private_note"
PIPELINE = "synthetic_pipeline"

EXAMPLE_MAPPING = json.loads((MIGRATION / "mappings" / "retail-source.example.json").read_text("utf-8"))


def default_mapping() -> dict:
    return copy.deepcopy(EXAMPLE_MAPPING)


def _normalize(name: str, destination: str) -> str:
    folded = name.lower()
    return folded.upper() if destination == "snowflake" else folded


def target_identity(destination: str, namespace: str = NAMESPACE) -> str:
    if destination == "bigquery":
        return f"synthetic-project/{namespace}"
    if destination == "snowflake":
        return f"SYNTH_ACCOUNT/SYNTH_DB/{namespace}"
    path = MIGRATION / ".dlt" / "pipelines" / PIPELINE / f"{PIPELINE}.duckdb"
    return f"duckdb/{path}/{namespace}"


def build_evidence(
    mapping_doc: dict,
    *,
    destination: str = "bigquery",
    source: str = "csv",
    raw_columns: dict[str, list[str]] | None = None,
    binding: str | None = BINDING_ID,
) -> tuple[dict, dict]:
    """Return a consistent (dry_run, run_manifest) pair for a mapping document."""
    tables = {name.casefold(): rules for name, rules in mapping_doc["tables"].items()}
    if raw_columns is None:
        raw_columns = {table: [rule["source"] for rule in rules] + [EXTRA_COLUMN] for table, rules in tables.items()}
    inventory = [
        {"table": table, "status": "ready",
         "columns": [{"name": name, "type": "csv source field text"} for name in columns]}
        for table, columns in sorted(raw_columns.items())
    ]
    snapshot = schema_snapshot(inventory)
    name_fp = snapshot_name_fingerprint(snapshot)
    typed_fp = snapshot_typed_fingerprint(snapshot)
    mapping_fp = mappings_fingerprint(tables)
    confirmation = schema_confirmation_fingerprint(name_fp, typed_fp, mapping_fp)
    entities = [item["table"] for item in inventory]
    destination_tables = [_normalize(name, destination) for name in entities]
    safety_tables = []
    for item in inventory:
        names = [column["name"] for column in item["columns"]]
        names += [
            rule["target"] for rule in tables.get(item["table"], [])
            if rule["source"].lower() != rule["target"].lower()
        ]
        safety_tables.append({
            "table": item["table"],
            "normalized_destination_table": _normalize(item["table"], destination),
            "status": "safe",
            "normalized_columns": [{"source": name, "destination": _normalize(name, destination)} for name in names],
            "collisions_after_normalization": [],
            "dlt_reserved_prefix_columns": [],
            "dlt_reserved_prefix_table": False,
            "generated_lineage_collisions": [],
        })
    profile = resolve_type_profile(destination).describe()
    identity = target_identity(destination)
    common = {
        "source_schema_snapshot": snapshot,
        "source_schema_snapshot_algorithm": SNAPSHOT_ALGORITHM,
        "source_schema_fingerprint": name_fp,
        "source_schema_fingerprint_algorithm": "sha256-table-and-ordered-column-names-v1",
        "typed_schema_fingerprint": typed_fp,
        "typed_schema_fingerprint_algorithm": "sha256-table-column-type-families-v1",
        "schema_confirmation_fingerprint": confirmation,
        "schema_confirmation_algorithm": SCHEMA_CONFIRMATION_ALGORITHM,
        "source_mapping_fingerprint": mapping_fp,
        "destination_type_profile": profile,
        "bronze_namespace": NAMESPACE,
        "destination": destination,
        "target_identity": identity,
        "destination_tables": destination_tables,
    }
    dry_run = {
        "mode": "dry_run",
        "source": source,
        **common,
        "schema_drift_review": None,
        "source_mapping_report": {
            "valid": True, "errors": [],
            "mappings": [{"table": table, **rule} for table, rules in tables.items() for rule in rules],
        },
        "inventory": inventory,
        "normalized_identifier_safety": {
            "status": "safe", "safe_to_extract": True, "normalized_dlt_reserved_prefix": "_dlt",
            "generated_lineage_fields": [], "tables": safety_tables, "note": "synthetic",
        },
        "retail_contract_compatibility": None,
        "retail_value_profile": None,
        "resolved_tables": entities,
        "table_confirmation": ",".join(destination_tables),
        "target_verified": False,
        "namespace_verified": False,
        "table_confirmation_verified": False,
        "preflight_ready": False,
        "ready_to_load": False,
        "blockers": ["target_not_verified", "table_list_not_confirmed"],
        "extraction_limits": {},
        "writes_performed": False,
    }
    if source == "sql":
        source_identity = {"kind": "sql", "schema": "synthetic_schema", "tables": list(entities),
                           "connection_identity": "redacted", "identity_scope": "descriptive_names_not_unique"}
    else:
        source_identity = {"kind": "csv", "identity_scope": "pre_extraction_file_metadata", "note": "synthetic",
                           "files": [{"entity": name, "file_name": f"{name}.csv", "size_bytes": 10} for name in entities]}
    manifest = {
        "run_id": str(uuid.UUID(int=0x5EED, version=4)),
        "pipeline_name": PIPELINE,
        "source_kind": source,
        "entities": entities,
        "source_identity": source_identity,
        "allow_empty_tables": [],
        "write_disposition": "replace",
        "source_row_counts": {},
        "started_at_utc": STARTED,
        "finished_at_utc": FINISHED,
        "status": "completed",
        "destination_write_may_be_partial": False,
        "source_schema_fingerprint_status": "matched_at_extraction",
        "typed_schema_fingerprint_status": "matched_at_extraction",
        "source_schema_fingerprint_observed": name_fp,
        "typed_schema_fingerprint_observed": typed_fp,
        "source_mapping_rule_count": sum(len(rules) for rules in tables.values()),
        "load_ids": [],
        "load_job_counts": {},
        **common,
    }
    if binding is not None:
        dry_run["source_binding_id"] = binding
        manifest["source_binding_id"] = binding
    return dry_run, manifest


def write_private(path: Path, payload: bytes | str, mode: int = 0o600) -> Path:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    os.chmod(path, mode)
    return path


def write_bundle(root: Path, mapping_doc, dry_run, manifest, *, manifest_name: str | None = None) -> dict[str, Path]:
    evidence = root / "evidence"
    evidence.mkdir(mode=0o700)
    mapping_payload = mapping_doc if isinstance(mapping_doc, (str, bytes)) else json.dumps(mapping_doc, indent=2)
    dry_payload = dry_run if isinstance(dry_run, (str, bytes)) else json.dumps(dry_run, indent=2)
    manifest_payload = manifest if isinstance(manifest, (str, bytes)) else json.dumps(manifest, indent=2)
    name = manifest_name or f"{manifest['run_id'] if isinstance(manifest, dict) else uuid.UUID(int=0x5EED, version=4)}.json"
    return {
        "mapping": write_private(evidence / "mapping.json", mapping_payload),
        "dry_run": write_private(evidence / "dry-run.json", dry_payload),
        "run_manifest": write_private(evidence / name, manifest_payload),
    }


def make_workspace(root: Path, edition: str = "bigquery") -> Path:
    workspace = root / f"workspace_{edition}"
    project = workspace if edition == "bigquery" else workspace / "kit"
    (project / "models" / "migration").mkdir(parents=True)
    name = "retail_analytics" if edition == "bigquery" else "reliable_pipeline"
    (project / "dbt_project.yml").write_text(f"name: '{name}'\nversion: '1.0.0'\nprofile: {name}\n", "utf-8")
    return workspace


def project_dir(workspace: Path, edition: str) -> Path:
    return workspace if edition == "bigquery" else workspace / "kit"


def argv(workspace: Path, paths: dict[str, Path], *, edition: str = "bigquery", extra: list[str] | None = None,
         binding: str | None = BINDING_ID, max_age: str | None = "24", dry_run: bool = True) -> list[str]:
    args = ["--edition", edition, "--workspace", str(workspace), "--source-id", SOURCE_ID]
    if "mapping" in paths:
        args += ["--mapping-file", str(paths["mapping"])]
    if "dry_run" in paths:
        args += ["--inventory-file", str(paths["dry_run"])]
    if "run_manifest" in paths:
        args += ["--run-manifest", str(paths["run_manifest"])]
    if binding is not None:
        args += ["--source-binding-id", binding]
    if max_age is not None:
        args += ["--max-evidence-age-hours", max_age]
    if dry_run:
        args.append("--dry-run")
    return args + (extra or [])


def tree_snapshot(root: Path) -> dict[str, tuple]:
    """Relative path -> (kind, mode, content or link target) without following links."""
    result = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = Path(directory) / name
            info = path.lstat()
            if path.is_symlink():
                value = ("link", os.readlink(path))
            elif path.is_file():
                value = ("file", path.read_bytes())
            else:
                value = ("other", None)
            result[str(path.relative_to(root))] = (value[0], info.st_mode, value[1])
    return result
