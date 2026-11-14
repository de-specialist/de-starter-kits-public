"""Card 274: comparability across declared grain, currency, unit, filter, null policy,
aggregation and time basis. Semantics are declared, never inferred."""

from __future__ import annotations

import unittest

from cycle_17._support import DROP, SECRET, dims, entry, evidence, metric, policy, side

from comparison_policy.compatibility import compare_dimensions
from comparison_policy.decision import decide, render
from comparison_policy.schema import DIMENSIONS, SideEvidence, parse_policy

POLICY_METRIC = parse_policy(policy(metric())).metrics[0]

# One alternative well-formed value per dimension.
ALTERNATIVES = {
    "grain": ["order_id", "order_item_id"],
    "currency": "USD",
    "unit": "decimal_measure",
    "filter": {"status_in": ["completed"], "date_range": {"start": "2024-01-01", "end": "2024-12-31"}},
    "null_policy": "null_as_zero",
    "aggregation": "max",
    "time_basis": "order_shipped_date",
}


def _compare(source_dims, target_dims, metric_policy=POLICY_METRIC):
    return compare_dimensions(SideEvidence("1.00", source_dims), SideEvidence("1.00", target_dims), metric_policy)


class CompatibilityTests(unittest.TestCase):
    def test_identical_declarations_are_comparable(self):
        self.assertEqual(_compare(dims(), dims()), ())

    def test_each_dimension_varied_alone(self):
        self.assertEqual(set(ALTERNATIVES), set(DIMENSIONS))
        for dimension, alternative in ALTERNATIVES.items():
            for varied_side in ("source", "target"):
                with self.subTest(dimension=dimension, side=varied_side):
                    varied = dims(**{dimension: alternative})
                    pair = (varied, dims()) if varied_side == "source" else (dims(), varied)
                    self.assertEqual(_compare(*pair), ((f"{dimension}_differs", None),))

    def test_asymmetric_absence_of_each_dimension(self):
        cases = 0
        for dimension in DIMENSIONS:
            for absent_side in ("source", "target"):
                with self.subTest(dimension=dimension, side=absent_side):
                    missing = dims(**{dimension: DROP})
                    pair = (missing, dims()) if absent_side == "source" else (dims(), missing)
                    self.assertEqual(_compare(*pair), ((f"dimension_absent_{absent_side}", dimension),))
                    cases += 1
        self.assertEqual(cases, 14)

    def test_absent_dimension_with_equal_values_is_evidence_missing(self):
        for dimension in DIMENSIONS:
            with self.subTest(dimension=dimension):
                report = decide(policy(metric()), evidence(entry(target_dims=dims(**{dimension: DROP}))))
                row = report["metrics"][0]
                self.assertEqual(row["outcome"], "evidence_missing")
                self.assertEqual(row["reasons"], [{"code": "dimension_absent_target", "dimension": dimension}])

    def test_differing_dimension_with_equal_values_is_incompatible(self):
        for dimension, alternative in ALTERNATIVES.items():
            with self.subTest(dimension=dimension):
                report = decide(policy(metric()), evidence(entry(source_dims=dims(**{dimension: alternative}))))
                self.assertEqual(report["metrics"][0]["outcome"], "incompatible")

    def test_grain_is_order_insensitive(self):
        self.assertEqual(
            _compare(dims(grain=["order_id", "customer_id"]), dims(grain=["customer_id", "order_id"])), ()
        )
        self.assertEqual(
            _compare(dims(grain=["order_id"]), dims(grain=["order_id", "customer_id"])),
            (("grain_differs", None),),
        )

    def test_filter_canonicalisation(self):
        a = {"status_in": ["complete", "shipped"], "date_range": {"start": "2024-01-01", "end": "2024-06-30"}}
        b = {"date_range": {"end": "2024-06-30", "start": "2024-01-01"}, "status_in": ["shipped", "complete"]}
        self.assertEqual(_compare(dims(filter=a), dims(filter=b)), ())
        self.assertEqual(_compare(dims(filter={}), dims(filter={})), ())
        differing = [
            ({}, {"status_in": ["complete"]}),
            ({"status_in": ["complete"]}, {"status_in": ["completed"]}),  # BigQuery vs Snowflake label
            ({"status_in": ["complete"]}, {"status_in": ["complete", "returned"]}),
            ({"status_in": ["cancelled"]}, {"status_in": ["returned"]}),
            (
                {"date_range": {"start": "2024-01-01", "end": "2024-06-30"}},
                {"date_range": {"start": "2024-01-01", "end": "2024-07-01"}},
            ),
        ]
        for left, right in differing:
            with self.subTest(left=left, right=right):
                self.assertEqual(_compare(dims(filter=left), dims(filter=right)), (("filter_differs", None),))

    def test_both_sides_must_agree_with_policy_unit_and_aggregation(self):
        agreeing_but_wrong = dims(unit="orders", aggregation="count")
        self.assertEqual(
            _compare(agreeing_but_wrong, agreeing_but_wrong),
            (("aggregation_differs", None), ("unit_differs", None)),
        )

    def test_malformed_declarations(self):
        malformed = {
            "grain": ["order_id", "order_id"],
            "currency": "aud",
            "unit": "dollars",
            "filter": {"status_in": "complete"},
            "null_policy": "drop_nulls",
            "aggregation": "avg",
            "time_basis": "Order Date",
        }
        for dimension, value in malformed.items():
            with self.subTest(dimension=dimension):
                self.assertEqual(
                    _compare(dims(**{dimension: value}), dims()), (("dimension_malformed", dimension),)
                )
        more_grain = ["order_id", [], "", ["Order_ID"], ["a"] * 17, ("order_id",), None]
        more_filter = [
            {"status_in": []},
            {"status_in": ["complete", "complete"]},
            {"region": ["au"]},
            {"date_range": {"start": "2024-02-30", "end": "2024-03-01"}},
            {"date_range": {"start": "2024-06-01", "end": "2024-01-01"}},
            {"date_range": {"start": "2024-01-01"}},
            {"date_range": {"start": "20240101", "end": "20240102"}},
            "status = complete",
        ]
        for dimension, values in (("grain", more_grain), ("filter", more_filter)):
            for value in values:
                with self.subTest(dimension=dimension, value=value):
                    self.assertEqual(
                        _compare(dims(), dims(**{dimension: value})), (("dimension_malformed", dimension),)
                    )
        self.assertEqual(_compare(dims(currency="not_applicable"), dims(currency="not_applicable")), ())

    def test_malformed_dimension_with_equal_values_is_evidence_invalid(self):
        report = decide(policy(metric()), evidence(entry(target_dims=dims(currency="dollars"))))
        self.assertEqual(report["metrics"][0]["outcome"], "evidence_invalid")

    def test_unknown_dimension_key_is_value_free(self):
        source = dims()
        source[SECRET] = SECRET
        self.assertEqual(_compare(source, dims()), (("dimension_unknown", None),))
        report = decide(policy(metric()), evidence(entry(source_dims=source)))
        self.assertEqual(report["metrics"][0]["outcome"], "evidence_invalid")
        self.assertNotIn(SECRET, render(report))

    def test_declared_labels_never_echoed(self):
        report = decide(
            policy(metric()),
            evidence(entry(source_dims=dims(time_basis=SECRET), target_dims=dims(grain=[SECRET]))),
        )
        self.assertEqual(report["metrics"][0]["outcome"], "incompatible")
        self.assertNotIn(SECRET, render(report))

    def test_non_dict_dimensions_and_missing_side(self):
        self.assertEqual(
            compare_dimensions(SideEvidence("1", [("grain", ["order_id"])]), side("1"), POLICY_METRIC),
            (("evidence_malformed", None),),
        )
        reasons = compare_dimensions(None, side("1"), POLICY_METRIC)
        self.assertEqual(reasons, tuple(sorted(("dimension_absent_source", d) for d in DIMENSIONS)))


if __name__ == "__main__":
    unittest.main()
