"""Offline tests for the incremental-ingest template. Standard library only; no network."""

from __future__ import annotations

import csv
import copy
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parents[1]
MOCK_DIR = TEMPLATE_DIR / "examples" / "mock"
MANIFEST_PATH = TEMPLATE_DIR / "manifest.example.json"
BATCHES = ("MOCK_orders_batch_001.csv", "MOCK_orders_batch_002.csv", "MOCK_orders_batch_003.csv")
NOW = datetime(2025, 3, 5, tzinfo=timezone.utc)
HEADER = "order_id,customer_id,order_status,order_total,updated_at\n"

_spec = importlib.util.spec_from_file_location("incremental_ingest_template", TEMPLATE_DIR / "ingest.py")
ingest_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ingest_module
_spec.loader.exec_module(ingest_module)


def ts(text: str) -> str:
    return ingest_module.canonical_timestamp(text)


class IngestTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.input_dir = self.root / "in"
        self.input_dir.mkdir()
        self.state_db = self.root / "state.sqlite"
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def streams(self, **overrides):
        document = copy.deepcopy(self.manifest)
        document["streams"][0].update(overrides)
        return ingest_module.parse_manifest(document)

    def stage(self, *names: str) -> None:
        for name in names:
            shutil.copy(MOCK_DIR / name, self.input_dir / name)

    def write(self, name: str, body: str) -> None:
        (self.input_dir / name).write_text(body, encoding="utf-8")

    def run_ingest(self, streams=None, **kwargs):
        kwargs.setdefault("now", NOW)
        return ingest_module.ingest(streams or self.streams(), self.input_dir, self.state_db, **kwargs)

    def query(self, sql: str, db: Path = None) -> list:
        connection = sqlite3.connect(str(db or self.state_db))
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def target(self, db: Path = None, with_lineage: bool = False) -> list:
        lineage = ", _source_file, _applied_run_id" if with_lineage else ""
        present = [row[1] for row in self.query("PRAGMA table_info(tgt_orders)", db)]
        channel = "mock_channel" if "mock_channel" in present else "NULL"
        return self.query(
            f"SELECT order_id, customer_id, order_status, order_total, updated_at, {channel}, "
            f"_cursor_value, _row_hash{lineage} FROM tgt_orders ORDER BY order_id",
            db,
        )

    def watermark(self) -> str:
        return self.query("SELECT high_watermark FROM _ingest_checkpoint WHERE stream = 'orders'")[0][0]

    def counts(self, summary: dict, index: int = 0) -> dict:
        return summary["streams"]["orders"]["files"][index]["counts"]


class WatermarkAndMergeTests(IngestTestCase):
    def test_timezone_boundary_fixtures_normalize_to_expected_utc_order_and_date(self) -> None:
        fixture = TEMPLATE_DIR / "fixtures" / "timestamp_parity.csv"
        with fixture.open(encoding="utf-8", newline="") as handle:
            cases = list(csv.DictReader(handle))

        self.assertEqual(len(cases), 5)
        normalized = [ts(case["input_timestamp"]) for case in cases]
        expected = [case["expected_utc"] for case in cases]
        self.assertEqual(normalized, expected)
        self.assertEqual(
            [value[:10] for value in normalized],
            [case["expected_utc_date"] for case in cases],
        )
        self.assertLess(normalized[0], normalized[1], "the repeated DST hour has two distinct instants")
        self.assertEqual(normalized[3], normalized[4], "offset variants of one instant must normalize equally")

    def test_first_batch_sets_watermark(self) -> None:
        self.stage(BATCHES[0])
        summary = self.run_ingest()
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(self.counts(summary)["inserted"], 3)
        self.assertIsNone(summary["streams"]["orders"]["watermark_before"])
        self.assertEqual(self.watermark(), ts("2025-03-02T08:15:00Z"))

    def test_second_batch_updates_dedupes_and_accepts_late_rows_in_lookback(self) -> None:
        self.stage(BATCHES[0])
        self.run_ingest()
        self.stage(BATCHES[1])
        summary = self.run_ingest()
        files = summary["streams"]["orders"]["files"]
        self.assertEqual(files[0]["status"], "skipped_already_applied")
        counts = files[1]["counts"]
        self.assertEqual(
            {k: counts[k] for k in ("inserted", "updated", "unchanged", "stale_ignored", "late_within_lookback")},
            {"inserted": 2, "updated": 1, "unchanged": 1, "stale_ignored": 1, "late_within_lookback": 3},
        )
        rows = {row[0]: row for row in self.target()}
        self.assertEqual(rows["MOCK-O-0001"][2], "shipped")
        self.assertEqual(rows["MOCK-O-0003"][2], "shipped", "an older version must not overwrite a newer one")
        self.assertIn("MOCK-O-0004", rows)
        self.assertEqual(self.watermark(), ts("2025-03-03T13:10:00Z"))

    def test_watermark_never_moves_backwards(self) -> None:
        self.stage(BATCHES[1])
        self.run_ingest()
        high = self.watermark()
        self.stage(BATCHES[0])
        self.run_ingest(self.streams(late_arrival_policy="apply"))
        self.assertEqual(self.watermark(), high)

    def test_equal_cursor_conflict_resolves_independently_of_order(self) -> None:
        first = "MOCK-O-0100,MOCK-C-01,placed,1.00,2025-03-01T00:00:00Z\n"
        second = "MOCK-O-0100,MOCK-C-01,held,1.00,2025-03-01T00:00:00Z\n"
        results = []
        for order in ((first, second), (second, first)):
            self.state_db = self.root / f"tie_{len(results)}.sqlite"
            self.write("MOCK_orders_batch_900.csv", HEADER + "".join(order))
            self.run_ingest()
            results.append(self.target())
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(results[0]), 1)


class IdempotencyAndReplayTests(IngestTestCase):
    def test_rerun_and_replay_change_nothing(self) -> None:
        self.stage(*BATCHES)
        self.run_ingest()
        before = self.target(with_lineage=True)
        quarantine = self.query("SELECT * FROM _ingest_quarantine")
        self.assertEqual(len(before), 6)

        rerun = self.run_ingest()
        self.assertTrue(all(f["status"] == "skipped_already_applied" for f in rerun["streams"]["orders"]["files"]))
        self.assertEqual(self.target(with_lineage=True), before)
        self.assertEqual(self.query("SELECT * FROM _ingest_quarantine"), quarantine)

    def test_replay_is_a_no_op_for_rows_already_applied(self) -> None:
        self.stage(*BATCHES)
        self.run_ingest(self.streams(late_arrival_policy="apply"))
        before = self.target(with_lineage=True)
        replay = self.run_ingest(mode="replay")
        for index in range(3):
            counts = self.counts(replay, index)
            self.assertEqual((counts["inserted"], counts["updated"]), (0, 0))
        self.assertEqual(self.target(with_lineage=True), before)

    def test_replay_converges_regardless_of_file_order(self) -> None:
        self.stage(*BATCHES)
        self.run_ingest(mode="replay")
        forward = self.target()

        reversed_dir = self.root / "reversed"
        reversed_dir.mkdir()
        for position, name in enumerate(reversed(BATCHES)):
            shutil.copy(MOCK_DIR / name, reversed_dir / f"MOCK_orders_batch_{position}.csv")
        other_db = self.root / "reversed.sqlite"
        ingest_module.ingest(self.streams(), reversed_dir, other_db, mode="replay", now=NOW)
        self.assertEqual(self.target(other_db), forward)
        self.assertEqual(len(forward), 7)

    def test_changed_file_fails_closed_until_replayed(self) -> None:
        self.stage(BATCHES[0])
        self.run_ingest()
        before = self.target()
        path = self.input_dir / BATCHES[0]
        path.write_text(
            path.read_text(encoding="utf-8") + "MOCK-O-0050,MOCK-C-09,placed,5.00,2025-03-02T09:00:00Z\n",
            encoding="utf-8",
        )
        with self.assertRaises(ingest_module.SourceChangedError):
            self.run_ingest()
        self.assertEqual(self.target(), before)
        self.run_ingest(mode="replay")
        self.assertEqual(len(self.target()), 4)
        self.assertEqual(self.run_ingest()["streams"]["orders"]["files"][0]["status"], "skipped_already_applied")


class LateArrivalAndBackfillTests(IngestTestCase):
    def test_row_beyond_lookback_is_quarantined_then_backfilled(self) -> None:
        self.stage(*BATCHES)
        summary = self.run_ingest()
        self.assertEqual(self.counts(summary, 2)["late_beyond_lookback"], 1)
        self.assertNotIn("MOCK-O-0007", [row[0] for row in self.target()])
        self.assertEqual(
            self.query("SELECT file_name, line_number, reason FROM _ingest_quarantine"),
            [(BATCHES[2], 4, "late_beyond_lookback")],
        )
        high = self.watermark()
        ledger = self.query("SELECT * FROM _ingest_files ORDER BY file_name")

        backfill = self.run_ingest(
            mode="backfill", backfill_from="2025-02-27T00:00:00Z", backfill_to="2025-02-28T00:00:00Z"
        )
        self.assertEqual(self.counts(backfill, 2)["inserted"], 1)
        self.assertEqual(self.counts(backfill, 0)["outside_backfill_window"], 3)
        self.assertIn("MOCK-O-0007", [row[0] for row in self.target()])
        self.assertEqual(self.query("SELECT * FROM _ingest_quarantine"), [])
        self.assertEqual(self.watermark(), high, "backfill must not move the watermark")
        self.assertEqual(self.query("SELECT * FROM _ingest_files ORDER BY file_name"), ledger)

        snapshot = self.target(with_lineage=True)
        self.run_ingest(mode="backfill", backfill_from="2025-02-27T00:00:00Z", backfill_to="2025-02-28T00:00:00Z")
        self.assertEqual(self.target(with_lineage=True), snapshot)

    def test_apply_policy_merges_rows_beyond_lookback(self) -> None:
        self.stage(*BATCHES)
        summary = self.run_ingest(self.streams(late_arrival_policy="apply"))
        self.assertEqual(self.counts(summary, 2)["late_beyond_lookback"], 1)
        self.assertEqual(len(self.target()), 7)
        self.assertEqual(self.query("SELECT * FROM _ingest_quarantine"), [])

    def test_backfill_bounds_are_validated(self) -> None:
        self.stage(BATCHES[0])
        for bounds in (
            {},
            {"backfill_from": "2025-03-02T00:00:00Z", "backfill_to": "2025-03-01T00:00:00Z"},
            {"backfill_from": "2025-03-01T00:00:00", "backfill_to": "2025-03-02T00:00:00Z"},
        ):
            with self.subTest(bounds=bounds), self.assertRaises(ingest_module.IngestError):
                self.run_ingest(mode="backfill", **bounds)
        with self.assertRaises(ingest_module.IngestError):
            self.run_ingest(backfill_from="2025-03-01T00:00:00Z", backfill_to="2025-03-02T00:00:00Z")


class SchemaDriftTests(IngestTestCase):
    def test_add_policy_adds_nullable_column_and_keeps_old_rows_stable(self) -> None:
        self.stage(BATCHES[0], BATCHES[1])
        self.run_ingest()
        untouched = [row for row in self.target(with_lineage=True) if row[0] != "MOCK-O-0005"]
        self.stage(BATCHES[2])
        self.run_ingest()
        rows = {row[0]: row for row in self.target(with_lineage=True)}
        self.assertEqual(rows["MOCK-O-0006"][5], "store")
        self.assertIsNone(rows["MOCK-O-0001"][5])
        self.assertEqual([rows[row[0]] for row in untouched], untouched)
        self.assertEqual(
            self.query("SELECT column_name, event, first_file_name FROM _ingest_schema_history"),
            [("mock_channel", "added", BATCHES[2])],
        )
        # Older files without the column still replay to the same hashes.
        replay = self.run_ingest(mode="replay")
        first = self.counts(replay, 0)
        self.assertEqual(
            (first["inserted"], first["updated"], first["unchanged"], first["stale_ignored"]), (0, 0, 2, 1)
        )

    def test_fail_policy_stops_before_any_change(self) -> None:
        self.stage(BATCHES[0])
        streams = self.streams(schema_drift={"on_new_column": "fail"})
        self.run_ingest(streams)
        before = (self.target(), self.watermark(), self.query("SELECT file_name FROM _ingest_files"))
        self.stage(BATCHES[2])
        with self.assertRaises(ingest_module.SchemaDriftError):
            self.run_ingest(streams)
        self.assertEqual((self.target(), self.watermark(), self.query("SELECT file_name FROM _ingest_files")), before)

    def test_ignore_policy_drops_the_column_and_records_it(self) -> None:
        self.stage(BATCHES[2])
        self.run_ingest(self.streams(schema_drift={"on_new_column": "ignore"}))
        self.assertTrue(all(row[5] is None for row in self.target()))
        self.assertEqual(
            self.query("SELECT column_name, event FROM _ingest_schema_history"), [("mock_channel", "ignored")]
        )

    def test_missing_required_column_fails_and_missing_nullable_is_recorded(self) -> None:
        self.write("MOCK_orders_batch_800.csv", "order_id,customer_id,order_total,updated_at\n")
        with self.assertRaises(ingest_module.SchemaDriftError):
            self.run_ingest()
        self.write(
            "MOCK_orders_batch_800.csv",
            "order_id,customer_id,order_status,updated_at\nMOCK-O-0200,MOCK-C-01,placed,2025-03-01T00:00:00Z\n",
        )
        self.run_ingest()
        self.assertIsNone(self.target()[0][3])
        self.assertEqual(
            self.query("SELECT column_name, event FROM _ingest_schema_history"), [("order_total", "missing_nullable")]
        )

    def test_duplicate_or_unsafe_header_fails(self) -> None:
        for header in ("order_id,order_id,customer_id,order_status,order_total,updated_at\n", HEADER.strip() + ",Bad Name\n"):
            with self.subTest(header=header):
                self.write("MOCK_orders_batch_800.csv", header)
                with self.assertRaises(ingest_module.SchemaDriftError):
                    self.run_ingest()


class FailureAndCheckpointTests(IngestTestCase):
    def test_failure_mid_file_rolls_back_and_resume_matches_clean_run(self) -> None:
        self.stage(*BATCHES)

        def fail_in_second_file(stream: str, file_name: str, rows: int) -> None:
            if file_name == BATCHES[1] and rows == 3:
                raise RuntimeError("simulated crash")

        with self.assertRaises(RuntimeError):
            self.run_ingest(fault_injector=fail_in_second_file)
        self.assertEqual(len(self.target()), 3, "only the first file may be committed")
        self.assertEqual(self.query("SELECT file_name FROM _ingest_files"), [(BATCHES[0],)])
        self.assertEqual(self.watermark(), ts("2025-03-02T08:15:00Z"))
        self.assertEqual(
            self.query("SELECT status, error_type, error_message FROM _ingest_runs"), [("failed", "RuntimeError", "")]
        )

        resumed = self.run_ingest()
        self.assertEqual(
            [f["status"] for f in resumed["streams"]["orders"]["files"]],
            ["skipped_already_applied", "applied", "applied"],
        )
        clean_db = self.root / "clean.sqlite"
        ingest_module.ingest(self.streams(), self.input_dir, clean_db, now=NOW)
        self.assertEqual(self.target(), self.target(clean_db))
        self.assertEqual(
            self.query("SELECT stream, file_name, line_number, reason FROM _ingest_quarantine"),
            self.query("SELECT stream, file_name, line_number, reason FROM _ingest_quarantine", clean_db),
        )
        self.assertEqual(self.query("SELECT status FROM _ingest_runs ORDER BY started_at, status")[-1], ("succeeded",))

    def test_bad_rows_fail_closed_by_default(self) -> None:
        self.stage(BATCHES[0])
        self.run_ingest()
        self.write(
            "MOCK_orders_batch_700.csv",
            HEADER
            + "MOCK-O-0300,MOCK-C-01,placed,not-a-number,2025-03-02T10:00:00Z\n"
            + "MOCK-O-0301,MOCK-C-01,placed,3.00,2025-03-02T11:00:00Z\n",
        )
        with self.assertRaises(ingest_module.RejectThresholdError) as caught:
            self.run_ingest()
        self.assertIn("line 2: invalid_decimal:order_total", str(caught.exception))
        self.assertNotIn("not-a-number", str(caught.exception))
        self.assertEqual(len(self.target()), 3)
        self.assertEqual(self.query("SELECT * FROM _ingest_quarantine"), [])

    def test_bad_rows_within_threshold_are_quarantined(self) -> None:
        self.write(
            "MOCK_orders_batch_700.csv",
            HEADER
            + ",MOCK-C-01,placed,1.00,2025-03-02T10:00:00Z\n"
            + "MOCK-O-0301,MOCK-C-01,placed,3.00,2025-03-02T11:00:00\n"
            + "MOCK-O-0302,MOCK-C-01,placed\n"
            + "MOCK-O-0303,MOCK-C-01,placed,3.00,2031-01-01T00:00:00Z\n"
            + "MOCK-O-0304,MOCK-C-01,placed,3.00,2025-03-02T12:00:00+10:00\n",
        )
        summary = self.run_ingest(self.streams(max_rejected_rows_per_file=4))
        self.assertEqual(self.counts(summary)["rejected"], 4)
        self.assertEqual([row[0] for row in self.target()], ["MOCK-O-0304"])
        self.assertEqual(
            self.query("SELECT line_number, reason FROM _ingest_quarantine ORDER BY line_number"),
            [
                (2, "null_not_allowed:order_id"),
                (3, "invalid_timestamp:updated_at"),
                (4, "field_count_mismatch"),
                (5, "future_cursor"),
            ],
        )
        # A rejected future timestamp must not drag the watermark forward.
        self.assertEqual(self.watermark(), ts("2025-03-02T02:00:00Z"))

    def test_non_utf8_file_fails(self) -> None:
        (self.input_dir / "MOCK_orders_batch_600.csv").write_bytes(b"\xff\xfe\x00bad")
        with self.assertRaises(ingest_module.IngestError):
            self.run_ingest()


class ManifestAndCliTests(IngestTestCase):
    def test_invalid_manifests_are_rejected(self) -> None:
        cases = {
            "version": lambda d: d.update(manifest_version=2),
            "glob path": lambda d: d["streams"][0].update(file_glob="../MOCK_*.csv"),
            "unknown key": lambda d: d["streams"][0].update(primary_key=["missing"]),
            "cursor type": lambda d: d["streams"][0].update(cursor_column="order_id"),
            "nullable key": lambda d: d["streams"][0]["columns"][0].update(nullable=True),
            "bad policy": lambda d: d["streams"][0].update(late_arrival_policy="drop"),
            "negative lookback": lambda d: d["streams"][0].update(lookback_seconds=-1),
            "reserved name": lambda d: d["streams"][0]["columns"][1].update(name="_row_hash"),
            "duplicate stream": lambda d: d["streams"].append(copy.deepcopy(d["streams"][0])),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                document = copy.deepcopy(self.manifest)
                mutate(document)
                with self.assertRaises(ingest_module.ManifestError):
                    ingest_module.parse_manifest(document)

    def test_cli_runs_the_mock_example_end_to_end(self) -> None:
        command = [sys.executable, str(TEMPLATE_DIR / "ingest.py"), "--manifest", str(MANIFEST_PATH)]
        validate = subprocess.run(command + ["--validate-only"], capture_output=True, text=True, check=False)
        self.assertEqual(validate.returncode, 0, validate.stderr)
        run = subprocess.run(
            command + ["--input-dir", str(MOCK_DIR), "--state-db", str(self.state_db)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["status"], "succeeded")
        self.assertEqual(len(self.target()), 6)

        self.stage(BATCHES[2])
        failed = subprocess.run(
            command + ["--input-dir", str(self.input_dir), "--state-db", str(self.root / "fail.sqlite"), "--mode", "backfill"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(json.loads(failed.stderr)["status"], "failed")

    def test_example_data_is_marked_mock(self) -> None:
        files = sorted(p.name for p in MOCK_DIR.iterdir())
        self.assertEqual(files, sorted(BATCHES))
        for name in files:
            lines = (MOCK_DIR / name).read_text(encoding="utf-8").splitlines()[1:]
            self.assertTrue(lines)
            for line in lines:
                fields = line.split(",")
                self.assertTrue(fields[0].startswith("MOCK-O-") and fields[1].startswith("MOCK-C-"), line)


if __name__ == "__main__":
    unittest.main()
