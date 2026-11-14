#!/usr/bin/env python3
"""Write a privacy-safe Bronze row-count reconciliation report for a completed run."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

# Match the migration CLI before importing dlt: configuration comes from this
# project's .dlt directory, and telemetry stays disabled for offline reporting.
MIGRATION_ROOT = Path(__file__).resolve().parent
os.environ["DLT_PROJECT_DIR"] = str(MIGRATION_ROOT)
os.environ["DLT_CONFIG_FOLDER"] = ".dlt"
os.environ["RUNTIME__DLTHUB_TELEMETRY"] = "false"
logging.disable(logging.CRITICAL)

import dlt
from dlt.common.schema.typing import DLT_NAME_PREFIX
from sqlalchemy import MetaData, Table, select
from sqlalchemy.engine import make_url

import acceptance_report
from _private_json import create_private_json, read_private_json
from run_migration import (
    _DEFAULT_NAME_NORMALIZER,
    _RETAIL_TESTS,
    _SAFE_IDENTIFIER,
    _cast_mapped_value,
    _csv_reader,
    _dlt_destination,
    _is_ascii_space_blank,
    _pipeline_name,
    _resolve_dlt_destination,
    _validated_csv_rows,
    create_read_only_sql_engine,
    discover_csv_files,
    ensure_private_directory,
    inventory_csv_inputs,
    inventory_sql_inputs,
    load_mapping_file,
    mappings_fingerprint,
    open_csv_input,
    schema_fingerprint,
    typed_schema_fingerprint,
)


_RUN_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Default in-memory key cap per entity; matches the loader's default total row budget.
_DEFAULT_MAX_KEY_ROWS_PER_TABLE = 1_000_000


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, help="Completed migration run UUID from its manifest.")
    parser.add_argument(
        "--query-destination",
        action="store_true",
        help="Explicitly query current Bronze table counts; this contacts the destination and may incur compute cost.",
    )
    parser.add_argument(
        "--target-id",
        help="Non-secret project ID or Snowflake account/database, required when querying a cloud destination.",
    )
    parser.add_argument(
        "--confirm-target",
        help="Type the exact target identity recorded in the migration manifest before querying destination tables.",
    )
    parser.add_argument(
        "--confirm-tables",
        help="Type the exact comma-separated normalized table list shown in the offline report.",
    )
    local = parser.add_argument_group(
        "local key/relationship reconciliation",
        "Re-scan the run's local CSV directory or SQLite file and write count-only key evidence. "
        "Never contacts a destination; key values are held in memory only.",
    )
    local.add_argument(
        "--local-keys",
        action="store_true",
        help="Write <run-id>.key-reconciliation.local.json instead of a count report.",
    )
    local.add_argument("--csv-dir", type=Path, help="CSV source directory used for the run (CSV manifests).")
    local.add_argument("--csv-glob", default="*.csv", help="CSV file glob used for the run (default: *.csv).")
    local.add_argument(
        "--source-url-env",
        default="MIGRATION_SOURCE_URL",
        help="Environment variable holding the SQLite URL used for the run (SQL manifests; SQLite only).",
    )
    local.add_argument(
        "--mapping-file",
        type=Path,
        help="The same mapping file used for the run; its fingerprint must match the manifest.",
    )
    local.add_argument(
        "--max-key-rows-per-table",
        type=int,
        default=_DEFAULT_MAX_KEY_ROWS_PER_TABLE,
        help="Stop scanning an entity beyond this many rows and mark its key evidence unavailable.",
    )
    return parser.parse_args()


def _validate_manifest_target(record: dict[str, object]) -> None:
    destination = record.get("destination")
    namespace = record.get("bronze_namespace")
    pipeline_name = record.get("pipeline_name")
    tables = record.get("destination_tables")
    entities = record.get("entities")
    if destination not in {"duckdb", "bigquery", "snowflake"}:
        raise ValueError("Manifest destination is invalid.")
    if not isinstance(namespace, str) or not _SAFE_IDENTIFIER.fullmatch(namespace):
        raise ValueError("Manifest Bronze namespace is invalid.")
    if not namespace.lower().startswith("starterkit_migration_"):
        raise ValueError("Manifest namespace is outside the migration namespace prefix.")
    if not isinstance(pipeline_name, str) or len(pipeline_name) > 120 or pipeline_name != pipeline_name.lower():
        raise ValueError("Manifest pipeline name is invalid.")
    legacy_pattern = re.compile(
        rf"(?P<base>[A-Za-z0-9_]+)_{re.escape(destination)}_{re.escape(namespace.lower())}_[0-9a-f]{{24}}"
    )
    compact_pattern = re.compile(
        rf"(?P<base>[A-Za-z0-9_]+)_{re.escape(destination)}_c[0-9a-f]{{24}}"
    )
    legacy_match = legacy_pattern.fullmatch(pipeline_name)
    compact_match = compact_pattern.fullmatch(pipeline_name)
    match = legacy_match or compact_match
    if not match or not _SAFE_IDENTIFIER.fullmatch(match.group("base")):
        raise ValueError("Manifest pipeline name does not follow the migration loader naming rule.")
    if compact_match:
        if destination == "duckdb":
            target_hint = f"duckdb/{namespace}"
        else:
            target_parts = str(record.get("target_identity", "")).split("/")
            expected_parts = 2 if destination == "bigquery" else 3
            if len(target_parts) != expected_parts or target_parts[-1].casefold() != namespace.casefold():
                raise ValueError("Compacted pipeline name cannot be bound to the manifest target identity.")
            target_hint = "/".join([*target_parts[:-1], namespace])
        expected_name = _pipeline_name(
            SimpleNamespace(pipeline_name=match.group("base"), destination=destination),
            namespace,
            target_hint,
        )
        if pipeline_name != expected_name:
            raise ValueError("Compacted pipeline name hash does not match the manifest target.")
    if not isinstance(tables, list) or not tables or not all(
        isinstance(name, str) and _SAFE_IDENTIFIER.fullmatch(name) and name == name.casefold()
        for name in tables
    ):
        raise ValueError("Manifest destination table names are not normalized migration identifiers.")
    if len({name.casefold() for name in tables}) != len(tables):
        raise ValueError("Manifest destination table list contains normalized-name collisions.")
    # Manifest table names are already dlt-normalized and case-folded by the loader;
    # apply the same case-folding convention to dlt's reserved prefix constant.
    normalized_dlt_prefix = DLT_NAME_PREFIX.casefold()
    if any(name.startswith(normalized_dlt_prefix) for name in tables):
        raise ValueError("Manifest destination table list uses dlt's reserved table-name prefix.")
    if not isinstance(entities, list) or len(entities) != len(tables) or not all(
        isinstance(name, str) and _SAFE_IDENTIFIER.fullmatch(name) for name in entities
    ):
        raise ValueError("Manifest source entity names do not follow the migration loader identifier rule.")


def _read_completed_manifest(run_id: str) -> tuple[Path, dict[str, object]]:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("Run ID must be a UUID.")
    manifest_dir = MIGRATION_ROOT / "run-manifests"
    manifest_path = manifest_dir / f"{run_id}.json"
    record = read_private_json(manifest_dir, f"{run_id}.json")
    if not isinstance(record, dict) or record.get("run_id") != run_id or record.get("status") != "completed":
        raise ValueError("Only a completed manifest for the requested run can be reconciled.")
    if record.get("write_disposition") != "replace":
        raise ValueError("Reconciliation supports only completed full-replacement runs.")
    _validate_manifest_target(record)
    return manifest_path, record


def _destination_counts(pipeline, tables: list[str]) -> dict[str, int]:
    # dlt 1.32.0's public Dataset.row_counts returns a relation with table_name and row_count.
    rows = pipeline.dataset().row_counts(table_names=tables).fetchall()
    result: dict[str, int] = {}
    for row in rows:
        if isinstance(row, dict):
            table_name, count = row.get("table_name"), row.get("row_count")
        elif hasattr(row, "_mapping"):
            mapping = row._mapping
            table_name, count = mapping.get("table_name"), mapping.get("row_count")
        else:
            table_name, count = row[0], row[1]
        if not isinstance(table_name, str) or isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("Destination returned an unexpected row-count result shape.")
        key = table_name.casefold()
        if key in result:
            raise ValueError("Destination returned duplicate table counts.")
        result[key] = count
    if set(result) != {table.casefold() for table in tables}:
        raise ValueError("Destination did not return a count for every confirmed Bronze table.")
    return result


def _attach_pipeline(pipeline_name: str, destination: str, dataset: str, pipelines_root: Path):
    """Attach with the same destination overrides used by the loader."""
    return dlt.attach(
        pipeline_name=pipeline_name,
        pipelines_dir=str(pipelines_root),
        destination=_dlt_destination(destination),
        dataset_name=dataset,
    )


def build_report(record: dict[str, object], destination_counts: dict[str, int] | None) -> dict[str, object]:
    tables = record.get("destination_tables")
    entities = record.get("entities")
    if not isinstance(tables, list) or not tables or not all(isinstance(name, str) for name in tables):
        raise ValueError("Manifest does not contain a valid destination table list.")
    if not isinstance(entities, list) or len(entities) != len(tables) or not all(isinstance(name, str) for name in entities):
        raise ValueError("Manifest source and destination entity lists do not align.")

    source_counts = record.get("source_row_counts", {})
    prepared_counts = record.get("dlt_prepared_row_counts", {})
    source_counts = source_counts if isinstance(source_counts, dict) else {}
    prepared_counts = prepared_counts if isinstance(prepared_counts, dict) else {}

    def count_or_none(value) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def comparison_status(left: int | None, right: int | None) -> str:
        if left is None or right is None:
            return "count_unavailable"
        return "count_match" if left == right else "count_mismatch"

    def combined_status(statuses: list[str]) -> str:
        if "count_mismatch" in statuses:
            return "count_mismatch"
        if "count_unavailable" in statuses:
            return "count_unavailable"
        return "count_match"

    rows: list[dict[str, object]] = []
    for entity, table in zip(entities, tables):
        source_entry = source_counts.get(entity, {})
        prepared_entry = prepared_counts.get(table, {})
        source_entry = source_entry if isinstance(source_entry, dict) else {}
        prepared_entry = prepared_entry if isinstance(prepared_entry, dict) else {}
        source_rows = count_or_none(source_entry.get("rows"))
        prepared_rows = count_or_none(prepared_entry.get("rows"))
        destination_rows = destination_counts.get(table.casefold()) if destination_counts is not None else None
        destination_rows = count_or_none(destination_rows)
        source_to_prepared = comparison_status(source_rows, prepared_rows)
        prepared_to_destination = comparison_status(prepared_rows, destination_rows)
        table_status = combined_status([source_to_prepared, prepared_to_destination])
        rows.append({
            "source_entity": entity,
            "bronze_table": table,
            "status": table_status,
            "source_rows": source_rows,
            "source_count_provenance": source_entry.get("provenance"),
            "dlt_prepared_rows": prepared_rows,
            "dlt_prepared_count_provenance": prepared_entry.get("provenance"),
            "current_destination_rows": destination_rows,
            "source_to_prepared": source_to_prepared,
            "prepared_to_current_destination": prepared_to_destination,
        })
    return {
        "report_version": 2,
        "run_id": record["run_id"],
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "report_type": "count_only_diagnostic",
        "reconciliation_status": combined_status([row["status"] for row in rows]),
        "cutover_ready": False,
        "cutover_readiness": "not_assessed_by_count_reconciliation",
        "scope": "Bronze row counts only; Silver and Gold are not reconciled.",
        "normalized_destination_tables": tables,
        "destination_count_query_performed": destination_counts is not None,
        "counts": rows,
        "limitations": [
            "CSV and SQL source rows are application-counted as yielded during extraction; SQL preflight does not independently count source rows before extraction.",
            "dlt NormalizeInfo row_counts records normalized/prepared items, not an independent source scan or destination write count.",
            "Destination counts are current full-table counts after replacement and can differ if concurrent writers changed tables.",
            "Equal application/prepared/destination counts do not establish a SQL source total or validate row values, keys, schema compatibility, rejects, or business metrics.",
        ],
    }


# --- Local key/relationship reconciliation -------------------------------------------------
# Re-scans the run's local source (CSV directory or SQLite file) for supported retail entities.
# Key values exist only in process memory; the report holds aggregate counts and statuses.


def _check_retail_key_contract() -> None:
    primary_keys = dict(tuple(key.split(".")) for key in _RETAIL_TESTS["unique_not_null"])
    relationships = tuple(
        (*child.split("."), *parent.split("."))
        for child, parent in (item.split(" -> ") for item in _RETAIL_TESTS["relationships"])
    )
    if primary_keys != acceptance_report.RETAIL_PRIMARY_KEYS or relationships != acceptance_report.RETAIL_RELATIONSHIPS:
        raise ValueError("Retail key contract differs between the loader and the acceptance report reader.")
    order = list(primary_keys)
    if any(order.index(parent) >= order.index(child) for child, _, parent, _ in relationships):
        raise ValueError("Retail parents must be scanned before their children.")


class _CsvKeySource:
    kind = "csv"

    def __init__(self, directory: Path, glob: str):
        self.directory = directory
        self.glob = glob
        self._paths = {path.stem.casefold(): path for path in discover_csv_files(directory, glob)}

    def inventory(self, entities: list[str]) -> list[dict[str, object]]:
        return inventory_csv_inputs(self.directory, self.glob, entities, count_rows=False)

    @contextmanager
    def open(self, entity: str):
        path = self._paths.get(entity.casefold())
        if path is None:
            raise ValueError("Local CSV source no longer contains a manifest entity.")
        with open_csv_input(path) as stream:
            reader = _csv_reader(stream)

            def fetch(selected: list[str]):
                for row in _validated_csv_rows(reader):
                    yield tuple(row[name] for name in selected)

            yield list(reader.fieldnames), fetch

    def close(self) -> None:
        pass


class _SqliteKeySource:
    kind = "sql"

    def __init__(self, connection_url: str, schema: str):
        # Local-only by design: server databases (including PostgreSQL) are refused.
        if make_url(connection_url).get_backend_name() != "sqlite":
            raise ValueError("Local key reconciliation supports SQLite files only.")
        self.engine = create_read_only_sql_engine(connection_url)
        self.schema = schema

    def inventory(self, entities: list[str]) -> list[dict[str, object]]:
        return inventory_sql_inputs(self.engine, self.schema, entities)

    @contextmanager
    def open(self, entity: str):
        with self.engine.connect() as connection:
            table = Table(entity, MetaData(), schema=self.schema, autoload_with=connection)

            def fetch(selected: list[str]):
                # pysqlite cursors fetch lazily; only the selected key columns are read.
                for row in connection.execute(select(*(table.c[name] for name in selected))):
                    yield tuple(row)

            yield [column.name for column in table.columns], fetch

    def close(self) -> None:
        self.engine.dispose()


def _resolve_key_column(columns: list[str], rules: list[dict[str, str]], field: str) -> tuple[str, str | None, str | None]:
    """Mirror the loader: a mapping rule targeting the field wins; else one raw normalized match."""
    def normalize(name: str) -> str:
        return _DEFAULT_NAME_NORMALIZER(name).casefold()

    target = normalize(field)
    mapped = [rule for rule in rules if normalize(rule["target"]) == target]
    if len(mapped) > 1:
        return "ambiguous", None, None
    if mapped:
        source = mapped[0]["source"]
        if source not in columns:
            return "missing", None, None
        if any(name != source and normalize(name) == target for name in columns):
            return "ambiguous", None, None
        return "available", source, mapped[0]["cast"]
    raw = [name for name in columns if normalize(name) == target]
    if len(raw) == 1:
        return "available", raw[0], None
    return ("ambiguous" if raw else "missing"), None, None


def _bronze_key(value: object, cast: str | None) -> str | None:
    """Return the in-memory comparison key, or None for NULL/blank per the Silver key policy."""
    if cast is not None:
        value = _cast_mapped_value(value, cast)  # ValueError on cast failure
    if value is None or _is_ascii_space_blank(value):
        return None
    return str(value)


def _scan_retail_keys(source, retail_entities: dict[str, str], mappings: dict[str, list[dict[str, str]]],
                      max_rows: int) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    primary_keys = acceptance_report.RETAIL_PRIMARY_KEYS
    entity_results: dict[str, dict[str, object]] = {}
    relationship_results: dict[str, object] = {}
    parent_keys: dict[str, set[str]] = {}
    for retail, primary_key in primary_keys.items():  # parents precede children
        entity = retail_entities.get(retail)
        if entity is None:
            continue
        rules = mappings.get(entity.casefold(), [])
        child_relationships = [rel for rel in acceptance_report.RETAIL_RELATIONSHIPS if rel[0] == retail]
        with source.open(entity) as (columns, fetch):
            pk_state, pk_column, pk_cast = _resolve_key_column(columns, rules, primary_key)
            checks: list[tuple[str, str, str | None, set[str]]] = []
            for relationship in child_relationships:
                name = acceptance_report.relationship_name(relationship)
                state, column, cast = _resolve_key_column(columns, rules, relationship[1])
                if state != "available":
                    relationship_results[name] = "unavailable_child_key_column_unresolved"
                elif relationship[2] not in retail_entities:
                    relationship_results[name] = "unavailable_entity_not_selected"
                elif relationship[2] not in parent_keys:
                    relationship_results[name] = "unavailable_parent_scan_incomplete"
                else:
                    checks.append((name, column, cast, parent_keys[relationship[2]]))
            if pk_state != "available":
                entity_results[retail] = {"unavailable": f"unavailable_key_column_{pk_state}"}
                for name, *_ in checks:
                    relationship_results[name] = "unavailable_child_scan_incomplete"
                continue
            entity_counts = dict.fromkeys(acceptance_report.KEY_ENTITY_COUNT_FIELDS, 0)
            relationship_counts = {name: dict.fromkeys(acceptance_report.KEY_RELATIONSHIP_COUNT_FIELDS, 0) for name, *_ in checks}
            orphan_keys: dict[str, set[str]] = {name: set() for name, *_ in checks}
            keys: set[str] = set()
            limit_exceeded = False
            for values in fetch([pk_column, *(column for _, column, _, _ in checks)]):
                if entity_counts["scanned_rows"] >= max_rows:
                    limit_exceeded = True
                    break
                entity_counts["scanned_rows"] += 1
                try:
                    key = _bronze_key(values[0], pk_cast)
                except ValueError:
                    entity_counts["key_cast_failures"] += 1
                else:
                    if key is None:
                        entity_counts["blank_or_null_keys"] += 1
                    else:
                        entity_counts["non_blank_keys"] += 1
                        if key in keys:
                            entity_counts["duplicate_key_rows"] += 1
                        else:
                            keys.add(key)
                for position, (name, _, cast, parents) in enumerate(checks, start=1):
                    counts = relationship_counts[name]
                    try:
                        child_key = _bronze_key(values[position], cast)
                    except ValueError:
                        counts["child_key_cast_failures"] += 1
                        continue
                    if child_key is None:
                        counts["blank_or_null_child_keys"] += 1
                        continue
                    counts["child_rows_checked"] += 1
                    if child_key not in parents:
                        counts["orphan_child_rows"] += 1
                        orphan_keys[name].add(child_key)
        if limit_exceeded:
            entity_results[retail] = {"unavailable": "unavailable_scan_limit_exceeded"}
            for name, *_ in checks:
                relationship_results[name] = "unavailable_child_scan_incomplete"
            continue
        entity_counts["distinct_keys"] = len(keys)
        entity_results[retail] = {"counts": entity_counts}
        parent_keys[retail] = keys
        for name, *_ in checks:
            relationship_counts[name]["distinct_orphan_keys"] = len(orphan_keys[name])
            relationship_results[name] = relationship_counts[name]
    return entity_results, relationship_results


def build_key_report(record: dict[str, object], source, mappings: dict[str, list[dict[str, str]]],
                     max_rows: int = _DEFAULT_MAX_KEY_ROWS_PER_TABLE) -> dict[str, object]:
    """Build a count-only local key/relationship report; never contacts a destination."""
    _check_retail_key_contract()
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows <= 0:
        raise ValueError("Key scan row limit must be a positive integer.")
    if record.get("source_kind") != source.kind:
        raise ValueError("Local source kind does not match the run manifest.")
    if mappings_fingerprint(mappings) != record.get("source_mapping_fingerprint"):
        raise ValueError("Mapping fingerprint does not match the run manifest; supply the run's mapping file.")
    entities, tables = record["entities"], record["destination_tables"]
    inventory = source.inventory(entities)
    if any(item.get("status") in {"missing", "no_matching_files"} for item in inventory):
        raise ValueError("Local source no longer contains every manifest entity.")
    binding = {
        "schema_fingerprint": "match" if schema_fingerprint(inventory) == record.get("source_schema_fingerprint") else "mismatch",
        "typed_schema_fingerprint": "match" if typed_schema_fingerprint(inventory) == record.get("typed_schema_fingerprint") else "mismatch",
        "mapping_fingerprint": "match",
    }
    binding["status"] = "bound" if binding["schema_fingerprint"] == binding["typed_schema_fingerprint"] == "match" else "mismatch"

    retail_entities = {
        table: entity for entity, table in zip(entities, tables) if table in acceptance_report.RETAIL_PRIMARY_KEYS
    }
    entity_results, relationship_results = _scan_retail_keys(source, retail_entities, mappings, max_rows)

    source_counts = record.get("source_row_counts")
    source_counts = source_counts if isinstance(source_counts, dict) else {}
    entity_rows = []
    for entity, table in zip(entities, tables):
        retail = table if table in acceptance_report.RETAIL_PRIMARY_KEYS else None
        counts: dict[str, int | None] = dict.fromkeys(acceptance_report.KEY_ENTITY_COUNT_FIELDS)
        unavailable = None
        manifest_rows = None
        if retail is not None:
            result = entity_results[retail]
            unavailable = result.get("unavailable")
            counts = result.get("counts", counts)
            entry = source_counts.get(entity)
            if isinstance(entry, dict) and entry.get("extraction_status", "complete") == "complete":
                rows = entry.get("rows")
                manifest_rows = rows if isinstance(rows, int) and not isinstance(rows, bool) and rows >= 0 else None
        row_count_status = (
            acceptance_report._comparison_status(counts["scanned_rows"], manifest_rows)
            if counts["scanned_rows"] is not None else "count_unavailable"
        )
        entity_rows.append({
            "source_entity": entity,
            "bronze_table": table,
            "retail_entity": retail,
            "primary_key_field": acceptance_report.RETAIL_PRIMARY_KEYS.get(retail),
            "status": acceptance_report.key_entity_status(retail, counts, row_count_status, unavailable),
            "manifest_source_rows": manifest_rows,
            "row_count_vs_manifest": row_count_status,
            **counts,
        })
    relationship_rows = []
    for relationship in acceptance_report.RETAIL_RELATIONSHIPS:
        name = acceptance_report.relationship_name(relationship)
        result = relationship_results.get(name, "unavailable_entity_not_selected")
        counts = result if isinstance(result, dict) else dict.fromkeys(acceptance_report.KEY_RELATIONSHIP_COUNT_FIELDS)
        unavailable = result if isinstance(result, str) else None
        relationship_rows.append({
            "relationship": name,
            "status": acceptance_report.key_relationship_status(counts, unavailable),
            **counts,
        })
    overall = acceptance_report.key_overall_status(
        binding["status"], [row["status"] for row in entity_rows], [row["status"] for row in relationship_rows]
    )
    return {
        "report_version": acceptance_report.KEY_RECONCILIATION_REPORT_VERSION,
        "run_id": record["run_id"],
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "report_type": acceptance_report.KEY_RECONCILIATION_REPORT_TYPE,
        "source_kind": source.kind,
        "key_reconciliation_status": overall,
        "cutover_ready": False,
        "cutover_readiness": "not_assessed_by_key_reconciliation",
        "destination_query_performed": False,
        "scope": "Local source re-scan of supported retail entities (customers, products, orders, order_items): primary-key completeness/uniqueness and modeled relationship orphans. Bronze, Silver, and Gold are not read.",
        "source_binding": binding,
        "key_policy": "Keys are compared as text after any mapped cast. NULL, empty, and ASCII-space-only keys are excluded as blank, matching the Silver filter; other whitespace and padded keys remain exact values.",
        "entities": entity_rows,
        "relationships": relationship_rows,
        "privacy": "No key values, source values, key digests, source column names, paths, or connection details are stored; deterministic aggregate counts can still reveal data characteristics.",
        "limitations": [
            "The local source is re-scanned after the load; matching schema/mapping fingerprints and row counts bind it to the run but do not prove the same bytes or rows were extracted.",
            "This report does not read Bronze, so it does not prove source-to-Bronze key-set equality; combine it with the count reconciliation and dbt data tests.",
            "Relationship checks cover only the modeled retail relationships when both entities were selected and fully scanned.",
            "Python cast and text-comparison semantics are not warehouse-equivalent; BigQuery/Snowflake key stringification is not verified.",
        ],
    }


def _local_key_source(args: argparse.Namespace, record: dict[str, object]):
    if record.get("source_kind") == "csv":
        if args.csv_dir is None:
            raise ValueError("--csv-dir is required for a CSV run.")
        directory = args.csv_dir.expanduser().resolve()
        if not directory.is_dir():
            raise ValueError("--csv-dir must be an existing directory.")
        return _CsvKeySource(directory, args.csv_glob)
    if record.get("source_kind") == "sql":
        identity = record.get("source_identity")
        schema = identity.get("schema") if isinstance(identity, dict) else None
        if not isinstance(schema, str) or not _SAFE_IDENTIFIER.fullmatch(schema):
            raise ValueError("Manifest SQL source schema is invalid.")
        connection_url = os.environ.get(args.source_url_env)
        if not connection_url:
            raise ValueError("SQLite source URL environment variable is unset.")
        return _SqliteKeySource(connection_url, schema)
    raise ValueError("Manifest source kind is invalid.")


def _write_local_key_report(args: argparse.Namespace, record: dict[str, object]) -> int:
    if args.query_destination or args.target_id or args.confirm_target or args.confirm_tables:
        raise ValueError("--local-keys never queries a destination; omit destination query options.")
    mappings = load_mapping_file(args.mapping_file)
    source = _local_key_source(args, record)
    try:
        report = build_key_report(record, source, mappings, args.max_key_rows_per_table)
    finally:
        source.close()
    report_path = MIGRATION_ROOT / "run-manifests" / f"{args.run_id}.key-reconciliation.local.json"
    if report_path.is_symlink():
        raise ValueError("Key reconciliation report path must not be a symlink.")
    ensure_private_directory(report_path.parent)
    create_private_json(report_path.parent, report_path.name, report)
    print(f"Local key reconciliation report written: {report_path.relative_to(MIGRATION_ROOT)}")
    print(f"Key reconciliation status: {report['key_reconciliation_status']}")
    print("Destination query performed: no")
    return 0


def main() -> int:
    args = _parse_args()
    try:
        _, record = _read_completed_manifest(args.run_id)
        if args.local_keys:
            return _write_local_key_report(args, record)
        destination_counts = None
        if args.query_destination:
            tables = record["destination_tables"]
            if args.confirm_tables != ",".join(tables):
                raise ValueError("--confirm-tables must exactly match the normalized table list in the offline report.")
            if not args.confirm_target or args.confirm_target != record.get("target_identity"):
                raise ValueError("--confirm-target must exactly match the identity recorded in the run manifest.")
            destination = record.get("destination")
            dataset = record.get("bronze_namespace")
            pipeline_name = record.get("pipeline_name")
            if destination in {"bigquery", "snowflake"} and not args.target_id:
                raise ValueError("--target-id is required for a cloud destination query.")
            pipelines_root = MIGRATION_ROOT / ".dlt" / "pipelines"
            if pipelines_root.is_symlink():
                raise ValueError("Migration pipeline state directory must not be a symlink.")
            pipeline = _attach_pipeline(pipeline_name, destination, dataset, pipelines_root)
            if pipeline.has_pending_data:
                raise ValueError("Pipeline has pending packages; refusing a potentially misleading destination count.")
            _, _, identity, namespace = _resolve_dlt_destination(
                pipeline, destination, args.target_id, dataset
            )
            if identity != record.get("target_identity") or namespace.casefold() != str(dataset).casefold():
                raise ValueError("Resolved destination does not match the completed run manifest.")
            destination_counts = _destination_counts(pipeline, tables)

        report = build_report(record, destination_counts)
        report_mode = "queried" if destination_counts is not None else "offline"
        report_path = MIGRATION_ROOT / "run-manifests" / f"{args.run_id}.bronze-reconciliation.{report_mode}.json"
        if report_path.is_symlink():
            raise ValueError("Reconciliation report path must not be a symlink.")
        ensure_private_directory(report_path.parent)
        create_private_json(report_path.parent, report_path.name, report)
        print(f"Bronze reconciliation report written: {report_path.relative_to(MIGRATION_ROOT)}")
        print(f"Destination count query performed: {'yes' if destination_counts is not None else 'no'}")
        print(f"Normalized table confirmation (--confirm-tables): {','.join(record['destination_tables'])}")
        return 0
    except Exception as error:
        # Driver errors may embed credentials, SQL, identifiers, or paths.
        print(f"Reconciliation stopped ({type(error).__name__}). Review local configuration privately.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
