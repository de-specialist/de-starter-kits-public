"""Explicit absolute and relative Decimal tolerances (card 268).

Rules (schema v1):

* **No defaults.** A metric with no ``comparison`` is rejected (``policy_metric_not_declared``);
  ``comparison: tolerance`` without a usable bound is rejected. An omitted tolerance never
  means zero or "any".
* Tolerances are only permitted for ``currency_amount`` / ``decimal_measure`` metrics with
  ``aggregation: sum``. Counts and temporal metrics are exact only.
* A bound is a canonical positive decimal string ``(0|[1-9][0-9]*)(\\.[0-9]*[1-9])?``; no
  sign, exponent, whitespace, NaN or infinity. ``"0"`` is rejected: use ``comparison: exact``.
* A ``rationale_code`` from the closed list is required.
* Caps: ``absolute <= 10**-scale`` (one unit in the last place) and ``relative <= 0.0001``
  (D3 default). Exceeding a cap rejects the metric policy.

Evaluation is division-free inside a fixed Decimal context with traps; nothing is rounded:

* ``diff = abs(target - source)``
* absolute: ``diff <= absolute``
* relative: ``diff <= relative * abs(source)``; the reference is always the source side (D4
  default). A zero source therefore admits only ``diff == 0``.
* Bounds are inclusive. When both are declared, both must hold (``all_of`` only, D5 default).
"""

from __future__ import annotations

import decimal
import re
from dataclasses import dataclass
from decimal import Decimal

from .exact import Reason, decimal_context
from .schema import (
    MAX_TOLERANCE_TEXT,
    RATIONALE_CODES,
    RELATIVE_TOLERANCE_CAP,
    TOLERANCE_AGGREGATIONS,
    TOLERANCE_UNITS,
    MetricPolicy,
)

_CANONICAL_BOUND = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]*[1-9])?")
_RELATIVE_CAP = Decimal(RELATIVE_TOLERANCE_CAP)


@dataclass(frozen=True)
class ToleranceBounds:
    absolute: Decimal | None
    relative: Decimal | None
    rationale_code: str


def _parse_bound(text: str | None) -> tuple[Decimal | None, str | None]:
    if text is None:
        return None, None
    if type(text) is not str or len(text) > MAX_TOLERANCE_TEXT or _CANONICAL_BOUND.fullmatch(text) is None:
        return None, "policy_tolerance_invalid"
    bound = Decimal(text)
    if bound == 0:
        return None, "policy_tolerance_invalid"
    return bound, None


def resolve_tolerance(metric: MetricPolicy) -> tuple[ToleranceBounds | None, tuple[Reason, ...]]:
    """Return ``(bounds, ())`` for a valid tolerance, ``(None, ())`` for an exact metric, or
    ``(None, reasons)`` with ``policy_*`` reasons when the metric policy is unusable."""
    if metric.comparison is None:
        return None, (("policy_metric_not_declared", None),)
    if metric.comparison == "exact":
        if metric.tolerance is not None:
            return None, (("policy_tolerance_invalid", None),)
        return None, ()

    if metric.unit not in TOLERANCE_UNITS or metric.aggregation not in TOLERANCE_AGGREGATIONS:
        return None, (("policy_tolerance_not_permitted_for_unit", None),)
    block = metric.tolerance
    if block is None or (block.absolute is None and block.relative is None):
        return None, (("policy_tolerance_invalid", None),)

    codes: set[str] = set()
    if block.rationale_code is None:
        codes.add("policy_tolerance_rationale_missing")
    elif block.rationale_code not in RATIONALE_CODES:
        codes.add("policy_tolerance_invalid")
    absolute, absolute_error = _parse_bound(block.absolute)
    relative, relative_error = _parse_bound(block.relative)
    codes.update(code for code in (absolute_error, relative_error) if code)
    if absolute is not None and absolute > Decimal(1).scaleb(-metric.scale):
        codes.add("policy_tolerance_exceeds_cap")
    if relative is not None and relative > _RELATIVE_CAP:
        codes.add("policy_tolerance_exceeds_cap")
    if codes:
        return None, tuple((code, None) for code in sorted(codes))
    return ToleranceBounds(absolute, relative, block.rationale_code), ()


def evaluate_tolerance(source: Decimal, target: Decimal, bounds: ToleranceBounds) -> tuple[Reason, ...]:
    """Compare two validated values under explicit bounds. Never divides, never rounds."""
    try:
        with decimal.localcontext(decimal_context()):
            diff = abs(target - source)
            if diff == 0:
                return (("exact_equal", None),)
            reasons: list[Reason] = []
            holds = True
            if bounds.absolute is not None:
                if diff <= bounds.absolute:
                    reasons.append(("within_absolute_tolerance", None))
                else:
                    holds = False
            if bounds.relative is not None:
                if diff <= bounds.relative * abs(source):
                    reasons.append(("within_relative_tolerance", None))
                else:
                    holds = False
    except decimal.DecimalException:
        return (("decimal_precision_exceeded", None),)
    if not holds:
        return (("outside_tolerance", None),)
    return tuple(reasons)
