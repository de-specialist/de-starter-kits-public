"""Injectable monotonic counters that refuse the next unit before a cap is exceeded (card 318).

A counter holds one supplied limit and the ``observed`` total of units actually admitted.
Callers inject counters into their own loops and call ``admit`` before doing each unit of
work:

* The cap is inclusive: reaching exactly the limit is admitted (F1).
* A unit that would take ``observed`` above the limit is refused **before** it is admitted;
  ``observed`` is unchanged and ``BudgetExhausted`` is raised (F2).
* Increments must be plain positive integers (zero only when the counter was built with
  ``allow_zero=True``). Booleans, floats, strings, negatives and values above ``2**63 - 1``
  raise ``CounterInputError`` and leave the counter unchanged (F3).
* There is no reset, no setter, no copy and no pickling. After a refusal the counter stays
  exhausted and refuses every later unit. More capacity requires a new counter built from a
  new limit (F4).
* ``CounterSet`` checks several dimensions atomically: if any would be exceeded, no
  dimension is incremented and the result names every dimension that refused (F10).

Counters never clean up, roll back or retry anything.
"""

from __future__ import annotations

import threading
from typing import Iterable, Mapping

from . import result as r
from .schema import BudgetEnvelope, Limit


class CounterInputError(ValueError):
    """An increment was not an admissible integer. The counter is unchanged."""


class MonotonicCounter:
    """Inclusive, non-resettable counter of admitted units for one supplied limit."""

    __slots__ = ("_limit", "_observed", "_exhausted", "_allow_zero", "_lock")

    def __init__(self, limit: Limit, *, allow_zero: bool = False) -> None:
        if type(limit) is not Limit:
            raise CounterInputError("A counter needs a Limit from a budget envelope.")
        if type(allow_zero) is not bool:
            raise CounterInputError("allow_zero must be a boolean.")
        object.__setattr__(self, "_limit", limit)
        object.__setattr__(self, "_observed", 0)
        object.__setattr__(self, "_exhausted", False)
        object.__setattr__(self, "_allow_zero", allow_zero)
        object.__setattr__(self, "_lock", threading.Lock())

    @classmethod
    def from_envelope(
        cls, envelope: BudgetEnvelope, key: str, unit: str, *, allow_zero: bool = False
    ) -> "MonotonicCounter":
        if type(envelope) is not BudgetEnvelope:
            raise CounterInputError("from_envelope needs a BudgetEnvelope.")
        return cls(envelope.limit(key, unit), allow_zero=allow_zero)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Counters cannot be reassigned or reset.")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Counters cannot be reassigned or reset.")

    def __copy__(self):
        raise TypeError("Counters cannot be copied; build a new counter from a new limit.")

    def __deepcopy__(self, memo):
        raise TypeError("Counters cannot be copied; build a new counter from a new limit.")

    def __reduce_ex__(self, protocol):
        raise TypeError("Counters cannot be pickled.")

    @property
    def limit(self) -> Limit:
        return self._limit

    @property
    def observed(self) -> int:
        return self._observed

    @property
    def remaining(self) -> int:
        return self._limit.value - self._observed

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    def _check_amount(self, amount: object) -> int:
        if not r.is_strict_int(amount):
            raise CounterInputError("Counter increments must be plain integers.")
        if amount < 0 or amount > r.MAX_VALUE:
            raise CounterInputError("Counter increments must be between 0 and 2**63 - 1.")
        if amount == 0 and not self._allow_zero:
            raise CounterInputError("This counter does not accept zero-size increments.")
        return amount

    def _fits(self, amount: int) -> bool:
        return not self._exhausted and amount <= self._limit.value - self._observed

    def would_admit(self, amount: int) -> bool:
        """Check without admitting. Validates ``amount`` like ``admit``."""
        amount = self._check_amount(amount)
        with self._lock:
            return self._fits(amount)

    def admit(self, amount: int = 1) -> None:
        """Admit ``amount`` units or raise ``BudgetExhausted`` without changing ``observed``."""
        amount = self._check_amount(amount)
        with self._lock:
            if not self._fits(amount):
                object.__setattr__(self, "_exhausted", True)
                raise r.BudgetExhausted(
                    r.BudgetResult(r.OBSERVED_EXHAUSTION, (self._report(refused=True),))
                )
            object.__setattr__(self, "_observed", self._observed + amount)

    def _report(self, *, refused: bool) -> r.DimensionReport:
        status = r.NEXT_UNIT_REFUSED if refused else r.WITHIN_LIMIT
        limit = self._limit
        return r.DimensionReport(limit.key, limit.unit, limit.value, r.OBSERVED, self._observed, status)

    def report(self) -> r.DimensionReport:
        with self._lock:
            return self._report(refused=self._exhausted)

    def result(self) -> r.BudgetResult:
        """``within_observed_limits`` for the run so far, or ``observed_exhaustion``."""
        report = self.report()
        outcome = r.OBSERVED_EXHAUSTION if self._exhausted else r.WITHIN_OBSERVED_LIMITS
        return r.BudgetResult(outcome, (report,))


class CounterSet:
    """Several independent counters checked together; no dimension hides another."""

    __slots__ = ("_counters", "_lock")

    def __init__(self, counters: Iterable[MonotonicCounter]) -> None:
        items = tuple(counters)
        if not items or any(type(item) is not MonotonicCounter for item in items):
            raise CounterInputError("A counter set needs one or more counters.")
        by_key = {item.limit.key: item for item in items}
        if len(by_key) != len(items):
            raise CounterInputError("A counter set cannot repeat a limit key.")
        object.__setattr__(self, "_counters", dict(sorted(by_key.items())))
        object.__setattr__(self, "_lock", threading.Lock())

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Counter sets cannot be reassigned or reset.")

    def __copy__(self):
        raise TypeError("Counter sets cannot be copied.")

    def __deepcopy__(self, memo):
        raise TypeError("Counter sets cannot be copied.")

    def __reduce_ex__(self, protocol):
        raise TypeError("Counter sets cannot be pickled.")

    def counter(self, key: str) -> MonotonicCounter:
        found = self._counters.get(key)
        if found is None:
            raise CounterInputError("No counter with that limit key is in this set.")
        return found

    def admit(self, amounts: Mapping[str, int]) -> None:
        """Admit all ``amounts`` or none. Refusal names every dimension that would exceed."""
        if not isinstance(amounts, Mapping) or not amounts:
            raise CounterInputError("admit needs a non-empty mapping of limit key to amount.")
        if any(key not in self._counters for key in amounts):
            raise CounterInputError("admit named a limit key that is not in this set.")
        checked = {key: self._counters[key]._check_amount(amounts[key]) for key in amounts}
        with self._lock:
            for counter in self._counters.values():
                counter._lock.acquire()
            try:
                refused = {
                    key
                    for key, counter in self._counters.items()
                    if counter._exhausted
                    or (key in checked and not counter._fits(checked[key]))
                }
                if refused:
                    for key in refused:
                        object.__setattr__(self._counters[key], "_exhausted", True)
                    reports = tuple(
                        counter._report(refused=counter._exhausted)
                        for counter in self._counters.values()
                    )
                    raise r.BudgetExhausted(r.BudgetResult(r.OBSERVED_EXHAUSTION, reports))
                for key, amount in checked.items():
                    counter = self._counters[key]
                    object.__setattr__(counter, "_observed", counter._observed + amount)
            finally:
                for counter in self._counters.values():
                    counter._lock.release()

    def result(self) -> r.BudgetResult:
        reports = tuple(counter.report() for counter in self._counters.values())
        exhausted = any(counter.exhausted for counter in self._counters.values())
        outcome = r.OBSERVED_EXHAUSTION if exhausted else r.WITHIN_OBSERVED_LIMITS
        return r.BudgetResult(outcome, reports)
