"""Card 324: disk probe with reserved headroom; failed or unsupported probes refuse (F6-F8)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from resource_budget import estimate as est
from resource_budget import result as r
from resource_budget.disk import check_headroom, shutil_free_bytes
from resource_budget.schema import load_envelope

GIB = 1_073_741_824
SECRET_DIR = "/home/secret_user/private_customer_dir"


def _envelope(scratch: int = 2 * GIB, headroom: int = GIB):
    return load_envelope(
        {
            "schema_version": 1,
            "limits": {
                "install.scratch_bytes": {"unit": "bytes", "limit": scratch},
                "disk.reserved_headroom_bytes": {"unit": "bytes", "limit": headroom},
            },
        }
    )


class RecordingProbe:
    def __init__(self, value=None, exc: BaseException | None = None):
        self.value, self.exc, self.calls = value, exc, []

    def __call__(self, directory):
        self.calls.append(directory)
        if self.exc is not None:
            raise self.exc
        return self.value


def _check(projected, probe, envelope=None, directory=SECRET_DIR):
    return check_headroom(
        envelope or _envelope(),
        projected_key="install.scratch_bytes",
        projected=projected,
        directory=directory,
        probe=probe,
    )


class HeadroomTests(unittest.TestCase):
    def test_sufficient_headroom_is_a_probe_claim_not_a_guarantee(self):
        probe = RecordingProbe(GIB + GIB)
        result = _check(est.known("bytes", GIB), probe)
        self.assertEqual(result.outcome, r.PROBE_HEADROOM_SUFFICIENT)
        self.assertFalse(result.refused)
        self.assertEqual(probe.calls, [SECRET_DIR])
        probe_dim = result.dimensions[1]
        self.assertEqual((probe_dim.claim_kind, probe_dim.value), (r.PROBE, GIB))
        self.assertEqual(probe_dim.status, r.HEADROOM_SUFFICIENT_AT_PROBE)

    def test_f7_below_projected_plus_headroom_refuses_before_any_write(self):
        probe = RecordingProbe(2 * GIB - 1)
        result = _check(est.known("bytes", GIB), probe)
        self.assertEqual(result.outcome, r.INSUFFICIENT_HEADROOM)
        self.assertTrue(result.refused)
        self.assertEqual(result.dimensions[1].value, GIB - 1)
        self.assertIn("did not delete", result.hint)

    def test_free_space_below_projected_reports_zero_remaining(self):
        result = _check(est.known("bytes", GIB), RecordingProbe(5))
        self.assertEqual(result.outcome, r.INSUFFICIENT_HEADROOM)
        self.assertEqual(result.dimensions[1].value, 0)

    def test_f8_failed_unsupported_or_malformed_probe_refuses(self):
        probes = [
            None,
            RecordingProbe(exc=OSError(2, "No such file", SECRET_DIR)),
            RecordingProbe(exc=NotImplementedError()),
            RecordingProbe(exc=RuntimeError(SECRET_DIR)),
            RecordingProbe(None),
            RecordingProbe(-1),
            RecordingProbe(True),
            RecordingProbe(1e20),
            RecordingProbe("999999999999"),
            RecordingProbe(2**63),
        ]
        for probe in probes:
            with self.subTest(probe=probe):
                result = _check(est.known("bytes", 1), probe)
                self.assertEqual(result.outcome, r.PROBE_FAILED)
                self.assertTrue(result.refused)
                probe_dim = result.dimensions[1]
                self.assertEqual((probe_dim.claim_kind, probe_dim.value), (r.UNKNOWN, None))
                self.assertNotIn(SECRET_DIR, repr(result.to_dict()))

    def test_f6_unknown_projection_refuses_without_probing(self):
        probe = RecordingProbe(10 * GIB)
        result = _check(est.product("bytes", None, 10), probe)
        self.assertEqual(result.outcome, r.ESTIMATE_UNKNOWN)
        self.assertTrue(result.refused)
        self.assertEqual(probe.calls, [])

    def test_f5_projection_over_its_limit_refuses_without_probing(self):
        probe = RecordingProbe(100 * GIB)
        result = _check(est.known("bytes", 2 * GIB + 1), probe)
        self.assertEqual(result.outcome, r.ESTIMATE_EXCEEDS_LIMIT)
        self.assertEqual(probe.calls, [])

    def test_projected_plus_headroom_overflow_is_unknown(self):
        envelope = _envelope(scratch=2**63 - 1, headroom=2**62)
        probe = RecordingProbe(2**63 - 1)
        result = _check(est.known("bytes", 2**62), probe, envelope)
        self.assertEqual(result.outcome, r.ESTIMATE_UNKNOWN)
        self.assertEqual(probe.calls, [])

    def test_requires_supplied_headroom_limit(self):
        envelope = load_envelope(
            {"schema_version": 1, "limits": {"install.scratch_bytes": {"unit": "bytes", "limit": 5}}}
        )
        with self.assertRaises(ValueError):
            _check(est.known("bytes", 1), RecordingProbe(10), envelope)

    def test_rejects_bad_arguments(self):
        with self.assertRaises(ValueError):
            _check(est.known("rows", 1), RecordingProbe(10))
        with self.assertRaises(ValueError):
            _check(est.known("bytes", 1), "not callable")
        with self.assertRaises(ValueError):
            _check(est.known("bytes", 1), RecordingProbe(10), directory=3)
        with self.assertRaises(ValueError):
            check_headroom(
                _envelope(),
                projected_key="disk.reserved_headroom_bytes",
                projected=est.known("bytes", 1),
                directory=SECRET_DIR,
            )

    def test_default_probe_reads_only_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = sorted(os.listdir(tmp))
            free = shutil_free_bytes(tmp)
            self.assertIsInstance(free, int)
            self.assertGreaterEqual(free, 0)
            self.assertEqual(sorted(os.listdir(tmp)), before)
            missing = os.path.join(tmp, "missing")
            result = check_headroom(
                load_envelope(
                    {
                        "schema_version": 1,
                        "limits": {
                            "install.scratch_bytes": {"unit": "bytes", "limit": 10},
                            "disk.reserved_headroom_bytes": {"unit": "bytes", "limit": 1},
                        },
                    }
                ),
                projected_key="install.scratch_bytes",
                projected=est.known("bytes", 1),
                directory=missing,
            )
            self.assertEqual(result.outcome, r.PROBE_FAILED)
            self.assertFalse(os.path.exists(missing))
            self.assertNotIn(tmp, repr(result.to_dict()))


if __name__ == "__main__":
    unittest.main()
