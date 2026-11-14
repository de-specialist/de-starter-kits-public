#!/usr/bin/env python3
"""Create a private, allowlisted acceptance summary from existing migration evidence."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import evidence_index
from _private_json import create_private_json, read_private_json


ROOT = Path(__file__).resolve().parent
MANIFEST_DIR = ROOT / "run-manifests"
_RUN_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REJECT_REASONS = {
    "extraction_budget_exceeded", "serialization_error", "mapping_cast_failed",
    "source_schema_changed_since_preflight", "unsafe_actual_csv_schema",
    "malformed_or_unreadable_csv", "empty_source_not_authorized",
    # Destination numeric type-profile codes; kept equal to destination_type_profiles.REASON_CODES by tests.
    "integer_out_of_destination_range", "decimal_integer_digits_exceed_destination_profile",
    "decimal_scale_exceeds_destination_profile", "destination_bound_value_unsupported",
}
_COUNT_PROVENANCE = {
    "application_counted_canonical_json_payload",
    "rows_yielded_from_csv_resource",
    "not_independently_counted_sql_source",
}
_SCHEMA_STATUSES = {"preflight_only", "matched_at_extraction", "mismatch"}


def _nonnegative_int(value: object, label: str, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Invalid nonnegative integer in {label}.")
    return value


def _comparison_status(left: int | None, right: int | None) -> str:
    if left is None or right is None:
        return "count_unavailable"
    return "count_match" if left == right else "count_mismatch"


def _combined_status(statuses: list[str]) -> str:
    if "count_mismatch" in statuses:
        return "count_mismatch"
    if "count_unavailable" in statuses:
        return "count_unavailable"
    return "count_match"


# Local key/relationship reconciliation contract. reconcile_bronze.py writes reports using
# these rules and this module re-derives every status from the reported counts on read.
KEY_RECONCILIATION_REPORT_VERSION = 1
KEY_RECONCILIATION_REPORT_TYPE = "local_source_key_relationship_diagnostic"
# Mirrors run_migration._RETAIL_TESTS (reconcile_bronze.py asserts they stay equal) so this
# reader does not import the loader and its dlt runtime configuration side effects.
RETAIL_PRIMARY_KEYS = {
    "customers": "customer_id",
    "products": "product_id",
    "orders": "order_id",
    "order_items": "order_item_id",
}
RETAIL_RELATIONSHIPS = (
    ("orders", "customer_id", "customers", "customer_id"),
    ("order_items", "order_id", "orders", "order_id"),
    ("order_items", "product_id", "products", "product_id"),
)
KEY_ENTITY_UNAVAILABLE = {
    "unavailable_key_column_missing",
    "unavailable_key_column_ambiguous",
    "unavailable_scan_limit_exceeded",
}
KEY_RELATIONSHIP_UNAVAILABLE = {
    "unavailable_entity_not_selected",
    "unavailable_child_key_column_unresolved",
    "unavailable_child_scan_incomplete",
    "unavailable_parent_scan_incomplete",
}
KEY_ENTITY_COUNT_FIELDS = (
    "scanned_rows", "non_blank_keys", "blank_or_null_keys", "distinct_keys", "duplicate_key_rows", "key_cast_failures",
)
KEY_RELATIONSHIP_COUNT_FIELDS = (
    "child_rows_checked", "blank_or_null_child_keys", "child_key_cast_failures", "orphan_child_rows", "distinct_orphan_keys",
)
_KEY_ENTITY_SCANNED = {"key_checks_passed", "key_issues_found", "key_evidence_unavailable"}


def relationship_name(relationship: tuple[str, str, str, str]) -> str:
    child, child_key, parent, parent_key = relationship
    return f"{child}.{child_key} -> {parent}.{parent_key}"


def key_entity_status(retail_entity: str | None, counts: dict[str, int | None],
                      row_count_vs_manifest: str, unavailable_reason: str | None) -> str:
    if retail_entity is None:
        return "not_supported_retail_entity"
    if unavailable_reason is not None:
        return unavailable_reason
    if (counts["blank_or_null_keys"] or counts["duplicate_key_rows"] or counts["key_cast_failures"]
            or row_count_vs_manifest == "count_mismatch"):
        return "key_issues_found"
    if row_count_vs_manifest != "count_match":
        return "key_evidence_unavailable"
    return "key_checks_passed"


def key_relationship_status(counts: dict[str, int | None], unavailable_reason: str | None) -> str:
    if unavailable_reason is not None:
        return unavailable_reason
    if counts["orphan_child_rows"] or counts["child_key_cast_failures"]:
        return "relationship_issues_found"
    return "relationship_checks_passed"


def key_overall_status(binding_status: str, entity_statuses: list[str], relationship_statuses: list[str]) -> str:
    if binding_status != "bound":
        return "source_binding_mismatch"
    retail_statuses = [status for status in entity_statuses if status != "not_supported_retail_entity"]
    statuses = retail_statuses + relationship_statuses
    if any(status in {"key_issues_found", "relationship_issues_found"} for status in statuses):
        return "key_issues_found"
    if not retail_statuses or any(
        status == "key_evidence_unavailable" or status.startswith("unavailable_") for status in statuses
    ):
        return "key_evidence_unavailable"
    return "key_checks_passed"


def _read_key_reconciliation(
    run_id: str,
    filename: str,
    *,
    expected_entities: list[str],
    expected_tables: list[str],
    expected_source_counts: dict[str, dict[str, object]],
) -> dict[str, object]:
    """Accept only allowlisted aggregate fields whose statuses re-derive from their counts."""
    record = read_private_json(MANIFEST_DIR, filename)
    if record.get("report_version") != KEY_RECONCILIATION_REPORT_VERSION or record.get("run_id") != run_id:
        raise ValueError("Key reconciliation report does not match this run.")
    if record.get("report_type") != KEY_RECONCILIATION_REPORT_TYPE or record.get("cutover_ready") is not False:
        raise ValueError("Key reconciliation report has an unsupported format.")
    if record.get("destination_query_performed") is not False:
        raise ValueError("Key reconciliation report must be local-only.")
    binding = record.get("source_binding")
    if not isinstance(binding, dict) or binding.get("mapping_fingerprint") != "match":
        raise ValueError("Key reconciliation source binding is malformed.")
    safe_binding = {"mapping_fingerprint": "match"}
    for field in ("schema_fingerprint", "typed_schema_fingerprint"):
        if binding.get(field) not in {"match", "mismatch"}:
            raise ValueError("Key reconciliation source binding is malformed.")
        safe_binding[field] = binding[field]
    binding_status = "bound" if safe_binding["schema_fingerprint"] == safe_binding["typed_schema_fingerprint"] == "match" else "mismatch"
    if binding.get("status") != binding_status:
        raise ValueError("Key reconciliation binding status contradicts its fingerprint checks.")
    safe_binding["status"] = binding_status

    rows = record.get("entities")
    if not isinstance(rows, list) or len(rows) != len(expected_entities) or len(expected_tables) != len(expected_entities):
        raise ValueError("Key reconciliation entity entries are malformed.")
    safe_rows: list[dict[str, object]] = []
    entity_by_retail: dict[str, dict[str, object]] = {}
    for index, row in enumerate(rows):
        entity, table = expected_entities[index], expected_tables[index]
        if not isinstance(row, dict) or row.get("source_entity") != entity or row.get("bronze_table") != table:
            raise ValueError("Key reconciliation entity entry does not match the run manifest.")
        retail = table if table in RETAIL_PRIMARY_KEYS else None
        if row.get("retail_entity") != retail or row.get("primary_key_field") != RETAIL_PRIMARY_KEYS.get(retail):
            raise ValueError("Key reconciliation retail entity mapping is malformed.")
        counts = {field: _nonnegative_int(row.get(field), f"entities.{field}", optional=True)
                  for field in KEY_ENTITY_COUNT_FIELDS}
        manifest_rows = _nonnegative_int(row.get("manifest_source_rows"), "entities.manifest_source_rows", optional=True)
        source = expected_source_counts.get(entity)
        expected_manifest_rows = (
            source["rows"] if retail is not None and source is not None and source["extraction_status"] == "complete"
            else None
        )
        if manifest_rows != expected_manifest_rows:
            raise ValueError("Key reconciliation manifest row count contradicts the run manifest.")
        status = row.get("status")
        unavailable = status if retail is not None and status in KEY_ENTITY_UNAVAILABLE else None
        if retail is None or unavailable is not None:
            if any(value is not None for value in counts.values()) or row.get("row_count_vs_manifest") != "count_unavailable":
                raise ValueError("Key reconciliation reports counts for an unscanned entity.")
            row_count_status = "count_unavailable"
        else:
            if any(value is None for value in counts.values()):
                raise ValueError("Key reconciliation scanned entity is missing counts.")
            if (counts["non_blank_keys"] + counts["blank_or_null_keys"] + counts["key_cast_failures"] != counts["scanned_rows"]
                    or counts["distinct_keys"] + counts["duplicate_key_rows"] != counts["non_blank_keys"]):
                raise ValueError("Key reconciliation entity counts are internally inconsistent.")
            row_count_status = _comparison_status(counts["scanned_rows"], manifest_rows)
            if row.get("row_count_vs_manifest") != row_count_status:
                raise ValueError("Key reconciliation row-count status contradicts its counts.")
        if status != key_entity_status(retail, counts, row_count_status, unavailable):
            raise ValueError("Key reconciliation entity status contradicts its counts.")
        safe_row = {
            "source_entity": entity,
            "bronze_table": table,
            "retail_entity": retail,
            "primary_key_field": RETAIL_PRIMARY_KEYS.get(retail),
            "status": status,
            "manifest_source_rows": manifest_rows,
            "row_count_vs_manifest": row_count_status,
            **counts,
        }
        safe_rows.append(safe_row)
        if retail is not None:
            entity_by_retail[retail] = safe_row

    relationships = record.get("relationships")
    if not isinstance(relationships, list) or len(relationships) != len(RETAIL_RELATIONSHIPS):
        raise ValueError("Key reconciliation relationship entries are malformed.")
    safe_relationships: list[dict[str, object]] = []
    for definition, row in zip(RETAIL_RELATIONSHIPS, relationships):
        name = relationship_name(definition)
        if not isinstance(row, dict) or row.get("relationship") != name:
            raise ValueError("Key reconciliation relationship entry is not a supported retail relationship.")
        counts = {field: _nonnegative_int(row.get(field), f"relationships.{field}", optional=True)
                  for field in KEY_RELATIONSHIP_COUNT_FIELDS}
        status = row.get("status")
        unavailable = status if status in KEY_RELATIONSHIP_UNAVAILABLE else None
        child, parent = entity_by_retail.get(definition[0]), entity_by_retail.get(definition[2])
        if unavailable is not None:
            if any(value is not None for value in counts.values()):
                raise ValueError("Key reconciliation reports counts for an unavailable relationship.")
        else:
            if any(value is None for value in counts.values()):
                raise ValueError("Key reconciliation relationship is missing counts.")
            if child is None or parent is None or child["status"] not in _KEY_ENTITY_SCANNED or parent["status"] not in _KEY_ENTITY_SCANNED:
                raise ValueError("Key reconciliation relationship lacks fully scanned entities.")
            if (counts["child_rows_checked"] + counts["blank_or_null_child_keys"] + counts["child_key_cast_failures"]
                    != child["scanned_rows"]
                    or not counts["distinct_orphan_keys"] <= counts["orphan_child_rows"] <= counts["child_rows_checked"]
                    or (counts["orphan_child_rows"] > 0) != (counts["distinct_orphan_keys"] > 0)):
                raise ValueError("Key reconciliation relationship counts are internally inconsistent.")
        if status != key_relationship_status(counts, unavailable):
            raise ValueError("Key reconciliation relationship status contradicts its counts.")
        safe_relationships.append({"relationship": name, "status": status, **counts})

    overall = key_overall_status(
        binding_status, [row["status"] for row in safe_rows], [row["status"] for row in safe_relationships]
    )
    if record.get("key_reconciliation_status") != overall:
        raise ValueError("Overall key reconciliation status contradicts its entries.")
    return {
        "mode": "local",
        "report_type": KEY_RECONCILIATION_REPORT_TYPE,
        "status": overall,
        "destination_query_performed": False,
        "cutover_ready": False,
        "source_binding": safe_binding,
        "entities": safe_rows,
        "relationships": safe_relationships,
    }


def _read_reconciliation(
    run_id: str,
    filename: str,
    *,
    queried: bool,
    expected_entities: list[str],
    expected_tables: list[str],
    expected_source_counts: dict[str, dict[str, object]],
) -> dict[str, object]:
    record = read_private_json(MANIFEST_DIR, filename)
    if record.get("report_version") != 2 or record.get("run_id") != run_id:
        raise ValueError("Reconciliation report does not match this run.")
    if record.get("report_type") != "count_only_diagnostic" or record.get("cutover_ready") is not False:
        raise ValueError("Reconciliation report has an unsupported format.")
    if record.get("destination_count_query_performed") is not queried:
        raise ValueError("Reconciliation mode does not match its filename.")
    status = record.get("reconciliation_status")
    if status not in {"count_match", "count_mismatch", "count_unavailable"}:
        raise ValueError("Reconciliation status is invalid.")
    tables = record.get("normalized_destination_tables")
    rows = record.get("counts")
    if not isinstance(tables, list) or not tables or tables != expected_tables or not all(
        isinstance(table, str) and evidence_index._SAFE_IDENTIFIER.fullmatch(table) for table in tables
    ) or len({table.casefold() for table in tables}) != len(tables):
        raise ValueError("Reconciliation table list is malformed.")
    if not isinstance(rows, list) or len(rows) != len(tables) or len(expected_entities) != len(tables):
        raise ValueError("Reconciliation count entries are malformed.")
    safe_rows = []
    derived_statuses = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("bronze_table") != tables[index]:
            raise ValueError("Reconciliation count entry does not match its table list.")
        entity = row.get("source_entity")
        if entity != expected_entities[index] or not evidence_index._SAFE_IDENTIFIER.fullmatch(entity):
            raise ValueError("Reconciliation source entity is malformed.")
        row_status = row.get("status")
        if row_status not in {"count_match", "count_mismatch", "count_unavailable"}:
            raise ValueError("Reconciliation table status is invalid.")
        values = {}
        for field in ("source_rows", "dlt_prepared_rows", "current_destination_rows"):
            values[field] = _nonnegative_int(row.get(field), f"counts.{field}", optional=True)
        derived_status = _combined_status([
            _comparison_status(values["source_rows"], values["dlt_prepared_rows"]),
            _comparison_status(values["dlt_prepared_rows"], values["current_destination_rows"]),
        ])
        if row_status != derived_status:
            raise ValueError("Reconciliation row status contradicts its counts.")
        derived_statuses.append(derived_status)
        source_provenance = row.get("source_count_provenance")
        expected_source = expected_source_counts.get(entity)
        if expected_source is None or expected_source["extraction_status"] == "unverified_legacy":
            if values["source_rows"] is not None or source_provenance is not None:
                raise ValueError("Reconciliation asserts source evidence absent from the run manifest.")
        else:
            if source_provenance not in _COUNT_PROVENANCE:
                raise ValueError("Reconciliation source-count provenance is invalid.")
            if values["source_rows"] != expected_source["rows"]:
                raise ValueError("Reconciliation source count contradicts the run manifest.")
            if source_provenance != expected_source["provenance"]:
                raise ValueError("Reconciliation source-count provenance contradicts the run manifest.")
        prepared_provenance = row.get("dlt_prepared_count_provenance")
        if prepared_provenance not in {None, "dlt_1_32_0_last_normalize_info_row_counts"}:
            raise ValueError("Reconciliation prepared-count provenance is invalid.")
        safe_rows.append({
            "source_entity": entity,
            "bronze_table": tables[index],
            "status": row_status,
            "source_count_provenance": source_provenance,
            "dlt_prepared_count_provenance": prepared_provenance,
            **values,
        })
    if status != _combined_status(derived_statuses):
        raise ValueError("Overall reconciliation status contradicts its table counts.")
    return {
        "mode": "queried" if queried else "offline",
        "report_type": "count_only_diagnostic",
        "status": status,
        "destination_count_query_performed": queried,
        "cutover_ready": False,
        "counts": safe_rows,
    }


def _rejection_summary(record: dict[str, object], entities: list[str]) -> dict[str, object]:
    summary = record.get("extraction_reject_summary")
    if summary is None:
        return {"status": "none_recorded", "entries": []}
    if not isinstance(summary, dict):
        raise ValueError("Extraction reject summary is malformed.")
    table = summary.get("table")
    reason = summary.get("reason_code")
    if table not in entities or reason not in _REJECT_REASONS:
        raise ValueError("Extraction reject summary contains an unsupported identifier.")
    accepted_rows = _nonnegative_int(summary.get("accepted_rows_total"), "reject.accepted_rows_total")
    accepted_bytes = _nonnegative_int(summary.get("accepted_bytes_total"), "reject.accepted_bytes_total")
    table_rows = _nonnegative_int(summary.get("accepted_table_rows"), "reject.accepted_table_rows")
    table_bytes = _nonnegative_int(summary.get("accepted_table_bytes"), "reject.accepted_table_bytes")
    return {
        "status": "rejection_recorded",
        "entries": [{
            "entity": table,
            "reason_code": reason,
            "accepted_rows_in_entity": table_rows,
            "accepted_bytes_in_entity": table_bytes,
            "accepted_rows_total": accepted_rows,
            "accepted_bytes_total": accepted_bytes,
        }],
    }


def build_report(record: dict[str, object], validated: dict[str, object], reconciliation: dict[str, object] | None,
                 key_reconciliation: dict[str, object] | None = None) -> dict[str, object]:
    entities = validated["entities"]
    source_counts = validated["source_row_counts"]
    inventory = []
    for entity in entities:
        count = source_counts.get(entity, {
            "rows": None,
            "payload_bytes": None,
            "extraction_status": "unverified_legacy",
            "provenance": "not_recorded",
        })
        inventory.append({
            "entity": entity,
            "rows": count["rows"],
            "payload_bytes": count["payload_bytes"],
            "extraction_status": count["extraction_status"],
            "count_provenance": count["provenance"],
        })
    mapping_recorded = "source_mapping_rule_count" in record
    raw_mapping_count = record.get("source_mapping_rule_count")
    mapping_count = _nonnegative_int(raw_mapping_count, "source_mapping_rule_count") if mapping_recorded else None
    mapping_fingerprint = record.get("source_mapping_fingerprint")
    if not isinstance(mapping_fingerprint, str) or not _SHA256.fullmatch(mapping_fingerprint):
        raise ValueError("Source mapping fingerprint is malformed.")
    schema_statuses = {}
    for field in ("source_schema_fingerprint_status", "typed_schema_fingerprint_status"):
        value = record.get(field, "not_recorded")
        if value != "not_recorded" and value not in _SCHEMA_STATUSES:
            raise ValueError(f"Manifest field {field} has an unsupported status.")
        schema_statuses[field] = value
    rejects = _rejection_summary(record, entities)
    if validated["status"] == "completed":
        destination_verification = {
            "load_status": "completed",
            "load_ids_recorded": bool(validated["load_ids"]),
            "load_package_count": validated["load_package_count"],
            "load_job_counts": validated["load_job_counts"],
            "load_evidence_incomplete": validated["load_evidence_incomplete"],
            "destination_write_may_be_partial": validated["destination_write_may_be_partial"],
        }
    else:
        destination_verification = {
            "load_status": "failed",
            "load_ids_recorded": bool(validated["load_ids"]),
            "load_package_count": validated["load_package_count"],
            "load_job_counts": validated["load_job_counts"],
            "load_evidence_incomplete": validated["load_evidence_incomplete"],
            "destination_write_may_be_partial": validated["destination_write_may_be_partial"],
        }
    return {
        "acceptance_report_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_id": validated["run_id"],
        "run_status": validated["status"],
        "source_inventory": {
            "source_kind": validated["source_kind"],
            "selected_entities": entities,
            "entity_destination_tables": [
                {"source_entity": entity, "bronze_table": table}
                for entity, table in zip(entities, record["destination_tables"])
            ],
            "entity_counts": inventory,
            "source_schema_fingerprint": validated["source_schema_fingerprint"],
            "typed_schema_fingerprint": validated["typed_schema_fingerprint"],
            "observed_source_schema_status": schema_statuses["source_schema_fingerprint_status"],
            "observed_typed_schema_status": schema_statuses["typed_schema_fingerprint_status"],
        },
        "mapping_review": {
            "rule_count": mapping_count,
            "rule_count_status": "recorded" if mapping_recorded else "not_recorded",
            "mapping_fingerprint": mapping_fingerprint,
            "semantic_review_status": "not_included_in_manifest",
        },
        "rejects": rejects,
        "reconciliation": reconciliation or {"status": "not_run", "cutover_ready": False},
        "key_relationship_reconciliation": key_reconciliation or {"status": "not_run", "cutover_ready": False},
        "destination_verification": destination_verification,
        "cutover_ready": False,
        "limitations": [
            "This report summarizes recorded evidence; it does not independently verify source rows or destination contents.",
            "Mapping fingerprints and rule counts do not include the mapping rules or prove that a human reviewed them.",
            "Reconciliation, when present, covers Bronze counts only; it does not establish row, key, schema, reject, Silver, Gold, or business-metric equivalence.",
            "Key/relationship reconciliation, when present, re-scans the local source only: it reports aggregate key and orphan counts for supported retail entities, never reads Bronze, and does not prove source-to-Bronze key-set equality.",
            "A completed load manifest does not establish dbt test success, cloud parity, cost suitability, or cutover approval.",
            "The report omits target identities, source paths, connection details, credentials, and row values.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="Canonical UUID from a migration run manifest.")
    parser.add_argument(
        "--reconciliation", choices=("auto", "none", "offline", "queried"), default="auto",
        help="Use an existing private reconciliation report; never initiates a destination query (default: auto).",
    )
    parser.add_argument(
        "--key-reconciliation", choices=("auto", "none", "local"), default="auto",
        help="Use an existing private local key/relationship report; never scans a source (default: auto).",
    )
    args = parser.parse_args()
    try:
        if not _RUN_ID.fullmatch(args.run_id):
            raise ValueError("Run ID must be a canonical UUID.")
        filename = f"{args.run_id}.json"
        raw_manifest = read_private_json(MANIFEST_DIR, filename)
        validated = evidence_index._validate_manifest(raw_manifest, args.run_id)
        tables = raw_manifest.get("destination_tables")
        if not isinstance(tables, list) or len(tables) != len(validated["entities"]) or not all(
            isinstance(table, str) and evidence_index._SAFE_IDENTIFIER.fullmatch(table)
            and table == table.casefold() for table in tables
        ) or len({table.casefold() for table in tables}) != len(tables):
            raise ValueError("Manifest destination table list is malformed.")
        reconciliation = None
        modes = ("queried", "offline") if args.reconciliation == "auto" else (args.reconciliation,)
        for mode in modes:
            if mode == "none":
                break
            report_name = f"{args.run_id}.bronze-reconciliation.{mode}.json"
            try:
                reconciliation = _read_reconciliation(
                    args.run_id,
                    report_name,
                    queried=mode == "queried",
                    expected_entities=validated["entities"],
                    expected_tables=tables,
                    expected_source_counts=validated["source_row_counts"],
                )
                break
            except FileNotFoundError:
                if args.reconciliation != "auto":
                    raise
        key_reconciliation = None
        if args.key_reconciliation != "none":
            try:
                key_reconciliation = _read_key_reconciliation(
                    args.run_id,
                    f"{args.run_id}.key-reconciliation.local.json",
                    expected_entities=validated["entities"],
                    expected_tables=tables,
                    expected_source_counts=validated["source_row_counts"],
                )
            except FileNotFoundError:
                if args.key_reconciliation != "auto":
                    raise
        output_name = f"acceptance-report-{args.run_id}.json"
        report = build_report(raw_manifest, validated, reconciliation, key_reconciliation)
        create_private_json(MANIFEST_DIR, output_name, report)
        print(f"Private migration acceptance report created: {output_name}")
        return 0
    except Exception as error:
        print(f"Acceptance report stopped ({type(error).__name__}); no existing file was overwritten.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
