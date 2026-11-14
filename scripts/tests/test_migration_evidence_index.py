"""Schema and privacy validation for the offline migration evidence index."""

from __future__ import annotations

import importlib
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "migration"))
evidence_index = importlib.import_module("evidence_index")


class EvidenceIndexCountValidationTests(unittest.TestCase):
    def _manifest(self, *, status="failed", counts=None, progress=None):
        run_id = str(uuid.uuid4())
        pipeline = "evidence_fixture"
        namespace = "starterkit_migration_x"
        db_path = evidence_index.ROOT / ".dlt" / "pipelines" / pipeline / f"{pipeline}.duckdb"
        manifest = {
            "run_id": run_id,
            "status": status,
            "source_kind": "csv",
            "destination": "duckdb",
            "bronze_namespace": namespace,
            "pipeline_name": pipeline,
            "entities": ["customers"],
            "write_disposition": "replace",
            "destination_write_may_be_partial": False,
            "source_identity": {
                "kind": "csv",
                "files": [{"entity": "customers", "file_name": "customers.csv", "size_bytes": 42}],
                "identity_scope": "pre_extraction_file_metadata",
            },
            "target_identity": f"duckdb/{db_path}/{namespace}",
            "source_schema_fingerprint": "1" * 64,
            "typed_schema_fingerprint": "2" * 64,
            "source_mapping_fingerprint": "3" * 64,
            "source_row_counts": counts if counts is not None else {
                "customers": {
                    "rows": None, "payload_bytes": None, "extraction_status": "incomplete",
                    "provenance": "application_counted_canonical_json_payload",
                },
            },
            "accepted_extraction_progress": progress if progress is not None else {
                "customers": {"rows": 2, "payload_bytes": 84},
            },
        }
        return manifest

    def test_failed_legacy_zero_count_is_unverified_not_not_started(self):
        manifest = self._manifest(
            counts={"customers": {
                "rows": 0, "payload_bytes": 0,
                "provenance": "application_counted_canonical_json_payload",
            }},
            progress=None,
        )
        manifest.pop("accepted_extraction_progress")
        validated = evidence_index._validate_manifest(manifest, manifest["run_id"])
        count = validated["source_row_counts"]["customers"]
        self.assertEqual(count["extraction_status"], "unverified_legacy")
        self.assertIsNone(count["rows"])
        self.assertEqual(validated["accepted_extraction_progress"]["customers"], {"rows": 0, "payload_bytes": 0})

    def test_current_progress_map_must_cover_every_entity(self):
        manifest = self._manifest(progress={})
        with self.assertRaisesRegex(ValueError, "cover every entity"):
            evidence_index._validate_manifest(manifest, manifest["run_id"])

    def test_complete_source_counts_must_match_progress(self):
        manifest = self._manifest(
            status="completed",
            counts={"customers": {
                "rows": 2, "payload_bytes": 84, "extraction_status": "complete",
                "provenance": "application_counted_canonical_json_payload",
            }},
            progress={"customers": {"rows": 3, "payload_bytes": 84}},
        )
        with self.assertRaisesRegex(ValueError, "match accepted extraction progress"):
            evidence_index._validate_manifest(manifest, manifest["run_id"])

    def test_incomplete_rows_and_payload_bytes_must_both_be_null(self):
        manifest = self._manifest(counts={"customers": {
            "rows": 0, "payload_bytes": None, "extraction_status": "incomplete",
            "provenance": "application_counted_canonical_json_payload",
        }})
        with self.assertRaisesRegex(ValueError, "both be present or both be null"):
            evidence_index._validate_manifest(manifest, manifest["run_id"])

    def test_not_started_entity_cannot_have_accepted_progress(self):
        manifest = self._manifest(
            counts={"customers": {
                "rows": None, "payload_bytes": None, "extraction_status": "not_started",
                "provenance": "application_counted_canonical_json_payload",
            }},
            progress={"customers": {"rows": 1, "payload_bytes": 5}},
        )
        with self.assertRaisesRegex(ValueError, "Not-started entities"):
            evidence_index._validate_manifest(manifest, manifest["run_id"])


if __name__ == "__main__":
    unittest.main()
