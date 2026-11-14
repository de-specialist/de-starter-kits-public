"""Value validation and exact Decimal comparison of aggregates (card 267).

* Absent is not zero: ``None`` yields ``value_absent_<side>``; ``0``, ``Decimal("0")`` and
  ``"0"`` are present values.
* Accepted value types: ``int`` (not ``bool``), finite ``decimal.Decimal``, or a canonical
  decimal string (optional ``-``, no leading zeros, no exponent, sign ``+``, or whitespace).
  ``float`` is rejected outright and never coerced.
* A value may not carry more decimal places than the policy ``scale``, may not be negative
  unless the policy allows it, and may not exceed the fixed precision. Nothing is rounded.
  The scale check is deliberately strict on the *written* exponent: ``"1.000"`` at scale 2 is
  ``value_scale_exceeds_policy`` even though it is numerically exact. This fails closed, so an
  upstream report that pads values beyond the policy scale must be fixed (or the policy scale
  raised) rather than silently normalised; trailing-zero tolerance is an owner decision.
* Equality is numeric: ``Decimal("1.0") == Decimal("1.00")`` and ``-0 == 0``.

Every reason is a ``(code, dimension)`` pair from ``schema.REASON_CODES``; no value is ever
included.
"""

from __future__ import annotations

import decimal
import re
from decimal import Decimal

from .schema import DECIMAL_PRECISION, MAX_VALUE_TEXT, MetricPolicy

Reason = tuple[str, "str | None"]

_CANONICAL_VALUE = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?")
_NON_FINITE_WORDS = frozenset({"nan", "snan", "inf", "infinity"})
# 2**200 is about 1.6e60; anything wider cannot fit the fixed precision.
_MAX_INT_BITS = 200

_TRAPS = (
    decimal.InvalidOperation,
    decimal.DivisionByZero,
    decimal.Overflow,
    decimal.Underflow,
    decimal.Subnormal,
    decimal.Inexact,
    decimal.Rounded,
    decimal.Clamped,
)


def decimal_context() -> decimal.Context:
    """A fresh fixed context: every rounding or overflow raises instead of silently changing a value."""
    return decimal.Context(
        prec=DECIMAL_PRECISION,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
        capitals=1,
        clamp=0,
        flags=[],
        traps=list(_TRAPS),
    )


def check_value(raw: object, side: str, metric: MetricPolicy) -> tuple[Decimal | None, tuple[Reason, ...]]:
    """Validate one side's value against the metric policy.

    Returns ``(value, ())`` for a usable value, otherwise ``(None, reasons)``.
    """
    if side not in ("source", "target"):
        raise ValueError("side must be source or target")
    if raw is None:
        return None, ((f"value_absent_{side}", None),)
    kind = type(raw)
    if kind is int:
        if raw.bit_length() > _MAX_INT_BITS:
            return None, (("decimal_precision_exceeded", None),)
        value = Decimal(raw)
    elif kind is Decimal:
        if not raw.is_finite():
            return None, (("value_not_finite", None),)
        value = raw
    elif kind is str:
        if len(raw) > MAX_VALUE_TEXT:
            return None, (("value_not_canonical_decimal", None),)
        if _CANONICAL_VALUE.fullmatch(raw) is None:
            word = raw.strip().lstrip("+-").lower()
            code = "value_not_finite" if word in _NON_FINITE_WORDS else "value_not_canonical_decimal"
            return None, ((code, None),)
        value = Decimal(raw)
    else:
        # float, bool, and any other type (including int/str/Decimal subclasses).
        return None, (("value_type_not_permitted", None),)

    sign, digits, exponent = value.as_tuple()
    if len(digits) > DECIMAL_PRECISION or value.adjusted() >= DECIMAL_PRECISION:
        return None, (("decimal_precision_exceeded", None),)
    if exponent < 0 and -exponent > metric.scale:
        return None, (("value_scale_exceeds_policy", None),)
    if not metric.allow_negative and value < 0:
        return None, (("value_negative_not_permitted", None),)
    return value, ()


def compare_exact(source: Decimal, target: Decimal) -> tuple[Reason, ...]:
    """Exact numeric comparison of two validated values."""
    try:
        with decimal.localcontext(decimal_context()):
            equal = source.compare(target) == 0
    except decimal.DecimalException:
        return (("decimal_precision_exceeded", None),)
    return (("exact_equal", None),) if equal else (("not_equal_exact", None),)


def evaluate_exact(source_raw: object, target_raw: object, metric: MetricPolicy) -> tuple[Reason, ...]:
    """Validate both values and compare them exactly. Absent and invalid values are never compared."""
    source, source_reasons = check_value(source_raw, "source", metric)
    target, target_reasons = check_value(target_raw, "target", metric)
    if source is None or target is None:
        return source_reasons + target_reasons
    return compare_exact(source, target)
