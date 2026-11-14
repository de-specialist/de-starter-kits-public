"""Card 323: counters refuse the next unit before exceeding a cap (F1-F4, F9, F10, F12)."""

from __future__ import annotations

import copy
import pickle
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from resource_budget import result as r
from resource_budget.counter import CounterInputError, CounterSet, MonotonicCounter
from resource_budget.result import BudgetExhausted
from resource_budget.schema import Limit, load_envelope


def _counter(limit: int = 10, key: str = "loader.table_rows", unit: str = "rows", **kwargs) -> MonotonicCounter:
    return MonotonicCounter(Limit(key, unit, limit), **kwargs)


class SingleCounterTests(unittest.TestCase):
    def test_f1_exact_cap_is_admitted(self):
        counter = _counter(10)
        counter.admit(4)
        counter.admit(6)
        self.assertEqual(counter.observed, 10)
        self.assertEqual(counter.remaining, 0)
        self.assertFalse(counter.exhausted)
        result = counter.result()
        self.assertEqual(result.outcome, r.WITHIN_OBSERVED_LIMITS)
        self.assertFalse(result.refused)
        self.assertEqual(result.exit_code, 0)

    def test_f2_refuses_before_admitting_and_observed_is_unchanged(self):
        counter = _counter(10)
        counter.admit(9)
        self.assertFalse(counter.would_admit(2))
        self.assertEqual(counter.observed, 9)
        with self.assertRaises(BudgetExhausted) as caught:
            counter.admit(2)
        self.assertEqual(counter.observed, 9)
        result = caught.exception.result
        self.assertEqual(result.outcome, r.OBSERVED_EXHAUSTION)
        dim = result.dimensions[0]
        self.assertEqual((dim.claim_kind, dim.value, dim.status), (r.OBSERVED, 9, r.NEXT_UNIT_REFUSED))
        self.assertLessEqual(dim.value, dim.limit)

    def test_first_unit_above_cap_refuses_with_no_partial_work(self):
        counter = _counter(10)
        with self.assertRaises(BudgetExhausted) as caught:
            counter.admit(11)
        self.assertFalse(caught.exception.result.partial_work_present)
        self.assertEqual(counter.observed, 0)

    def test_f3_bad_increments_are_refused_and_counter_unchanged(self):
        counter = _counter(10)
        counter.admit(3)
        for bad in (-1, 0, True, False, 1.0, "1", None, 2**63, [1]):
            with self.subTest(bad=bad):
                with self.assertRaises(CounterInputError):
                    counter.admit(bad)
                with self.assertRaises(CounterInputError):
                    counter.would_admit(bad)
                self.assertEqual(counter.observed, 3)
                self.assertFalse(counter.exhausted)

    def test_zero_increment_only_when_explicitly_allowed(self):
        counter = _counter(1, allow_zero=True)
        counter.admit(0)
        counter.admit(1)
        counter.admit(0)
        self.assertEqual(counter.observed, 1)
        with self.assertRaises(CounterInputError):
            _counter(1, allow_zero=1)

    def test_f4_no_reset_reassign_copy_or_pickle(self):
        counter = _counter(10)
        counter.admit(5)
        for name in ("reset", "clear", "set_limit"):
            self.assertFalse(hasattr(counter, name))
        for attr, value in (("_observed", 0), ("observed", 0), ("_limit", Limit("loader.table_rows", "rows", 10**6)), ("_exhausted", False)):
            with self.subTest(attr=attr):
                with self.assertRaises(AttributeError):
                    setattr(counter, attr, value)
        with self.assertRaises(AttributeError):
            del counter._observed
        with self.assertRaises(TypeError):
            copy.copy(counter)
        with self.assertRaises(TypeError):
            copy.deepcopy(counter)
        with self.assertRaises(TypeError):
            pickle.dumps(counter)
        self.assertEqual(counter.observed, 5)

    def test_f4_exhaustion_latches_even_for_units_that_would_fit(self):
        counter = _counter(10)
        counter.admit(8)
        with self.assertRaises(BudgetExhausted):
            counter.admit(5)
        self.assertTrue(counter.exhausted)
        self.assertFalse(counter.would_admit(1))
        with self.assertRaises(BudgetExhausted):
            counter.admit(1)
        self.assertEqual(counter.observed, 8)
        self.assertEqual(counter.result().outcome, r.OBSERVED_EXHAUSTION)

    def test_f9_mid_run_exhaustion_reports_partial_work_and_no_cleanup(self):
        counter = _counter(10)
        counter.admit(7)
        with self.assertRaises(BudgetExhausted) as caught:
            counter.admit(4)
        result = caught.exception.result
        self.assertTrue(result.partial_work_present)
        self.assertEqual(result.cleanup, "not_performed_by_budget")
        data = result.to_dict()
        self.assertIs(data["partial_work_present"], True)
        text = repr(data).lower()
        for phrase in ("rolled back", "rollback", "no changes", "reverted"):
            self.assertNotIn(phrase, text)

    def test_f12_exhaustion_exits_non_zero_with_local_hint(self):
        counter = _counter(2, key="install.scratch_files", unit="files")
        counter.admit(2)
        with self.assertRaises(BudgetExhausted) as caught:
            counter.admit(1)
        result = caught.exception.result
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("`install.scratch_files`", result.hint)
        self.assertIn("Raise", result.hint)
        self.assertEqual(str(caught.exception), "Local resource budget refused the next unit.")

    def test_from_envelope_requires_supplied_limit_and_unit(self):
        envelope = load_envelope({"schema_version": 1, "limits": {"loader.table_rows": {"unit": "rows", "limit": 3}}})
        counter = MonotonicCounter.from_envelope(envelope, "loader.table_rows", "rows")
        self.assertEqual(counter.limit.value, 3)
        with self.assertRaises(ValueError):
            MonotonicCounter.from_envelope(envelope, "loader.table_bytes", "bytes")
        with self.assertRaises(ValueError):
            MonotonicCounter.from_envelope(envelope, "loader.table_rows", "bytes")
        with self.assertRaises(CounterInputError):
            MonotonicCounter(3)

    def test_concurrent_admits_never_exceed_cap(self):
        counter = _counter(1000)
        refusals = []

        def worker():
            for _ in range(400):
                try:
                    counter.admit(1)
                except BudgetExhausted:
                    refusals.append(1)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(counter.observed, 1000)
        self.assertEqual(len(refusals), 600)


class CounterSetTests(unittest.TestCase):
    def setUp(self):
        self.rows = _counter(10, key="loader.table_rows", unit="rows")
        self.bytes = _counter(100, key="loader.table_bytes", unit="bytes")
        self.set = CounterSet([self.rows, self.bytes])

    def test_admits_all_dimensions_together(self):
        self.set.admit({"loader.table_rows": 1, "loader.table_bytes": 40})
        self.assertEqual((self.rows.observed, self.bytes.observed), (1, 40))
        self.assertEqual(self.set.result().outcome, r.WITHIN_OBSERVED_LIMITS)

    def test_f10_either_dimension_refuses_independently_and_is_named(self):
        self.set.admit({"loader.table_rows": 1, "loader.table_bytes": 90})
        with self.assertRaises(BudgetExhausted) as caught:
            self.set.admit({"loader.table_rows": 1, "loader.table_bytes": 20})
        self.assertEqual((self.rows.observed, self.bytes.observed), (1, 90))
        result = caught.exception.result
        self.assertEqual(result.refusing_limit_keys, ("loader.table_bytes",))
        statuses = {d.limit_key: d.status for d in result.dimensions}
        self.assertEqual(statuses, {"loader.table_bytes": r.NEXT_UNIT_REFUSED, "loader.table_rows": r.WITHIN_LIMIT})

        rows = _counter(1, key="loader.table_rows", unit="rows")
        other = CounterSet([rows, _counter(100, key="loader.table_bytes", unit="bytes")])
        other.admit({"loader.table_rows": 1, "loader.table_bytes": 1})
        with self.assertRaises(BudgetExhausted) as caught:
            other.admit({"loader.table_rows": 1, "loader.table_bytes": 1})
        self.assertEqual(caught.exception.result.refusing_limit_keys, ("loader.table_rows",))

    def test_f10_both_dimensions_hit_are_both_reported(self):
        with self.assertRaises(BudgetExhausted) as caught:
            self.set.admit({"loader.table_rows": 11, "loader.table_bytes": 101})
        self.assertEqual(set(caught.exception.result.refusing_limit_keys), {"loader.table_rows", "loader.table_bytes"})
        self.assertIn("`loader.table_rows`", caught.exception.result.hint)
        self.assertIn("`loader.table_bytes`", caught.exception.result.hint)
        self.assertEqual((self.rows.observed, self.bytes.observed), (0, 0))

    def test_set_validates_every_amount_before_admitting_any(self):
        with self.assertRaises(CounterInputError):
            self.set.admit({"loader.table_rows": 1, "loader.table_bytes": -5})
        with self.assertRaises(CounterInputError):
            self.set.admit({"loader.unknown_rows": 1})
        with self.assertRaises(CounterInputError):
            self.set.admit({})
        self.assertEqual((self.rows.observed, self.bytes.observed), (0, 0))

    def test_set_rejects_duplicates_copy_and_reassignment(self):
        with self.assertRaises(CounterInputError):
            CounterSet([self.rows, _counter(5)])
        with self.assertRaises(CounterInputError):
            CounterSet([])
        with self.assertRaises(AttributeError):
            self.set._counters = {}
        with self.assertRaises(TypeError):
            copy.deepcopy(self.set)


if __name__ == "__main__":
    unittest.main()
