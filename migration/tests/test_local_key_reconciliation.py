"""Offline tests for privacy-safe local key/relationship reconciliation evidence.

All fixtures are synthetic. Nothing here connects to BigQuery, Snowflake, or any server
database: the manifests name a cloud destination only to prove that --local-keys never
touches it.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "migration"))
try:
    reconcile_bronze = importlib.import_module("reconcile_bronze")
    acceptance_report = importlib.import_module("acceptance_report")
    from _private_json import create_private_json, read_private_json
except ModuleNotFoundError as error:
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name not in {"dlt", "sqlalchemy"}:
        raise
    reconcile_bronze = None


RUN_ID = "123e4567-e89b-42d3-a456-426614174000"
NAMESPACE = "starterkit_migration_test"
PIPELINE = f"fixture_bigquery_{NAMESPACE}_0123456789abcdef01234567"
KEY_REPORT = f"{RUN_ID}.key-reconciliation.local.json"

# Every synthetic key value carries this marker so tests can prove none reach a report.
CLEAN_CSV = {
    "customers": "customer_id,created_at,country\nSYNTH_C1,2025-01-01T00:00:00Z,AU\nSYNTH_C2,2025-01-02T00:00:00Z,NZ\n",
    "products": "product_id,product_name,category\nSYNTH_P1,Tote,Bags\nSYNTH_P2,Mug,Home\n",
    "orders": (
        "order_id,customer_id,order_status,order_date,updated_at\n"
        "SYNTH_O1,SYNTH_C1,complete,2025-02-01,2025-02-01T00:00:00Z\n"
        "SYNTH_O2,SYNTH_C2,complete,2025-02-02,2025-02-02T00:00:00Z\n"
    ),
    "order_items": (
        "order_item_id,order_id,product_id,quantity,unit_price\n"
        "SYNTH_I1,SYNTH_O1,SYNTH_P1,1,2.00\n"
        "SYNTH_I2,SYNTH_O2,SYNTH_P2,1,3.00\n"
        "SYNTH_I3,SYNTH_O2,SYNTH_P1,2,2.00\n"
    ),
}


@unittest.skipIf(reconcile_bronze is None, "migration runtime dependencies are not installed")
class LocalKeyReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest_dir = self.root / "run-manifests"
        reconcile_bronze.ensure_private_directory(self.manifest_dir)
        self.csv_dir = self.root / "source"
        self.csv_dir.mkdir()
        for target, attribute, value in (
            (reconcile_bronze, "MIGRATION_ROOT", self.root),
            (acceptance_report, "MANIFEST_DIR", self.manifest_dir),
        ):
            patcher = mock.patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    # --- fixtures -------------------------------------------------------------------------

    def _write_csvs(self, files: dict[str, str]) -> list[str]:
        for name, text in files.items():
            (self.csv_dir / f"{name}.csv").write_text(text, encoding="utf-8")
        return sorted(files)

    def _write_manifest(self, source_kind: str, entities: list[str], inventory: list[dict[str, object]],
                        row_counts: dict[str, int], *, mapping_fingerprint: str | None = None,
                        schema_fingerprint: str | None = None) -> dict[str, object]:
        provenance = "application_counted_canonical_json_payload"
        if source_kind == "csv":
            identity = {
                "kind": "csv",
                "files": [{"entity": entity, "file_name": f"{entity}.csv", "size_bytes": 1} for entity in entities],
                "identity_scope": "pre_extraction_file_metadata",
            }
        else:
            identity = {
                "kind": "sql", "schema": "main", "tables": sorted(entities),
                "connection_identity": "redacted", "identity_scope": "descriptive_names_not_unique",
            }
        record = {
            "run_id": RUN_ID,
            "pipeline_name": PIPELINE,
            "source_kind": source_kind,
            "entities": entities,
            "destination_tables": [entity.casefold() for entity in entities],
            "source_identity": identity,
            "destination": "bigquery",
            "target_identity": f"synthetic-project/{NAMESPACE}",
            "bronze_namespace": NAMESPACE,
            "write_disposition": "replace",
            "source_row_counts": {
                entity: {"rows": row_counts[entity], "payload_bytes": 10 * row_counts[entity],
                         "extraction_status": "complete", "provenance": provenance}
                for entity in entities
            },
            "accepted_extraction_progress": {
                entity: {"rows": row_counts[entity], "payload_bytes": 10 * row_counts[entity]} for entity in entities
            },
            "dlt_prepared_row_counts": {
                entity.casefold(): {"rows": row_counts[entity], "provenance": "dlt_1_32_0_last_normalize_info_row_counts"}
                for entity in entities
            },
            "source_schema_fingerprint": schema_fingerprint or reconcile_bronze.schema_fingerprint(inventory),
            "typed_schema_fingerprint": reconcile_bronze.typed_schema_fingerprint(inventory),
            "source_mapping_fingerprint": mapping_fingerprint or reconcile_bronze.mappings_fingerprint({}),
            "source_mapping_rule_count": 0,
            "source_schema_fingerprint_status": "matched_at_extraction",
            "typed_schema_fingerprint_status": "matched_at_extraction",
            "status": "completed",
            "destination_write_may_be_partial": False,
            "load_ids": ["load.1"],
            "load_package_count": 1,
            "load_job_counts": {"completed_jobs": len(entities)},
        }
        create_private_json(self.manifest_dir, f"{RUN_ID}.json", record)
        return record

    def _csv_run(self, files: dict[str, str], **manifest_options) -> dict[str, object]:
        entities = self._write_csvs(files)
        inventory = reconcile_bronze.inventory_csv_inputs(self.csv_dir, "*.csv", entities, count_rows=False)
        counts = {name: text.count("\n") - 1 for name, text in files.items()}
        counts.update(manifest_options.pop("row_counts", {}))
        return self._write_manifest("csv", entities, inventory, counts, **manifest_options)

    def _reconcile(self, *arguments: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["reconcile_bronze.py", "--run-id", RUN_ID, "--local-keys", *arguments]
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "argv", argv))
            stack.enter_context(mock.patch.dict(os.environ, env or {}))
            self.attach = stack.enter_context(mock.patch.object(reconcile_bronze, "_attach_pipeline"))
            self.resolve = stack.enter_context(mock.patch.object(reconcile_bronze, "_resolve_dlt_destination"))
            self.counts = stack.enter_context(mock.patch.object(reconcile_bronze, "_destination_counts"))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            code = reconcile_bronze.main()
        for destination_call in (self.attach, self.resolve, self.counts):
            destination_call.assert_not_called()
        return code, stdout.getvalue(), stderr.getvalue()

    def _key_report(self) -> dict[str, object]:
        path = self.manifest_dir / KEY_REPORT
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        return read_private_json(self.manifest_dir, KEY_REPORT)

    def _acceptance(self, *arguments: str) -> tuple[int, dict[str, object] | None, str]:
        stderr = io.StringIO()
        argv = ["acceptance_report.py", RUN_ID, "--reconciliation", "none", *arguments]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(stderr):
            code = acceptance_report.main()
        name = f"acceptance-report-{RUN_ID}.json"
        artifact = read_private_json(self.manifest_dir, name) if (self.manifest_dir / name).exists() else None
        return code, artifact, stderr.getvalue()

    def _replace_key_report(self, report: dict[str, object]) -> None:
        (self.manifest_dir / KEY_REPORT).unlink()
        create_private_json(self.manifest_dir, KEY_REPORT, report)

    @staticmethod
    def _by(items: list[dict[str, object]], field: str) -> dict[str, dict[str, object]]:
        return {item[field]: item for item in items}

    # --- tests ----------------------------------------------------------------------------

    def test_retail_key_contract_matches_loader(self) -> None:
        reconcile_bronze._check_retail_key_contract()

    def test_clean_csv_source_passes_without_emitting_keys_or_querying_destination(self) -> None:
        files = dict(CLEAN_CSV)
        files["orders"] = files["orders"].replace("complete", "SENSITIVE_ORDER_STATUS_XYZ")
        self._csv_run(files)
        code, stdout, stderr = self._reconcile("--csv-dir", str(self.csv_dir))
        self.assertEqual(code, 0, stderr)
        self.assertIn("Destination query performed: no", stdout)
        report = self._key_report()
        self.assertEqual(report["key_reconciliation_status"], "key_checks_passed")
        self.assertIs(report["cutover_ready"], False)
        self.assertIs(report["destination_query_performed"], False)
        self.assertEqual(report["source_binding"]["status"], "bound")
        entities = self._by(report["entities"], "source_entity")
        self.assertEqual(entities["order_items"]["scanned_rows"], 3)
        self.assertEqual(entities["order_items"]["row_count_vs_manifest"], "count_match")
        self.assertEqual({row["status"] for row in report["relationships"]}, {"relationship_checks_passed"})
        self.assertEqual(
            set(report),
            {
                "report_version", "run_id", "generated_at_utc", "report_type", "source_kind",
                "key_reconciliation_status", "cutover_ready", "cutover_readiness",
                "destination_query_performed", "scope", "source_binding", "key_policy",
                "entities", "relationships", "privacy", "limitations",
            },
        )
        self.assertEqual(
            set(entities["customers"]),
            {
                "source_entity", "bronze_table", "retail_entity", "primary_key_field", "status",
                "manifest_source_rows", "row_count_vs_manifest", *acceptance_report.KEY_ENTITY_COUNT_FIELDS,
            },
        )
        self.assertEqual(
            set(report["relationships"][0]),
            {"relationship", "status", *acceptance_report.KEY_RELATIONSHIP_COUNT_FIELDS},
        )
        serialized = json.dumps(report)
        self.assertNotIn("SYNTH_", serialized)
        self.assertNotIn(str(self.csv_dir), serialized)
        for raw_value in (
            "Tote", "Bags", "2025-01-01", "2025-02-01", "SENSITIVE_ORDER_STATUS_XYZ", "2.00",
        ):
            self.assertNotIn(raw_value, serialized)

    def test_duplicate_blank_and_orphan_keys_are_counted_not_emitted(self) -> None:
        files = dict(CLEAN_CSV)
        files["customers"] = "customer_id,created_at,country\nSYNTH_C1,x,AU\nSYNTH_C1,x,AU\n   ,x,AU\n"
        files["orders"] = (
            "order_id,customer_id,order_status,order_date,updated_at\n"
            "SYNTH_O1,SYNTH_C1,complete,2025-02-01,x\n"
            "SYNTH_O2,SYNTH_GHOST_CUSTOMER,complete,2025-02-01,x\n"
            "SYNTH_O3,,complete,2025-02-01,x\n"
        )
        files["order_items"] = (
            "order_item_id,order_id,product_id,quantity,unit_price\n"
            "SYNTH_I1,SYNTH_O1,SYNTH_P1,1,1\n"
            "SYNTH_I2,SYNTH_GHOST_ORDER,SYNTH_P1,1,1\n"
            "SYNTH_I3,SYNTH_GHOST_ORDER,SYNTH_P2,1,1\n"
        )
        files["notes"] = "note_id,body\nSYNTH_N1,free text\n"
        self._csv_run(files)
        code, _, stderr = self._reconcile("--csv-dir", str(self.csv_dir))
        self.assertEqual(code, 0, stderr)
        report = self._key_report()
        self.assertEqual(report["key_reconciliation_status"], "key_issues_found")
        entities = self._by(report["entities"], "source_entity")
        self.assertEqual(entities["notes"]["status"], "not_supported_retail_entity")
        self.assertIsNone(entities["notes"]["scanned_rows"])
        customers = entities["customers"]
        self.assertEqual(customers["status"], "key_issues_found")
        self.assertEqual(
            {field: customers[field] for field in acceptance_report.KEY_ENTITY_COUNT_FIELDS},
            {"scanned_rows": 3, "non_blank_keys": 2, "blank_or_null_keys": 1, "distinct_keys": 1,
             "duplicate_key_rows": 1, "key_cast_failures": 0},
        )
        relationships = self._by(report["relationships"], "relationship")
        to_customers = relationships["orders.customer_id -> customers.customer_id"]
        self.assertEqual((to_customers["child_rows_checked"], to_customers["blank_or_null_child_keys"],
                          to_customers["orphan_child_rows"], to_customers["distinct_orphan_keys"]), (2, 1, 1, 1))
        to_orders = relationships["order_items.order_id -> orders.order_id"]
        self.assertEqual((to_orders["status"], to_orders["orphan_child_rows"], to_orders["distinct_orphan_keys"]),
                         ("relationship_issues_found", 2, 1))
        self.assertEqual(relationships["order_items.product_id -> products.product_id"]["status"],
                         "relationship_checks_passed")
        self.assertNotIn("SYNTH_", json.dumps(report))

    def test_mapping_file_must_match_manifest_and_resolves_mapped_keys(self) -> None:
        mapping_path = self.root / "mapping.json"
        mapping_path.write_text(json.dumps({"version": 1, "tables": {"customers": [
            {"source": "customer_number", "target": "customer_id", "cast": "string"},
        ]}}), encoding="utf-8")
        mappings = reconcile_bronze.load_mapping_file(mapping_path)
        files = {
            "customers": "customer_number,created_at,country\nSYNTH_C1,x,AU\n",
            "orders": CLEAN_CSV["orders"].replace("SYNTH_C2", "SYNTH_C1"),
        }
        self._csv_run(files, mapping_fingerprint=reconcile_bronze.mappings_fingerprint(mappings))

        code, _, stderr = self._reconcile("--csv-dir", str(self.csv_dir))
        self.assertEqual(code, 1)
        self.assertIn("ValueError", stderr)
        self.assertFalse((self.manifest_dir / KEY_REPORT).exists())

        code, _, stderr = self._reconcile("--csv-dir", str(self.csv_dir), "--mapping-file", str(mapping_path))
        self.assertEqual(code, 0, stderr)
        report = self._key_report()
        entities = self._by(report["entities"], "source_entity")
        self.assertEqual(entities["customers"]["status"], "key_checks_passed")
        relationships = self._by(report["relationships"], "relationship")
        self.assertEqual(relationships["orders.customer_id -> customers.customer_id"]["status"],
                         "relationship_checks_passed")
        self.assertEqual(relationships["order_items.order_id -> orders.order_id"]["status"],
                         "unavailable_entity_not_selected")
        self.assertEqual(report["key_reconciliation_status"], "key_evidence_unavailable")
        self.assertNotIn("customer_number", json.dumps(report))

    def test_source_drift_is_reported_as_binding_or_count_issue(self) -> None:
        self._csv_run(CLEAN_CSV, row_counts={"customers": 5})
        code, _, stderr = self._reconcile("--csv-dir", str(self.csv_dir))
        self.assertEqual(code, 0, stderr)
        report = self._key_report()
        customers = self._by(report["entities"], "source_entity")["customers"]
        self.assertEqual((customers["row_count_vs_manifest"], customers["status"]), ("count_mismatch", "key_issues_found"))

        (self.manifest_dir / KEY_REPORT).unlink()
        (self.manifest_dir / f"{RUN_ID}.json").unlink()
        self._csv_run(CLEAN_CSV, schema_fingerprint="0" * 64)
        code, _, stderr = self._reconcile("--csv-dir", str(self.csv_dir))
        self.assertEqual(code, 0, stderr)
        report = self._key_report()
        self.assertEqual(report["source_binding"]["schema_fingerprint"], "mismatch")
        self.assertEqual(report["key_reconciliation_status"], "source_binding_mismatch")

    def test_scan_limit_marks_evidence_unavailable(self) -> None:
        self._csv_run(CLEAN_CSV)
        code, _, stderr = self._reconcile("--csv-dir", str(self.csv_dir), "--max-key-rows-per-table", "2")
        self.assertEqual(code, 0, stderr)
        report = self._key_report()
        entities = self._by(report["entities"], "source_entity")
        self.assertEqual(entities["customers"]["status"], "key_checks_passed")
        self.assertEqual(entities["order_items"]["status"], "unavailable_scan_limit_exceeded")
        self.assertIsNone(entities["order_items"]["scanned_rows"])
        relationships = self._by(report["relationships"], "relationship")
        self.assertEqual(relationships["order_items.order_id -> orders.order_id"]["status"],
                         "unavailable_child_scan_incomplete")
        self.assertEqual(report["key_reconciliation_status"], "key_evidence_unavailable")

    def test_destination_options_are_rejected_in_local_mode(self) -> None:
        self._csv_run(CLEAN_CSV)
        code, _, _ = self._reconcile("--csv-dir", str(self.csv_dir), "--query-destination")
        self.assertEqual(code, 1)
        self.assertFalse((self.manifest_dir / KEY_REPORT).exists())

    def test_sqlite_source_compares_keys_as_text(self) -> None:
        database = self.root / "source.sqlite"
        with contextlib.closing(sqlite3.connect(database)) as connection:
            connection.executescript(
                "CREATE TABLE customers (customer_id INTEGER, created_at TEXT, country TEXT);"
                "INSERT INTO customers VALUES (1, 'x', 'AU'), (2, 'x', 'NZ');"
                "CREATE TABLE orders (order_id TEXT, customer_id TEXT, order_status TEXT, order_date TEXT, updated_at TEXT);"
                "INSERT INTO orders VALUES ('SYNTH_O1', '1', 'complete', 'x', 'x'), ('SYNTH_O2', '2', 'complete', 'x', 'x');"
            )
            connection.commit()
        url = f"sqlite:///{database}"
        engine = reconcile_bronze.create_read_only_sql_engine(url)
        try:
            inventory = reconcile_bronze.inventory_sql_inputs(engine, "main", ["customers", "orders"])
        finally:
            engine.dispose()
        self._write_manifest("sql", ["customers", "orders"], inventory, {"customers": 2, "orders": 2})
        code, _, stderr = self._reconcile(env={"MIGRATION_SOURCE_URL": url})
        self.assertEqual(code, 0, stderr)
        report = self._key_report()
        relationships = self._by(report["relationships"], "relationship")
        self.assertEqual(relationships["orders.customer_id -> customers.customer_id"]["status"],
                         "relationship_checks_passed")
        self.assertEqual(self._by(report["entities"], "source_entity")["customers"]["status"], "key_checks_passed")

    def test_server_database_urls_are_refused_before_connecting(self) -> None:
        self._write_manifest("sql", ["customers"], [], {"customers": 1})
        secret_url = "postgresql+psycopg://synthetic_user:synthetic_pw@db.invalid/synthetic"
        with mock.patch.object(reconcile_bronze, "create_read_only_sql_engine") as engine_factory:
            code, stdout, stderr = self._reconcile(env={"MIGRATION_SOURCE_URL": secret_url})
        engine_factory.assert_not_called()
        self.assertEqual(code, 1)
        self.assertNotIn("synthetic_pw", stdout + stderr)
        self.assertNotIn("db.invalid", stdout + stderr)

    def test_acceptance_report_includes_validated_key_evidence(self) -> None:
        code, artifact, stderr = self._acceptance("--key-reconciliation", "local")
        self.assertEqual(code, 1)  # explicit local mode requires the report; manifest is also absent
        self._csv_run(CLEAN_CSV)
        code, artifact, stderr = self._acceptance("--key-reconciliation", "local")
        self.assertEqual(code, 1, "explicit local mode must fail when no key report exists")

        code, artifact, stderr = self._acceptance()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(artifact["key_relationship_reconciliation"], {"status": "not_run", "cutover_ready": False})
        (self.manifest_dir / f"acceptance-report-{RUN_ID}.json").unlink()

        self.assertEqual(self._reconcile("--csv-dir", str(self.csv_dir))[0], 0)
        original = self._key_report()
        # An unrecognized field carrying a key value must not be propagated.
        self._replace_key_report({**original, "debug_sample_keys": ["SYNTH_C1"]})
        code, artifact, stderr = self._acceptance()
        self.assertEqual(code, 0, stderr)
        section = artifact["key_relationship_reconciliation"]
        self.assertEqual(section["status"], "key_checks_passed")
        self.assertIs(section["cutover_ready"], False)
        self.assertIs(artifact["cutover_ready"], False)
        self.assertEqual(len(section["entities"]), 4)
        self.assertNotIn("SYNTH_", json.dumps(artifact))

    def test_acceptance_report_rejects_contradictory_key_evidence(self) -> None:
        self._csv_run(CLEAN_CSV)
        self.assertEqual(self._reconcile("--csv-dir", str(self.csv_dir))[0], 0)
        original = self._key_report()

        def tampered(mutate):
            report = json.loads(json.dumps(original))
            mutate(report)
            return report

        def inflate_overall(report):
            report["relationships"][1].update(
                status="unavailable_entity_not_selected",
                **dict.fromkeys(acceptance_report.KEY_RELATIONSHIP_COUNT_FIELDS),
            )
            report["key_reconciliation_status"] = "key_checks_passed"

        cases = {
            "duplicate hidden behind passing status": lambda r: r["entities"][0].update(
                duplicate_key_rows=1, distinct_keys=r["entities"][0]["distinct_keys"] - 1),
            "manifest row count rewritten": lambda r: r["entities"][0].update(manifest_source_rows=99),
            "orphans with passing status": lambda r: r["relationships"][0].update(
                orphan_child_rows=1, distinct_orphan_keys=1),
            "cutover asserted": lambda r: r.update(cutover_ready=True),
            "destination query asserted": lambda r: r.update(destination_query_performed=True),
            "binding status contradicts fingerprints": lambda r: r["source_binding"].update(status="mismatch"),
            "overall status inflated": inflate_overall,
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                self._replace_key_report(tampered(mutate))
                code, artifact, stderr = self._acceptance()
                self.assertEqual(code, 1, label)
                self.assertIsNone(artifact)
                self.assertIn("ValueError", stderr)


if __name__ == "__main__":
    unittest.main()
