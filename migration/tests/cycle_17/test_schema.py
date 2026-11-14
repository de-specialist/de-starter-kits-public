"""Card 271: strict versioned policy schema, closed vocabularies, value-free errors, bounds."""

from __future__ import annotations

import ast
import subprocess
import json
import sys
import unittest

from cycle_17._support import DROP, PACKAGE_DIR, SECRET, count_metric, metric, policy, tolerance_metric

from comparison_policy import schema
from comparison_policy.decision import decide
from comparison_policy.schema import (
    MAX_METRICS,
    MAX_POLICY_BYTES,
    PRECEDENCE,
    REASON_CODES,
    REASON_MESSAGES,
    REASON_OUTCOME,
    ComparisonOutcome,
    PolicyError,
    parse_policy,
)

BANNED = ("pass", "ready", "verified", "parity", "equivalent", "cutover")


class PolicyParseTests(unittest.TestCase):
    def assertRejected(self, document, code):
        with self.assertRaises(PolicyError) as ctx:
            parse_policy(document)
        self.assertEqual(ctx.exception.code, code)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertIsNone(ctx.exception.__context__)
        self.assertNotIn(SECRET, str(ctx.exception))
        self.assertNotIn(SECRET, repr(ctx.exception))
        return ctx.exception

    def test_valid_policy_from_dict_and_json_text_and_bytes(self):
        document = policy(metric(), count_metric(), tolerance_metric("net_revenue", relative="0.0001"))
        parsed = parse_policy(document)
        self.assertEqual(parsed, parse_policy(json.dumps(document)))
        self.assertEqual(parsed, parse_policy(json.dumps(document).encode("utf-8")))
        self.assertEqual([m.metric_id for m in parsed.metrics], ["revenue", "order_count", "net_revenue"])
        self.assertEqual(parsed.metrics[2].tolerance.relative, "0.0001")

    def test_unsupported_version(self):
        for version in (0, 2, "1", True, 1.0, None):
            with self.subTest(version=version):
                self.assertRejected(policy(version=version), "policy_version_unsupported")
        self.assertRejected(policy(version=DROP), "policy_required_field_missing")

    def test_newer_version_with_new_fields_is_reported_as_unsupported_version(self):
        self.assertRejected(policy(version=2, new_field=1), "policy_version_unsupported")

    def test_unknown_field_at_every_level(self):
        self.assertRejected(policy(**{SECRET: 1}), "policy_unknown_field")
        self.assertRejected(policy(metric(**{SECRET: 1})), "policy_unknown_field")
        bad_tolerance = tolerance_metric()
        bad_tolerance["tolerance"][SECRET] = "1"
        self.assertRejected(policy(bad_tolerance), "policy_unknown_field")

    def test_duplicate_metric_id(self):
        self.assertRejected(policy(metric("revenue"), count_metric("revenue")), "policy_duplicate_metric")

    def test_duplicate_json_key(self):
        text = '{"version": 1, "version": 1, "evidence_scope": "local_synthetic", "metrics": []}'
        self.assertRejected(text, "policy_duplicate_field")

    def test_missing_required_metric_fields(self):
        for field in ("metric_id", "unit", "aggregation", "scale", "allow_negative"):
            with self.subTest(field=field):
                self.assertRejected(policy(metric(**{field: DROP})), "policy_required_field_missing")
        for field in ("evidence_scope", "metrics"):
            with self.subTest(field=field):
                self.assertRejected(policy(**{field: DROP}), "policy_required_field_missing")

    def test_missing_comparison_is_kept_for_per_metric_rejection_not_defaulted(self):
        parsed = parse_policy(policy(metric(comparison=DROP)))
        self.assertIsNone(parsed.metrics[0].comparison)
        self.assertIsNone(parsed.metrics[0].tolerance)

    def test_bool_and_float_rejected(self):
        cases = [
            policy(metric(scale=True)),
            policy(metric(scale=2.0)),
            policy(metric(allow_negative=0)),
            policy(metric(allow_negative="false")),
            policy(tolerance_metric(absolute=0.01)),
            policy(tolerance_metric(absolute=True)),
            policy(tolerance_metric(rationale=1)),
        ]
        for document in cases:
            with self.subTest(document=document):
                self.assertRejected(document, "policy_malformed")

    def test_json_float_nan_and_infinity_rejected_at_decode(self):
        base = json.dumps(policy(metric()))
        for token in ("2.0", "NaN", "Infinity", "-Infinity", "2e0"):
            with self.subTest(token=token):
                self.assertRejected(base.replace('"scale": 2', f'"scale": {token}'), "policy_malformed")

    def test_closed_vocabularies(self):
        cases = [
            policy(metric(unit="dollars")),
            policy(metric(aggregation="avg")),
            policy(metric(comparison="approximate")),
            policy(metric(metric_id="Revenue")),
            policy(metric(metric_id=SECRET + " with space")),
            policy(metric(metric_id="r" * 65)),
            policy(metric(scale=-1)),
            policy(metric(scale=19)),
            policy(evidence_scope="production"),
            policy(metrics=[]),
            policy(metrics={}),
            [policy()],
            "[]",
        ]
        for document in cases:
            with self.subTest(document=document):
                self.assertRejected(document, "policy_malformed")

    def test_unit_aggregation_and_scale_consistency(self):
        self.assertRejected(policy(metric(unit="orders", aggregation="max", scale=0)), "policy_malformed")
        self.assertRejected(policy(metric(unit="date", aggregation="sum", scale=0)), "policy_malformed")
        self.assertRejected(policy(count_metric(scale=2)), "policy_malformed")
        parse_policy(policy(metric(unit="timestamp", aggregation="max", scale=0)))

    def test_reserved_evidence_scopes_cannot_be_declared(self):
        for scope in ("local_customer_shaped", "ci", "warehouse_isolated"):
            with self.subTest(scope=scope):
                self.assertRejected(policy(evidence_scope=scope), "policy_evidence_scope_unsupported")

    def test_size_bounds(self):
        too_many = policy(*[metric(f"m{i}") for i in range(MAX_METRICS + 1)])
        self.assertRejected(too_many, "policy_limit_exceeded")
        policy_at_limit = policy(*[metric(f"m{i}") for i in range(MAX_METRICS)])
        self.assertEqual(len(parse_policy(policy_at_limit).metrics), MAX_METRICS)
        oversized = json.dumps(policy()) + " " * MAX_POLICY_BYTES
        self.assertRejected(oversized, "policy_limit_exceeded")
        self.assertRejected(json.dumps(policy(metric(scale=10**25))), "policy_limit_exceeded")
        self.assertRejected(policy(tolerance_metric(absolute="0." + "0" * 40 + "1")), "policy_limit_exceeded")

    def test_deep_nesting_is_bounded_and_value_free(self):
        depth = 30000
        text = "[" * depth + "]" * depth
        self.assertRejected(text[: MAX_POLICY_BYTES - 1], "policy_malformed")

    def test_unpaired_surrogate_is_malformed_and_value_free(self):
        for text in ('{"version":1,"x":"\ud800"}', '{"version":1,"' + SECRET + '\udfff":1}', "\ud800"):
            with self.subTest(length=len(text)):
                with self.assertRaises(PolicyError) as ctx:
                    parse_policy(text)
                error = ctx.exception
                self.assertEqual(error.code, "policy_malformed")
                self.assertEqual(str(error), REASON_MESSAGES["policy_malformed"])
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)
                self.assertNotIn(SECRET, str(error).lower())
        report = decide('{"version":1,"x":"\ud800"}', None)
        self.assertEqual(report["report_reasons"], [{"code": "policy_malformed", "dimension": None}])

    def test_metric_id_with_banned_token_rejected(self):
        for metric_id in ("passenger_revenue", "ready_count", "verified_total", "parity", "equivalent_units",
                          "precutover_sum", "bypass"):
            with self.subTest(metric_id=metric_id):
                self.assertRejected(policy(metric(metric_id=metric_id)), "policy_metric_id_reserved")
        self.assertEqual(decide(policy(metric(metric_id="passenger_revenue")), None)["report_reasons"],
                         [{"code": "policy_metric_id_reserved", "dimension": None}])
        parse_policy(policy(metric(metric_id="rider_revenue")))

    def test_banned_tokens_single_source(self):
        from comparison_policy import decision
        self.assertIs(decision.BANNED_OUTPUT_TOKENS, schema.BANNED_OUTPUT_TOKENS)
        self.assertEqual(schema.BANNED_OUTPUT_TOKENS, BANNED)

    def test_invalid_utf8_and_syntax(self):
        self.assertRejected(b"\xff\xfe{}", "policy_malformed")
        self.assertRejected('{"version": 1,', "policy_malformed")
        self.assertRejected("", "policy_malformed")

    def test_secret_in_rejected_values_never_echoed(self):
        documents = [
            policy(metric(unit=SECRET)),
            policy(evidence_scope=SECRET),
            json.dumps(policy(metric(metric_id=SECRET.upper()))),
            '{"version": 1, "' + SECRET + '": 1, "' + SECRET + '": 2}',
        ]
        for document in documents:
            with self.subTest(document=document):
                with self.assertRaises(PolicyError) as ctx:
                    parse_policy(document)
                self.assertNotIn(SECRET, str(ctx.exception).lower())
                self.assertIn(ctx.exception.code, REASON_CODES)


class VocabularyTests(unittest.TestCase):
    def test_precedence_order(self):
        self.assertEqual(
            [outcome.value for outcome in PRECEDENCE],
            [
                "policy_rejected",
                "evidence_invalid",
                "incompatible",
                "evidence_missing",
                "not_equal",
                "within_tolerance",
                "equal_exact",
            ],
        )

    def test_highest_for_every_pair(self):
        for i, first in enumerate(PRECEDENCE):
            for j, second in enumerate(PRECEDENCE):
                with self.subTest(first=first, second=second):
                    self.assertIs(schema.highest([first, second]), PRECEDENCE[min(i, j)])
        self.assertIsNone(schema.highest([]))

    def test_every_reason_has_one_outcome_and_a_fixed_message(self):
        self.assertEqual(set(REASON_MESSAGES), set(REASON_CODES))
        self.assertEqual(set(REASON_OUTCOME.values()), set(ComparisonOutcome))
        for code in REASON_CODES:
            self.assertRegex(code, r"^[a-z][a-z0-9_]*$")
            self.assertNotIn("{", REASON_MESSAGES[code])

    def test_proposal_reason_codes_present(self):
        required = {
            "policy_version_unsupported", "policy_unknown_field", "policy_duplicate_metric",
            "policy_metric_not_declared", "policy_tolerance_invalid",
            "policy_tolerance_not_permitted_for_unit", "policy_tolerance_exceeds_cap",
            "policy_tolerance_rationale_missing", "value_absent_source", "value_absent_target",
            "value_type_not_permitted", "value_not_finite", "value_not_canonical_decimal",
            "value_scale_exceeds_policy", "value_negative_not_permitted", "decimal_precision_exceeded",
            "upstream_metric_unavailable", "upstream_grain_duplicates", "upstream_report_missing",
            "grain_differs", "currency_differs", "unit_differs", "filter_differs", "null_policy_differs",
            "aggregation_differs", "time_basis_differs", "dimension_absent_source",
            "dimension_absent_target", "exact_equal", "not_equal_exact", "within_absolute_tolerance",
            "within_relative_tolerance", "outside_tolerance",
        }
        self.assertLessEqual(required, REASON_CODES)

    def test_no_banned_tokens_in_vocabulary(self):
        words = list(REASON_CODES) + list(REASON_MESSAGES.values()) + [o.value for o in ComparisonOutcome]
        words += list(schema.DIMENSIONS) + list(schema.UNITS) + list(schema.RATIONALE_CODES)
        for word in words:
            for token in BANNED:
                self.assertNotIn(token, word.lower(), word)

    def test_dimension_set_is_closed(self):
        self.assertEqual(
            schema.DIMENSIONS,
            ("grain", "currency", "unit", "filter", "null_policy", "aggregation", "time_basis"),
        )


class ImportIsolationTests(unittest.TestCase):
    ALLOWED = {"__future__", "decimal", "dataclasses", "enum", "re", "typing", "json"}
    FORBIDDEN_NAMES = {"open", "exec", "eval", "__import__", "compile", "input"}

    def test_package_imports_only_allowed_stdlib(self):
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                with self.subTest(module=path.name, line=getattr(node, "lineno", 0)):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            self.assertIn(alias.name.split(".")[0], self.ALLOWED)
                    elif isinstance(node, ast.ImportFrom) and node.level == 0:
                        self.assertIn(node.module.split(".")[0], self.ALLOWED)
                    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                        self.assertNotIn(node.func.id, self.FORBIDDEN_NAMES)

    def test_importing_package_loads_no_loader_or_reconciliation_module(self):
        probe = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import comparison_policy.decision; "
            "names = ('dlt', 'duckdb', 'sqlite3', 'socket', 'acceptance_report', 'reconcile_bronze', "
            "'run_migration', 'evidence_index', '_private_json'); "
            "print(','.join(n for n in names if n in sys.modules))"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", probe, str(PACKAGE_DIR.parent)],
            capture_output=True, text=True, timeout=60, check=True,
        )
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
