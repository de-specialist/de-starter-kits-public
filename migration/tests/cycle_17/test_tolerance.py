"""Card 273: explicit Decimal absolute/relative tolerances with no defaults."""

from __future__ import annotations

import unittest
from decimal import Decimal

from cycle_17._support import DROP, count_metric, entry, evidence, metric, policy, tolerance_metric

from comparison_policy import tolerance as tolerance_module
from comparison_policy.decision import decide
from comparison_policy.schema import parse_policy
from comparison_policy.tolerance import ToleranceBounds, evaluate_tolerance, resolve_tolerance


def _resolve(document_metric):
    return resolve_tolerance(parse_policy(policy(document_metric)).metrics[0])


def _codes(reasons):
    return sorted(code for code, _ in reasons)


def _eval(source, target, absolute=None, relative=None):
    bounds = ToleranceBounds(
        None if absolute is None else Decimal(absolute),
        None if relative is None else Decimal(relative),
        "source_rounding_at_scale",
    )
    return _codes(evaluate_tolerance(Decimal(source), Decimal(target), bounds))


class ToleranceDeclarationTests(unittest.TestCase):
    def test_valid_declarations(self):
        bounds, reasons = _resolve(tolerance_metric(absolute="0.01"))
        self.assertEqual((bounds.absolute, bounds.relative, reasons), (Decimal("0.01"), None, ()))
        bounds, reasons = _resolve(tolerance_metric(absolute=None, relative="0.0001"))
        self.assertEqual((bounds.absolute, bounds.relative, reasons), (None, Decimal("0.0001"), ()))
        bounds, _ = _resolve(tolerance_metric(absolute="1", scale=0))
        self.assertEqual(bounds.absolute, Decimal("1"))

    def test_exact_metric_has_no_bounds(self):
        self.assertEqual(_resolve(metric()), (None, ()))

    def test_no_default_when_comparison_missing(self):
        self.assertEqual(_codes(_resolve(metric(comparison=DROP))[1]), ["policy_metric_not_declared"])
        self.assertFalse([name for name in dir(tolerance_module) if "DEFAULT" in name.upper()])

    def test_tolerance_mode_without_bounds_rejected(self):
        self.assertEqual(_codes(_resolve(metric(comparison="tolerance"))[1]), ["policy_tolerance_invalid"])
        only_rationale = tolerance_metric(absolute=None, relative=None)
        self.assertEqual(_codes(_resolve(only_rationale)[1]), ["policy_tolerance_invalid"])

    def test_tolerance_block_on_exact_metric_rejected(self):
        document = metric(tolerance={"absolute": "0.01", "rationale_code": "source_rounding_at_scale"})
        self.assertEqual(_codes(_resolve(document)[1]), ["policy_tolerance_invalid"])

    def test_zero_and_non_canonical_bounds_rejected(self):
        for text in ("0", "0.0", "0.00", "00.01", "0.010", "-0.01", "+0.01", "1e-2", "1E-2", " 0.01",
                     "0.01 ", ".01", "0.", "NaN", "Infinity", "sNaN", "inf", "", "0,01"):
            with self.subTest(text=text):
                self.assertEqual(_codes(_resolve(tolerance_metric(absolute=text))[1]), ["policy_tolerance_invalid"])
                self.assertEqual(
                    _codes(_resolve(tolerance_metric(absolute=None, relative=text))[1]), ["policy_tolerance_invalid"]
                )

    def test_tolerance_not_permitted_for_counts_or_temporal_or_non_sum(self):
        cases = [
            count_metric(comparison="tolerance", tolerance={"absolute": "1", "rationale_code": "source_float_type"}),
            tolerance_metric(unit="rows", aggregation="sum", scale=0, absolute="1"),
            tolerance_metric(unit="customers", aggregation="count_distinct", scale=0, absolute="1"),
            tolerance_metric(unit="date", aggregation="max", scale=0, absolute="1"),
            tolerance_metric(unit="decimal_measure", aggregation="max"),
        ]
        for document in cases:
            with self.subTest(document=document):
                self.assertEqual(_codes(_resolve(document)[1]), ["policy_tolerance_not_permitted_for_unit"])

    def test_rationale_required_and_closed(self):
        self.assertEqual(_codes(_resolve(tolerance_metric(rationale=None))[1]), ["policy_tolerance_rationale_missing"])
        self.assertEqual(_codes(_resolve(tolerance_metric(rationale="because"))[1]), ["policy_tolerance_invalid"])

    def test_caps(self):
        self.assertEqual(_codes(_resolve(tolerance_metric(absolute="0.02"))[1]), ["policy_tolerance_exceeds_cap"])
        self.assertEqual(_codes(_resolve(tolerance_metric(absolute="0.011"))[1]), ["policy_tolerance_exceeds_cap"])
        self.assertEqual(_codes(_resolve(tolerance_metric(absolute="2", scale=0))[1]), ["policy_tolerance_exceeds_cap"])
        self.assertEqual(
            _codes(_resolve(tolerance_metric(absolute=None, relative="0.00011"))[1]), ["policy_tolerance_exceeds_cap"]
        )
        self.assertEqual(_resolve(tolerance_metric(absolute="0.001", scale=3))[1], ())
        self.assertEqual(_resolve(tolerance_metric(absolute="0.005"))[1], ())

    def test_multiple_problems_reported_sorted(self):
        reasons = _resolve(tolerance_metric(absolute="0.02", relative="abc", rationale=None))[1]
        self.assertEqual(
            _codes(reasons),
            ["policy_tolerance_exceeds_cap", "policy_tolerance_invalid", "policy_tolerance_rationale_missing"],
        )


class ToleranceEvaluationTests(unittest.TestCase):
    def test_absolute_below_on_and_above(self):
        self.assertEqual(_eval("10.00", "10.00", absolute="0.01"), ["exact_equal"])
        self.assertEqual(_eval("10.000", "10.009", absolute="0.01"), ["within_absolute_tolerance"])
        self.assertEqual(_eval("10.00", "10.01", absolute="0.01"), ["within_absolute_tolerance"])
        self.assertEqual(_eval("10.00", "9.99", absolute="0.01"), ["within_absolute_tolerance"])
        self.assertEqual(_eval("10.000", "10.011", absolute="0.01"), ["outside_tolerance"])
        self.assertEqual(_eval("10.00", "10.02", absolute="0.01"), ["outside_tolerance"])
        # A bound finer than the value scale is never rounded up.
        self.assertEqual(_eval("10.00", "10.01", absolute="0.005"), ["outside_tolerance"])

    def test_relative_below_on_and_above(self):
        # Bound = 0.0001 * 10000.00 = 1.0000.
        self.assertEqual(_eval("10000.00", "10000.99", relative="0.0001"), ["within_relative_tolerance"])
        self.assertEqual(_eval("10000.00", "10001.00", relative="0.0001"), ["within_relative_tolerance"])
        self.assertEqual(_eval("10000.00", "9999.00", relative="0.0001"), ["within_relative_tolerance"])
        self.assertEqual(_eval("10000.00", "10001.01", relative="0.0001"), ["outside_tolerance"])

    def test_relative_reference_is_the_source_side(self):
        # 0.0001 * 9999 = 0.9999 < 1, but 0.0001 * 10000 = 1.
        self.assertEqual(_eval("9999.00", "10000.00", relative="0.0001"), ["outside_tolerance"])
        self.assertEqual(_eval("10000.00", "9999.00", relative="0.0001"), ["within_relative_tolerance"])

    def test_zero_source_with_relative_bound_admits_only_zero_difference(self):
        self.assertEqual(_eval("0", "0", relative="0.0001"), ["exact_equal"])
        self.assertEqual(_eval("0", "0.01", relative="0.0001"), ["outside_tolerance"])
        self.assertEqual(_eval("0.00", "-0.01", relative="0.0001"), ["outside_tolerance"])

    def test_negative_source_uses_magnitude(self):
        self.assertEqual(_eval("-10000.00", "-10000.99", relative="0.0001"), ["within_relative_tolerance"])
        self.assertEqual(_eval("-10000.00", "-10001.01", relative="0.0001"), ["outside_tolerance"])

    def test_both_bounds_must_hold(self):
        # abs 0.01; rel bound 0.0001 * 50.00 = 0.005.
        self.assertEqual(_eval("50.00", "50.01", absolute="0.01", relative="0.0001"), ["outside_tolerance"])
        # rel bound 0.0001 * 100000 = 10; abs 0.01 fails for diff 0.02.
        self.assertEqual(_eval("100000.00", "100000.02", absolute="0.01", relative="0.0001"), ["outside_tolerance"])
        self.assertEqual(
            _eval("100000.00", "100000.01", absolute="0.01", relative="0.0001"),
            ["within_absolute_tolerance", "within_relative_tolerance"],
        )

    def test_precision_trap_instead_of_rounding(self):
        source = Decimal("1" * 42 + "." + "1" * 18)
        target = source + Decimal("0.000000000000000001")
        bounds = ToleranceBounds(None, Decimal("0.0000999999999999999"), "source_float_type")
        self.assertEqual(evaluate_tolerance(source, target, bounds), (("decimal_precision_exceeded", None),))


class ToleranceDecisionTests(unittest.TestCase):
    def _decide(self, document_metric, source, target):
        report = decide(policy(document_metric), evidence(entry(source=source, target=target)))
        return report["metrics"][0]

    def test_within_tolerance_is_its_own_outcome(self):
        row = self._decide(tolerance_metric(absolute="0.01"), "10.00", "10.01")
        self.assertEqual(row["outcome"], "within_tolerance")
        self.assertEqual(row["comparison_mode"], "tolerance")
        self.assertEqual(row["tolerance"], {"absolute": "0.01", "relative": None, "rationale_code": "source_rounding_at_scale"})

    def test_equal_values_under_tolerance_are_equal_exact(self):
        self.assertEqual(self._decide(tolerance_metric(), "10.00", "10.0")["outcome"], "equal_exact")

    def test_rejected_tolerance_is_per_metric(self):
        report = decide(
            policy(tolerance_metric("revenue", absolute="0"), metric("other")),
            evidence(entry("revenue"), entry("other")),
        )
        outcomes = {row["metric_id"]: row["outcome"] for row in report["metrics"]}
        self.assertEqual(outcomes, {"other": "equal_exact", "revenue": "policy_rejected"})
        self.assertEqual(report["rollup"], "policy_rejected")

    def test_precision_trap_is_evidence_invalid(self):
        document_metric = tolerance_metric(absolute=None, relative="0.0000999999999999999", scale=18,
                                           rationale="source_float_type")
        source = "1" * 42 + "." + "1" * 18
        target = "1" * 42 + "." + "1" * 17 + "2"
        row = self._decide(document_metric, source, target)
        self.assertEqual((row["outcome"], row["reasons"][0]["code"]), ("evidence_invalid", "decimal_precision_exceeded"))


if __name__ == "__main__":
    unittest.main()
