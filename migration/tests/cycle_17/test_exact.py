"""Card 272: exact integer/Decimal comparison where absent is not zero."""

from __future__ import annotations

import unittest
from decimal import Decimal

from cycle_17._support import count_metric, metric, policy

from comparison_policy.exact import check_value, compare_exact, evaluate_exact
from comparison_policy.schema import parse_policy


def _metric(**overrides):
    return parse_policy(policy(metric(**overrides))).metrics[0]


def _count():
    return parse_policy(policy(count_metric())).metrics[0]


class IntSub(int):
    pass


class StrSub(str):
    pass


class ExactComparisonTests(unittest.TestCase):
    def assertCodes(self, reasons, *expected):
        self.assertEqual(sorted(code for code, _ in reasons), sorted(expected))

    def test_trailing_zeros_within_scale_are_equal(self):
        m = _metric(scale=2)
        self.assertCodes(evaluate_exact("1.0", "1.00", m), "exact_equal")
        self.assertCodes(evaluate_exact(Decimal("1.0"), Decimal("1.00"), m), "exact_equal")
        self.assertCodes(evaluate_exact(1, "1.00", m), "exact_equal")
        self.assertCodes(evaluate_exact("1.01", "1.00", m), "not_equal_exact")

    def test_scale_overflow_is_invalid_not_rounded(self):
        m = _metric(scale=2)
        self.assertCodes(evaluate_exact("1.005", "1.00", m), "value_scale_exceeds_policy")
        self.assertCodes(evaluate_exact("1.00", Decimal("1.004"), m), "value_scale_exceeds_policy")
        # Representational scale is strict: trailing zeros beyond the policy scale are refused.
        self.assertCodes(evaluate_exact("1.000", "1.00", m), "value_scale_exceeds_policy")
        self.assertCodes(evaluate_exact("1.5", "1", _count()), "value_scale_exceeds_policy")
        # Positive exponents are integral and within any scale.
        self.assertCodes(evaluate_exact(Decimal("1E+2"), 100, m), "exact_equal")

    def test_negative_zero_equals_zero(self):
        m = _metric(allow_negative=False)
        for negative_zero in (Decimal("-0"), "-0", Decimal("-0.00"), "-0.00"):
            with self.subTest(value=negative_zero):
                self.assertCodes(evaluate_exact(negative_zero, 0, m), "exact_equal")

    def test_absent_is_not_zero(self):
        m = _metric()
        self.assertCodes(evaluate_exact(0, None, m), "value_absent_target")
        self.assertCodes(evaluate_exact(None, Decimal("0"), m), "value_absent_source")
        self.assertCodes(evaluate_exact(None, None, m), "value_absent_source", "value_absent_target")
        self.assertCodes(evaluate_exact("0", 0, m), "exact_equal")
        self.assertCodes(evaluate_exact(0, "0.00", m), "exact_equal")

    def test_large_integers_above_int64_are_exact(self):
        m = _count()
        big = 2**63 + 1
        self.assertCodes(evaluate_exact(big, big, m), "exact_equal")
        self.assertCodes(evaluate_exact(2**64, 2**64 + 1, m), "not_equal_exact")
        self.assertCodes(evaluate_exact(str(2**64 + 1), 2**64 + 1, m), "exact_equal")
        # A float would collapse these two; Decimal keeps them distinct.
        self.assertEqual(float(2**53), float(2**53 + 1))
        self.assertCodes(evaluate_exact(2**53, 2**53 + 1, m), "not_equal_exact")

    def test_float_and_bool_and_subclasses_rejected(self):
        m = _metric()
        for raw in (0.0, 1.5, float("nan"), True, False, IntSub(1), StrSub("1"), b"1", [1], {"v": 1}):
            with self.subTest(raw=raw):
                self.assertCodes(evaluate_exact(raw, "1.00", m), "value_type_not_permitted")
                self.assertCodes(evaluate_exact("1.00", raw, m), "value_type_not_permitted")

    def test_non_finite_rejected(self):
        m = _metric()
        for raw in (Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity"),
                    "NaN", "nan", "Infinity", "-inf", "+Infinity"):
            with self.subTest(raw=raw):
                self.assertCodes(evaluate_exact(raw, "1.00", m), "value_not_finite")

    def test_non_canonical_strings_rejected(self):
        m = _metric(allow_negative=True)
        for raw in ("", " 1", "1 ", "+1", "01", "1.", ".5", "1e3", "1E+2", "1,000", "0x10", "1_000",
                    "--1", "١", "1" * 129):
            with self.subTest(raw=raw):
                self.assertCodes(evaluate_exact(raw, "1.00", m), "value_not_canonical_decimal")

    def test_negative_values_follow_policy(self):
        self.assertCodes(evaluate_exact("-1.00", "-1.00", _metric(allow_negative=False)),
                         "value_negative_not_permitted", "value_negative_not_permitted")
        self.assertCodes(evaluate_exact("-1.00", "-1.00", _metric(allow_negative=True)), "exact_equal")
        self.assertCodes(evaluate_exact(-5, 5, _count()), "value_negative_not_permitted")

    def test_precision_bound(self):
        m = _count()
        self.assertCodes(evaluate_exact(2**201, 1, m), "decimal_precision_exceeded")
        self.assertCodes(evaluate_exact(1, "9" * 61, m), "decimal_precision_exceeded")
        self.assertCodes(evaluate_exact(Decimal("1E+60"), 1, m), "decimal_precision_exceeded")
        self.assertCodes(evaluate_exact("9" * 60, "9" * 60, m), "exact_equal")

    def test_check_value_reports_side_and_returns_no_value_on_failure(self):
        m = _metric()
        self.assertEqual(check_value(None, "source", m), (None, (("value_absent_source", None),)))
        self.assertEqual(check_value("2.50", "target", m), (Decimal("2.50"), ()))
        with self.assertRaises(ValueError):
            check_value(1, "other", m)

    def test_compare_exact_is_numeric(self):
        self.assertEqual(compare_exact(Decimal("1E+1"), Decimal("10.00")), (("exact_equal", None),))
        self.assertEqual(compare_exact(Decimal("0"), Decimal("-0")), (("exact_equal", None),))
        self.assertEqual(compare_exact(Decimal("0.01"), Decimal("0.02")), (("not_equal_exact", None),))


if __name__ == "__main__":
    unittest.main()
