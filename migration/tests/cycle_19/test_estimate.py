"""Card 322: conservative estimates; unknown propagates; overflow is unknown; round up (F5, F6)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from resource_budget import estimate as est
from resource_budget import result as r
from resource_budget.estimate import Estimate, EstimateInputError
from resource_budget.schema import load_envelope

MAX = 2**63 - 1


def _envelope(**limits: int):
    return load_envelope(
        {
            "schema_version": 1,
            "limits": {key: {"unit": key.rsplit("_", 1)[1], "limit": value} for key, value in limits.items()},
        }
    )


class ArithmeticTests(unittest.TestCase):
    def test_product_and_total(self):
        self.assertEqual(est.product("bytes", 10, 7), Estimate("bytes", 70))
        self.assertEqual(est.total("rows", 1, est.known("rows", 2), 3), Estimate("rows", 6))

    def test_unknown_propagates_and_is_never_a_smaller_number(self):
        for result in (
            est.product("bytes", 10, None),
            est.product("bytes", None, 0),
            est.total("bytes", 5, None),
            est.total("bytes", est.known("bytes", 5), est.unknown("bytes")),
            est.scale_up(est.unknown("bytes"), 2, 1),
            est.table_bytes(None, [4, 8]),
            est.table_bytes(10, [4, None]),
            est.table_bytes(10, [4], per_row_overhead=None),
            est.table_bytes(10, []),
        ):
            with self.subTest(result=result):
                self.assertFalse(result.known)
                self.assertIsNone(result.value)

    def test_overflow_becomes_unknown_not_a_clamp(self):
        for result in (
            est.product("bytes", MAX, 2),
            est.product("bytes", 2**32, 2**32),
            est.total("bytes", MAX, 1),
            est.scale_up(est.known("bytes", MAX), 2, 2),
            est.table_bytes(MAX, [1, 1]),
        ):
            with self.subTest(result=result):
                self.assertFalse(result.known)
                self.assertEqual(result.unknown_reason, est.REASON_OVERFLOW)
        self.assertEqual(est.total("bytes", est.product("bytes", MAX, 2), 1).unknown_reason, est.REASON_OVERFLOW)

    def test_exact_maximum_is_known(self):
        self.assertEqual(est.product("bytes", MAX, 1).value, MAX)
        self.assertEqual(est.total("bytes", MAX - 1, 1).value, MAX)

    def test_rounding_is_always_up(self):
        self.assertEqual(est.ceil_div(10, 3), 4)
        self.assertEqual(est.ceil_div(9, 3), 3)
        self.assertEqual(est.ceil_div(0, 3), 0)
        self.assertEqual(est.scale_up(est.known("bytes", 101), 125, 100).value, 127)
        self.assertEqual(est.scale_up(est.known("bytes", 1), 1, 3).value, 1)

    def test_table_bytes_sums_widths_and_overhead_then_multiplies(self):
        self.assertEqual(est.table_bytes(1000, [8, 16, 255], per_row_overhead=24).value, 1000 * (8 + 16 + 255 + 24))
        self.assertEqual(est.table_bytes(0, [8]).value, 0)

    def test_invalid_inputs_are_refused_not_coerced(self):
        for bad in (True, False, 1.5, "10", -1, 2**63, [1]):
            with self.subTest(bad=bad):
                with self.assertRaises(EstimateInputError):
                    est.product("bytes", bad, 1)
                with self.assertRaises(EstimateInputError):
                    est.total("bytes", 1, bad)
                with self.assertRaises(EstimateInputError):
                    est.table_bytes(bad, [1])
                with self.assertRaises(EstimateInputError):
                    est.table_bytes(1, [bad])
        with self.assertRaises(EstimateInputError):
            est.ceil_div(1, 0)
        with self.assertRaises(EstimateInputError):
            est.scale_up(est.known("bytes", 1), 1, 0)
        with self.assertRaises(EstimateInputError):
            est.table_bytes(1, (w for w in [1]))
        with self.assertRaises(EstimateInputError):
            est.product("MB", 1)
        with self.assertRaises(EstimateInputError):
            est.total("bytes", est.known("rows", 1))
        with self.assertRaises(EstimateInputError):
            Estimate("bytes", None)
        with self.assertRaises(EstimateInputError):
            Estimate("bytes", 5, est.REASON_OVERFLOW)
        with self.assertRaises(EstimateInputError):
            est.product("bytes", *([1] * (est.MAX_PARTS + 1)))


class CheckEstimatesTests(unittest.TestCase):
    def setUp(self):
        self.envelope = _envelope(**{"install.scratch_bytes": 1000, "install.scratch_files": 10})

    def test_f5_estimate_above_limit_refuses_before_work(self):
        result = est.check_estimates(self.envelope, {"install.scratch_bytes": est.known("bytes", 1001)})
        self.assertEqual(result.outcome, r.ESTIMATE_EXCEEDS_LIMIT)
        self.assertTrue(result.refused)
        self.assertEqual(result.phase, r.PRE_RUN)
        self.assertIsNone(result.partial_work_present)
        self.assertNotEqual(result.outcome, r.OBSERVED_EXHAUSTION)
        dim = result.dimensions[0]
        self.assertEqual((dim.claim_kind, dim.value, dim.status), (r.ESTIMATE, 1001, r.EXCEEDS_LIMIT))
        self.assertNotIn("observed", dim.to_dict())

    def test_estimate_at_limit_is_within_but_never_within_observed_limits(self):
        result = est.check_estimates(self.envelope, {"install.scratch_bytes": est.known("bytes", 1000)})
        self.assertEqual(result.outcome, r.ESTIMATE_WITHIN_LIMIT)
        self.assertFalse(result.refused)
        self.assertNotEqual(result.outcome, r.WITHIN_OBSERVED_LIMITS)

    def test_f6_unknown_estimate_refuses_by_default(self):
        result = est.check_estimates(self.envelope, {"install.scratch_bytes": est.product("bytes", None, 4)})
        self.assertEqual(result.outcome, r.ESTIMATE_UNKNOWN)
        self.assertTrue(result.refused)
        self.assertEqual(result.exit_code, r.EXIT_REFUSED)
        dim = result.dimensions[0]
        self.assertEqual((dim.claim_kind, dim.value), (r.UNKNOWN, None))
        self.assertNotIn("estimate", dim.to_dict())

    def test_every_dimension_is_reported_and_exceeding_wins_over_unknown(self):
        result = est.check_estimates(
            self.envelope,
            {"install.scratch_bytes": est.unknown("bytes"), "install.scratch_files": est.known("files", 11)},
        )
        self.assertEqual(result.outcome, r.ESTIMATE_EXCEEDS_LIMIT)
        self.assertEqual(
            {d.limit_key: d.status for d in result.dimensions},
            {"install.scratch_bytes": r.STATUS_UNKNOWN, "install.scratch_files": r.EXCEEDS_LIMIT},
        )
        self.assertEqual(set(result.refusing_limit_keys), {"install.scratch_bytes", "install.scratch_files"})

    def test_missing_limit_or_wrong_unit_is_refused(self):
        with self.assertRaises(ValueError):
            est.check_estimates(self.envelope, {"disk.reserved_headroom_bytes": est.known("bytes", 1)})
        with self.assertRaises(ValueError):
            est.check_estimates(self.envelope, {"install.scratch_bytes": est.known("rows", 1)})
        with self.assertRaises(ValueError):
            est.check_estimates(self.envelope, {})
        with self.assertRaises(ValueError):
            est.check_estimates(self.envelope, {1: est.known("bytes", 1), "a.b_bytes": est.known("bytes", 1)})


if __name__ == "__main__":
    unittest.main()
