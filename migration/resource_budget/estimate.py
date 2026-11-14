"""Conservative pre-run estimates from supplied counts and widths (card 317).

An estimate (claim kind ``estimate``) is an upper bound computed before work starts. It can
refuse work early (``estimate_exceeds_limit``) but never proves that work fits, and it is
never reported as observed.

Rules:

* Inputs are supplied by the caller. ``None`` means unknown. Nothing here reads a source,
  a file, a planned workload unit, an hour or a token budget.
* Unknown propagates: if any factor or part is unknown, the whole estimate is unknown. It is
  never "partially estimated" as a smaller number.
* Every division rounds up. Any intermediate or final value above ``2**63 - 1`` makes the
  estimate unknown (reason ``overflow``) instead of clamping.
* Booleans, floats, strings and negative numbers are refused with ``EstimateInputError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from . import result as r
from .schema import BudgetEnvelope

MAX_PARTS = 4_096

REASON_INPUT_UNKNOWN = "input_unknown"
REASON_OVERFLOW = "overflow"
_REASONS = frozenset({REASON_INPUT_UNKNOWN, REASON_OVERFLOW})


class EstimateInputError(ValueError):
    """A supplied count or width is not a non-negative bounded integer or ``None``."""


@dataclass(frozen=True)
class Estimate:
    """A conservative upper bound in ``unit``; ``value`` is ``None`` when unknown."""

    unit: str
    value: int | None
    unknown_reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.unit) is not str or self.unit not in r.UNITS:
            raise EstimateInputError("Estimate unit is not a supported unit.")
        if self.value is None:
            if self.unknown_reason not in _REASONS:
                raise EstimateInputError("An unknown estimate needs a known reason code.")
            return
        if self.unknown_reason is not None:
            raise EstimateInputError("A known estimate cannot carry an unknown reason.")
        if not r.is_strict_int(self.value) or not 0 <= self.value <= r.MAX_VALUE:
            raise EstimateInputError("Estimate value is not a non-negative bounded integer.")

    @property
    def known(self) -> bool:
        return self.value is not None


def known(unit: str, value: int) -> Estimate:
    _check_count(value)
    return Estimate(unit, value)


def unknown(unit: str, reason: str = REASON_INPUT_UNKNOWN) -> Estimate:
    return Estimate(unit, None, reason)


def _check_count(value: object) -> int | None:
    if value is None:
        return None
    if not r.is_strict_int(value):
        raise EstimateInputError("Estimate inputs must be plain integers or None.")
    if not 0 <= value <= r.MAX_VALUE:
        raise EstimateInputError("Estimate inputs must be between 0 and 2**63 - 1.")
    return value


def _check_parts(parts: Sequence[object]) -> None:
    if len(parts) > MAX_PARTS:
        raise EstimateInputError("Too many estimate inputs.")


def ceil_div(numerator: int, denominator: int) -> int:
    """Integer division rounded up, for non-negative numerator and positive denominator."""
    if _check_count(numerator) is None or _check_count(denominator) is None:
        raise EstimateInputError("ceil_div needs known integers.")
    if denominator == 0:
        raise EstimateInputError("ceil_div denominator must be positive.")
    return -(-numerator // denominator)


def product(unit: str, *factors: int | None) -> Estimate:
    """Multiply supplied counts, e.g. ``rows * bytes_per_row``. Unknown if any is unknown."""
    _check_parts(factors)
    if not factors:
        raise EstimateInputError("product needs at least one factor.")
    values = [_check_count(factor) for factor in factors]
    if any(value is None for value in values):
        return unknown(unit)
    total = 1
    for value in values:
        total *= value
        if total > r.MAX_VALUE:
            return unknown(unit, REASON_OVERFLOW)
    return Estimate(unit, total)


def total(unit: str, *parts: Estimate | int | None) -> Estimate:
    """Add estimates or supplied counts of one unit. Unknown if any part is unknown."""
    _check_parts(parts)
    if not parts:
        raise EstimateInputError("total needs at least one part.")
    values: list[int | None] = []
    overflow = False
    for part in parts:
        if type(part) is Estimate:
            if part.unit != unit:
                raise EstimateInputError("Cannot add estimates with different units.")
            values.append(part.value)
            overflow = overflow or part.unknown_reason == REASON_OVERFLOW
        else:
            values.append(_check_count(part))
    if any(value is None for value in values):
        return unknown(unit, REASON_OVERFLOW if overflow else REASON_INPUT_UNKNOWN)
    running = 0
    for value in values:
        running += value
        if running > r.MAX_VALUE:
            return unknown(unit, REASON_OVERFLOW)
    return Estimate(unit, running)


def scale_up(estimate: Estimate, numerator: int, denominator: int) -> Estimate:
    """Multiply by ``numerator / denominator`` and round up, e.g. a 125/100 safety margin."""
    if type(estimate) is not Estimate:
        raise EstimateInputError("scale_up needs an Estimate.")
    if _check_count(numerator) is None or _check_count(denominator) is None:
        raise EstimateInputError("scale_up needs a known ratio.")
    if denominator == 0:
        raise EstimateInputError("scale_up denominator must be positive.")
    if not estimate.known:
        return estimate
    scaled = estimate.value * numerator
    if scaled > r.MAX_VALUE:
        return unknown(estimate.unit, REASON_OVERFLOW)
    return Estimate(estimate.unit, ceil_div(scaled, denominator))


def table_bytes(
    row_count: int | None,
    column_widths: Sequence[int | None],
    *,
    per_row_overhead: int = 0,
) -> Estimate:
    """Upper bound on bytes for ``row_count`` rows of the supplied maximum column widths.

    ``column_widths`` are the caller's maximum encoded bytes per column. An empty list is
    unknown, not zero: a table with no known columns cannot be bounded.
    """
    _check_count(row_count)
    _check_count(per_row_overhead)
    if type(column_widths) not in (list, tuple):
        raise EstimateInputError("column_widths must be a list or tuple.")
    if not column_widths:
        return unknown("bytes")
    row_width = total("bytes", *column_widths, per_row_overhead)
    return product("bytes", row_count, row_width.value) if row_width.known else row_width


def check_estimates(
    envelope: BudgetEnvelope, estimates: Mapping[str, Estimate]
) -> r.BudgetResult:
    """Compare pre-run estimates with supplied limits.

    Every supplied dimension is reported. Any estimate above its limit gives
    ``estimate_exceeds_limit``; otherwise any unknown estimate gives ``estimate_unknown``
    (refused by default, owner decision D3); otherwise ``estimate_within_limit``, which states
    only that the conservative estimate is within the limit, not that the run will fit.
    """
    if type(envelope) is not BudgetEnvelope:
        raise EstimateInputError("check_estimates needs a BudgetEnvelope.")
    if not isinstance(estimates, Mapping) or not estimates:
        raise EstimateInputError("check_estimates needs a non-empty mapping of estimates.")
    _check_parts(list(estimates))
    if not all(r.is_valid_limit_key(key) for key in estimates):
        raise EstimateInputError("check_estimates keys must be valid limit keys.")
    reports = []
    for key in sorted(estimates):
        estimate = estimates[key]
        if type(estimate) is not Estimate:
            raise EstimateInputError("check_estimates values must be Estimate objects.")
        limit = envelope.limit(key, estimate.unit)
        reports.append(estimate_report(limit.key, limit.unit, limit.value, estimate))
    statuses = {report.status for report in reports}
    if r.EXCEEDS_LIMIT in statuses:
        outcome = r.ESTIMATE_EXCEEDS_LIMIT
    elif r.STATUS_UNKNOWN in statuses:
        outcome = r.ESTIMATE_UNKNOWN
    else:
        outcome = r.ESTIMATE_WITHIN_LIMIT
    return r.BudgetResult(outcome, tuple(reports))


def estimate_report(key: str, unit: str, limit: int, estimate: Estimate) -> r.DimensionReport:
    if not estimate.known:
        return r.DimensionReport(key, unit, limit, r.UNKNOWN, None, r.STATUS_UNKNOWN)
    status = r.EXCEEDS_LIMIT if estimate.value > limit else r.WITHIN_LIMIT
    return r.DimensionReport(key, unit, limit, r.ESTIMATE, estimate.value, status)
