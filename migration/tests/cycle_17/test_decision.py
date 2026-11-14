"""Card 275: deterministic, value-free decision composition and the evidence-gate matrix."""

from __future__ import annotations

import itertools
import json
import unittest
from decimal import Decimal

from cycle_17._support import DROP, SECRET, dims, entry, evidence, metric, policy, side, tolerance_metric

from comparison_policy.decision import BANNED_OUTPUT_TOKENS, decide, render
from comparison_policy.schema import (
    PRECEDENCE,
    ComparisonEvidence,
    ComparisonOutcome,
    MetricEvidence,
)

BANNED = ("pass", "ready", "verified", "parity", "equivalent", "cutover")


def scenario(outcome: ComparisonOutcome, metric_id: str):
    """Return (policy metric, evidence entry) that yields exactly ``outcome``."""
    if outcome is ComparisonOutcome.POLICY_REJECTED:
        return metric(metric_id, comparison=DROP), entry(metric_id)
    if outcome is ComparisonOutcome.EVIDENCE_INVALID:
        return metric(metric_id), entry(metric_id, source=1.0)
    if outcome is ComparisonOutcome.INCOMPATIBLE:
        return metric(metric_id), entry(metric_id, target_dims=dims(currency="USD"))
    if outcome is ComparisonOutcome.EVIDENCE_MISSING:
        return metric(metric_id), entry(metric_id, target=None)
    if outcome is ComparisonOutcome.NOT_EQUAL:
        return metric(metric_id), entry(metric_id, source="1000.25", target="1003.75")
    if outcome is ComparisonOutcome.WITHIN_TOLERANCE:
        return tolerance_metric(metric_id), entry(metric_id, source="20.00", target="20.01")
    return metric(metric_id), entry(metric_id, source="5", target="5.00")


def scan_banned(text: str) -> list[str]:
    stripped = text.lower().replace('"cutover_ready":false', "")
    return [token for token in BANNED if token in stripped]


class OutcomeScenarioTests(unittest.TestCase):
    def test_each_scenario_yields_its_outcome(self):
        for outcome in PRECEDENCE:
            with self.subTest(outcome=outcome):
                policy_metric, item = scenario(outcome, "m")
                report = decide(policy(policy_metric), evidence(item))
                self.assertEqual(report["metrics"][0]["outcome"], outcome.value)
                self.assertEqual(report["rollup"], outcome.value)

    def test_rollup_truth_table_for_every_pair(self):
        for first, second in itertools.product(PRECEDENCE, repeat=2):
            with self.subTest(first=first, second=second):
                pa, ea = scenario(first, "metric_a")
                pb, eb = scenario(second, "metric_b")
                report = decide(policy(pa, pb), evidence(ea, eb))
                expected = PRECEDENCE[min(PRECEDENCE.index(first), PRECEDENCE.index(second))]
                self.assertEqual(report["rollup"], expected.value)
                counts = {o.value: 0 for o in PRECEDENCE}
                counts[first.value] += 1
                counts[second.value] += 1
                self.assertEqual(report["outcome_counts"], counts)
                self.assertIs(report["cutover_ready"], False)
                self.assertEqual(scan_banned(render(report)), [])


class BlockerDominanceTests(unittest.TestCase):
    BLOCKERS = {
        "incompatible": dict(target_dims=dims(grain=["order_item_id"])),
        "dimension_absent": dict(source_dims=dims(null_policy=DROP)),
        "dimension_malformed": dict(source_dims=dims(currency="aud")),
        "upstream_unavailable": dict(upstream_status="unavailable"),
        "grain_duplicates": dict(duplicate_grain_rows=3),
        "bad_upstream_status": dict(upstream_status="rejected"),
        "bad_duplicate_count": dict(duplicate_grain_rows=True),
    }
    VALUE_PAIRS = {"equal": ("20.00", "20.00"), "within": ("20.00", "20.01"), "outside": ("20.00", "25.00")}

    def test_blockers_never_yield_numeric_agreement(self):
        for (blocker, kwargs), (relation, (source, target)), mode in itertools.product(
            self.BLOCKERS.items(), self.VALUE_PAIRS.items(), ("exact", "tolerance")
        ):
            with self.subTest(blocker=blocker, relation=relation, mode=mode):
                policy_metric = metric() if mode == "exact" else tolerance_metric()
                report = decide(policy(policy_metric), evidence(entry(source=source, target=target, **kwargs)))
                row = report["metrics"][0]
                self.assertNotIn(row["outcome"], {"equal_exact", "within_tolerance", "not_equal"})
                self.assertFalse(
                    {r["code"] for r in row["reasons"]}
                    & {"exact_equal", "not_equal_exact", "within_absolute_tolerance", "within_relative_tolerance",
                       "outside_tolerance"}
                )

    def test_combined_blockers_follow_precedence(self):
        cases = [
            (dict(source=1.5, target_dims=dims(currency="USD")), "evidence_invalid"),
            (dict(target=None, target_dims=dims(currency="USD")), "incompatible"),
            (dict(upstream_status="unavailable", target_dims=dims(unit="decimal_measure")), "incompatible"),
            (dict(target=None, source_dims=dims(grain=DROP)), "evidence_missing"),
        ]
        for kwargs, expected in cases:
            with self.subTest(expected=expected):
                report = decide(policy(metric()), evidence(entry(**kwargs)))
                self.assertEqual(report["metrics"][0]["outcome"], expected)

    def test_policy_rejection_dominates_everything(self):
        report = decide(
            policy(metric(comparison=DROP)),
            evidence(entry(source=1.5, target=None, target_dims=dims(currency="USD"), upstream_status="unavailable")),
        )
        row = report["metrics"][0]
        self.assertEqual((row["outcome"], [r["code"] for r in row["reasons"]]),
                         ("policy_rejected", ["policy_metric_not_declared"]))

    def test_upstream_reasons_are_reported(self):
        row = decide(policy(metric()), evidence(entry(upstream_status="unavailable", duplicate_grain_rows=2)))["metrics"][0]
        self.assertEqual({r["code"] for r in row["reasons"]}, {"upstream_metric_unavailable", "upstream_grain_duplicates"})


class EvidenceShapeTests(unittest.TestCase):
    def test_empty_evidence_is_evidence_missing(self):
        report = decide(policy(metric("a"), metric("b")), evidence())
        self.assertEqual(report["rollup"], "evidence_missing")
        self.assertEqual([r["reasons"][0]["code"] for r in report["metrics"]], ["evidence_metric_absent"] * 2)

    def test_policy_metric_missing_from_evidence(self):
        report = decide(policy(metric("a"), metric("b")), evidence(entry("a")))
        self.assertEqual({r["metric_id"]: r["outcome"] for r in report["metrics"]},
                         {"a": "equal_exact", "b": "evidence_missing"})
        self.assertEqual(report["rollup"], "evidence_missing")

    def test_no_evidence_report(self):
        report = decide(policy(metric()), None)
        self.assertEqual(report["rollup"], "evidence_missing")
        self.assertEqual(report["report_reasons"], [{"code": "upstream_report_missing", "dimension": None}])

    def test_evidence_metric_not_in_policy(self):
        report = decide(policy(metric("a")), evidence(entry("a"), entry("zeta_label")))
        self.assertEqual([r["metric_id"] for r in report["metrics"]], ["a"])
        self.assertEqual(report["rollup"], "policy_rejected")
        self.assertIn({"code": "evidence_metric_not_in_policy", "dimension": None}, report["report_reasons"])
        self.assertEqual(report["evidence_metrics_not_in_policy"], 1)
        self.assertNotIn("zeta", render(report))

    def test_evidence_only_ids_never_echoed_even_when_duplicated(self):
        report = decide(
            policy(metric("a")),
            evidence(entry("a"), entry("acme_corp_revenue"), entry("acme_corp_revenue"), entry("other_label")),
        )
        text = render(report)
        for label in ("acme", "other_label"):
            self.assertNotIn(label, text)
        self.assertEqual(report["evidence_metrics_not_in_policy"], 3)
        self.assertNotIn({"code": "evidence_duplicate_metric", "dimension": None}, report["report_reasons"])
        self.assertEqual(report["metrics"][0]["outcome"], "equal_exact")

    def test_no_undeclared_count_is_zero(self):
        report = decide(policy(metric("a")), evidence(entry("a")))
        self.assertEqual(report["evidence_metrics_not_in_policy"], 0)
        self.assertEqual(decide("not json", None)["evidence_metrics_not_in_policy"], 0)

    def test_duplicate_evidence_metric(self):
        report = decide(policy(metric("a")), evidence(entry("a"), entry("a", target="9.00")))
        self.assertEqual(report["rollup"], "evidence_invalid")
        self.assertEqual(len(report["metrics"]), 1)
        self.assertEqual(report["metrics"][0]["reasons"][0]["code"], "evidence_duplicate_metric")

    def test_unsafe_evidence_metric_id_not_echoed(self):
        report = decide(policy(metric("a")), evidence(entry("a"), entry(SECRET + " Name")))
        self.assertEqual(report["rollup"], "evidence_invalid")
        self.assertIn({"code": "evidence_metric_id_invalid", "dimension": None}, report["report_reasons"])
        self.assertNotIn(SECRET, render(report))

    def test_malformed_evidence_containers(self):
        good = entry()
        for bad in (
            [good],
            ComparisonEvidence(metrics=[good]),
            ComparisonEvidence(metrics=(good, {"metric_id": "revenue"})),
            ComparisonEvidence(metrics=(MetricEvidence("revenue", {"value": "1"}, side("1.00"), "available", 0),)),
        ):
            with self.subTest(bad=type(bad).__name__):
                report = decide(policy(metric()), bad)
                self.assertEqual(report["rollup"], "evidence_invalid")

    def test_evidence_size_bound(self):
        report = decide(policy(metric()), evidence(*[entry(f"m{i}") for i in range(257)]))
        self.assertEqual(report["report_reasons"], [{"code": "evidence_limit_exceeded", "dimension": None}])

    def test_rejected_policy_document(self):
        report = decide(policy(version=2), evidence(entry()))
        self.assertEqual(
            (report["rollup"], report["policy_version"], report["evidence_scope"], report["metrics"]),
            ("policy_rejected", None, None, []),
        )
        self.assertIs(report["cutover_ready"], False)


class OutputContractTests(unittest.TestCase):
    def _mixed(self):
        return (
            policy(metric("revenue"), tolerance_metric("net_revenue", absolute=None, relative="0.0001"), metric("tax"),
                   metric("refunds", allow_negative=True)),
            evidence(
                entry("revenue", source="1000.25", target="1003.75"),
                entry("net_revenue", source="987654.32", target="987654.33"),
                entry("tax", source=Decimal("123.45"), target=None),
                entry("refunds", source="-77.10", target="-77.10"),
            ),
        )

    def test_output_is_byte_identical_and_order_independent(self):
        document, items = self._mixed()
        first = render(decide(document, items))
        self.assertEqual(first, render(decide(document, items)))
        shuffled_policy = dict(document, metrics=list(reversed(document["metrics"])))
        shuffled_evidence = ComparisonEvidence(metrics=tuple(reversed(items.metrics)))
        self.assertEqual(first, render(decide(json.dumps(shuffled_policy), shuffled_evidence)))
        self.assertEqual([m["metric_id"] for m in json.loads(first)["metrics"]],
                         ["net_revenue", "refunds", "revenue", "tax"])

    def test_no_values_differences_or_ratios_in_output(self):
        document, items = self._mixed()
        text = render(decide(document, items))
        for leaked in ("1000.25", "1003.75", "3.50", "3.5", "987654.32", "987654.33", "123.45", "77.10", "77.1",
                       "0.01", "0.0035"):
            self.assertNotIn(leaked, text)
        self.assertIn('"relative":"0.0001"', text)  # the policy tolerance itself is not a data value

    def test_cutover_ready_is_constant_false(self):
        document, items = self._mixed()
        for report in (decide(document, items), decide(document, None), decide({}, None),
                       decide(policy(metric()), evidence(entry()))):
            self.assertIs(report["cutover_ready"], False)

    def test_report_shape(self):
        report = decide(*self._mixed())
        self.assertEqual(
            set(report),
            {"report_kind", "policy_version", "evidence_scope", "cutover_ready", "rollup", "report_reasons",
             "outcome_counts", "evidence_metrics_not_in_policy", "metrics", "limitations"},
        )
        for row in report["metrics"]:
            self.assertEqual(
                set(row), {"metric_id", "outcome", "reasons", "comparison_mode", "tolerance", "evidence_scope"}
            )
            self.assertEqual(row["evidence_scope"], "local_synthetic")
            self.assertEqual(row["reasons"], sorted(row["reasons"], key=lambda r: (r["code"], r["dimension"] or "")))
        self.assertEqual(sum(report["outcome_counts"].values()), len(report["metrics"]))
        self.assertEqual(report["rollup"], "evidence_missing")

    def test_ban_list_scan_over_many_outputs(self):
        self.assertEqual(tuple(BANNED_OUTPUT_TOKENS), BANNED)
        document, items = self._mixed()
        outputs = [decide(document, items), decide(document, None), decide("not json", None),
                   decide(policy(metric()), evidence()), decide(policy(metric()), [1])]
        for outcome in PRECEDENCE:
            pm, item = scenario(outcome, "m")
            outputs.append(decide(policy(pm), evidence(item)))
        outputs.append(decide(policy(metric("a")), evidence(entry("a"), entry("passenger_revenue"))))
        outputs.append(decide(policy(metric("passenger_revenue")), evidence(entry("passenger_revenue"))))
        for report in outputs:
            self.assertEqual(scan_banned(render(report)), [])
            self.assertEqual(render(report).count("cutover"), 1)


if __name__ == "__main__":
    unittest.main()
