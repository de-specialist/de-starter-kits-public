"""Card 325: result objects keep claim kinds distinct and stay value-, path- and cost-free (F9, F11, F12)."""

from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from resource_budget import estimate as est
from resource_budget import result as r
from resource_budget.counter import MonotonicCounter
from resource_budget.disk import check_headroom
from resource_budget.result import BudgetResult, BudgetResultError, DimensionReport
from resource_budget.schema import Limit, load_envelope

PACKAGE_DIR = Path(r.__file__).resolve().parent
SECRET_DIR = "/home/secret_user/customer_exports"
ALLOWED_RESULT_KEYS = {"outcome", "phase", "refused", "exit_code", "partial_work_present", "cleanup", "hint", "dimensions"}
ALLOWED_DIMENSION_KEYS = {"limit_key", "unit", "limit", "claim_kind", "status", "estimate", "observed", "probe"}


def _sample_results() -> list[BudgetResult]:
    envelope = load_envelope(
        {
            "schema_version": 1,
            "limits": {
                "install.scratch_bytes": {"unit": "bytes", "limit": 100},
                "disk.reserved_headroom_bytes": {"unit": "bytes", "limit": 10},
            },
        }
    )
    results = [
        est.check_estimates(envelope, {"install.scratch_bytes": est.known("bytes", 50)}),
        est.check_estimates(envelope, {"install.scratch_bytes": est.known("bytes", 500)}),
        est.check_estimates(envelope, {"install.scratch_bytes": est.unknown("bytes")}),
    ]
    for probe in (lambda d: 1000, lambda d: 20, None, lambda d: (_ for _ in ()).throw(OSError(SECRET_DIR))):
        results.append(
            check_headroom(
                envelope,
                projected_key="install.scratch_bytes",
                projected=est.known("bytes", 50),
                directory=SECRET_DIR,
                probe=probe,
            )
        )
    counter = MonotonicCounter(Limit("loader.table_rows", "rows", 3))
    counter.admit(2)
    results.append(counter.result())
    try:
        counter.admit(5)
    except r.BudgetExhausted as exc:
        results.append(exc.result)
    return results


class SerializationTests(unittest.TestCase):
    def test_every_outcome_is_covered_by_samples(self):
        self.assertEqual({res.outcome for res in _sample_results()}, set(r.OUTCOMES))

    def test_f11_only_allowed_fields_and_json_safe(self):
        for res in _sample_results():
            with self.subTest(outcome=res.outcome):
                data = res.to_dict()
                self.assertEqual(set(data), ALLOWED_RESULT_KEYS)
                json.dumps(data, allow_nan=False)
                for dim in data["dimensions"]:
                    self.assertLessEqual(set(dim), ALLOWED_DIMENSION_KEYS)
                    value_keys = set(dim) & {"estimate", "observed", "probe"}
                    if dim["claim_kind"] == r.UNKNOWN:
                        self.assertEqual(value_keys, set())
                    else:
                        self.assertEqual(value_keys, {dim["claim_kind"]})
                        self.assertIs(type(dim[dim["claim_kind"]]), int)
                    self.assertIs(type(dim["limit"]), int)

    def test_f11_no_paths_exception_text_or_reprs(self):
        for res in _sample_results():
            text = json.dumps(res.to_dict())
            self.assertNotIn(SECRET_DIR, text)
            self.assertNotIn("secret_user", text)
            self.assertNotIn("OSError", text)
            self.assertNotIn("Traceback", text)
            self.assertNotIn("<", text)

    def test_never_rolled_back_or_cost_claims(self):
        forbidden = (
            "rolled back", "rollback", "roll back", "reverted", "no changes", "cleaned up",
            "billed", "billing", "credit", "cost", "currency", "$", "usd", "budget_ok", '"ok"',
            "warehouse", "guarantee",
        )
        for res in _sample_results():
            text = json.dumps(res.to_dict()).lower()
            for phrase in forbidden:
                self.assertNotIn(phrase, text, f"{res.outcome}: {phrase}")
        for template in r._HINTS.values():
            for phrase in forbidden:
                self.assertNotIn(phrase, template.lower())

    def test_cleanup_is_always_not_performed(self):
        for res in _sample_results():
            self.assertEqual(res.cleanup, "not_performed_by_budget")

    def test_f12_refusals_exit_non_zero_with_hint_and_others_exit_zero(self):
        for res in _sample_results():
            with self.subTest(outcome=res.outcome):
                if res.refused:
                    self.assertNotEqual(res.exit_code, 0)
                    self.assertTrue(res.hint)
                    self.assertIn("`", res.hint)
                else:
                    self.assertEqual(res.exit_code, 0)
                    self.assertIsNone(res.hint)

    def test_no_bare_ok_outcome_codes(self):
        for code in r.OUTCOMES | r.STATUSES:
            self.assertNotIn(code, {"ok", "budget_ok", "pass", "success"})


class ConsistencyTests(unittest.TestCase):
    def _dim(self, kind=r.ESTIMATE, value=5, status=r.WITHIN_LIMIT, limit=10, key="install.scratch_bytes"):
        return DimensionReport(key, "bytes", limit, kind, value, status)

    def test_within_observed_limits_cannot_carry_estimates(self):
        with self.assertRaises(BudgetResultError):
            BudgetResult(r.WITHIN_OBSERVED_LIMITS, (self._dim(),))
        with self.assertRaises(BudgetResultError):
            BudgetResult(r.ESTIMATE_WITHIN_LIMIT, (self._dim(kind=r.OBSERVED),))

    def test_estimate_cannot_be_labelled_observed_exhaustion(self):
        with self.assertRaises(BudgetResultError):
            BudgetResult(r.OBSERVED_EXHAUSTION, (self._dim(value=11, status=r.EXCEEDS_LIMIT),))

    def test_unknown_carries_no_value_and_known_needs_int(self):
        with self.assertRaises(BudgetResultError):
            self._dim(kind=r.UNKNOWN, value=0, status=r.STATUS_UNKNOWN)
        for bad in (None, True, 1.0, "5", -1, 2**63):
            with self.subTest(bad=bad):
                with self.assertRaises(BudgetResultError):
                    self._dim(value=bad)

    def test_status_and_value_must_agree(self):
        with self.assertRaises(BudgetResultError):
            self._dim(value=11, status=r.WITHIN_LIMIT)
        with self.assertRaises(BudgetResultError):
            self._dim(value=10, status=r.EXCEEDS_LIMIT)
        with self.assertRaises(BudgetResultError):
            self._dim(kind=r.OBSERVED, value=11, status=r.NEXT_UNIT_REFUSED)
        with self.assertRaises(BudgetResultError):
            self._dim(kind=r.ESTIMATE, status=r.NEXT_UNIT_REFUSED)
        with self.assertRaises(BudgetResultError):
            self._dim(kind=r.LIMIT)

    def test_rejects_unknown_codes_and_bad_shapes(self):
        with self.assertRaises(BudgetResultError):
            BudgetResult("ok", (self._dim(),))
        with self.assertRaises(BudgetResultError):
            BudgetResult(r.ESTIMATE_WITHIN_LIMIT, ())
        with self.assertRaises(BudgetResultError):
            BudgetResult(r.ESTIMATE_WITHIN_LIMIT, [self._dim()])
        with self.assertRaises(BudgetResultError):
            BudgetResult(r.ESTIMATE_WITHIN_LIMIT, (self._dim(), self._dim()))
        with self.assertRaises(BudgetResultError):
            DimensionReport(SECRET_DIR, "bytes", 1, r.ESTIMATE, 0, r.WITHIN_LIMIT)
        with self.assertRaises(BudgetResultError):
            r.BudgetExhausted(BudgetResult(r.ESTIMATE_WITHIN_LIMIT, (self._dim(),)))

    def test_partial_work_is_none_before_run(self):
        res = BudgetResult(r.ESTIMATE_WITHIN_LIMIT, (self._dim(),))
        self.assertIsNone(res.partial_work_present)


class PackageBoundaryTests(unittest.TestCase):
    """Stdlib only; no network, subprocess, deletion, credentials, or loader integration."""

    FORBIDDEN_IMPORTS = {"subprocess", "socket", "urllib", "http", "requests", "ssl", "ftplib", "smtplib", "asyncio", "multiprocessing", "dlt", "duckdb", "run_migration", "getpass", "keyring"}
    FORBIDDEN_CALLS = {"remove", "unlink", "rmtree", "rmdir", "removedirs", "rename", "replace", "system", "popen", "walk", "scandir", "listdir", "glob", "getenv", "environ", "sleep", "makedirs", "mkdir", "open", "write_text", "write_bytes"}

    def test_modules_use_only_allowed_imports_and_calls(self):
        modules = sorted(PACKAGE_DIR.glob("*.py"))
        self.assertEqual({m.name for m in modules}, {"__init__.py", "schema.py", "estimate.py", "counter.py", "disk.py", "result.py"})
        for module in modules:
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom):
                    names = {(node.module or "").split(".")[0]} if node.level == 0 else set()
                else:
                    names = set()
                self.assertFalse(names & self.FORBIDDEN_IMPORTS, f"{module.name}: {names}")
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, self.FORBIDDEN_CALLS, module.name)
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, self.FORBIDDEN_CALLS, module.name)

    def test_run_migration_extraction_budget_is_not_wired_in(self):
        source = (PACKAGE_DIR.parent / "run_migration.py").read_text(encoding="utf-8")
        self.assertIn("class ExtractionBudget", source)
        self.assertNotIn("resource_budget", source)


if __name__ == "__main__":
    unittest.main()
