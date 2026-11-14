"""Comparability of declared source and target semantics (card 269).

Each side declares every dimension in ``schema.DIMENSIONS``: ``grain``, ``currency``,
``unit``, ``filter``, ``null_policy``, ``aggregation`` and ``time_basis`` (the last two are the
D6 default). Semantics are *declared, not inferred*: equal labels are compared, nothing is
guessed from a metric name. Canonical forms:

* ``grain``: non-empty list of unique safe identifiers; order-insensitive (sorted).
* ``currency``: ISO-4217-style ``[A-Z]{3}`` or ``"not_applicable"``.
* ``unit`` / ``aggregation``: closed vocabularies; each side must also agree with the policy.
* ``filter``: dict with optional ``status_in`` (non-empty list of unique identifiers,
  order-insensitive) and ``date_range`` (``{"start", "end"}`` ISO dates, start <= end). ``{}``
  means no filter.
* ``null_policy``: closed vocabulary.
* ``time_basis``: a safe identifier.

A missing key is ``dimension_absent_<side>`` (evidence missing); a present but malformed
declaration is ``dimension_malformed`` (evidence invalid); an unknown key is
``dimension_unknown``. A difference between two well-formed declarations is
``<dimension>_differs`` (incompatible). No declared label is ever echoed.
"""

from __future__ import annotations

import re

from .exact import Reason
from .schema import (
    AGGREGATIONS,
    CURRENCY_NOT_APPLICABLE,
    DIMENSIONS,
    MAX_FILTER_STATUSES,
    MAX_GRAIN_COLUMNS,
    NULL_POLICIES,
    UNITS,
    MetricPolicy,
    SideEvidence,
    is_identifier,
)

_CURRENCY = re.compile(r"[A-Z]{3}")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_FILTER_KEYS = frozenset({"status_in", "date_range"})
_MALFORMED = object()
_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _identifier_set(value: object, limit: int) -> object:
    if type(value) is not list or not value or len(value) > limit:
        return _MALFORMED
    if not all(is_identifier(item) for item in value) or len(set(value)) != len(value):
        return _MALFORMED
    return tuple(sorted(value))


def _iso_date(value: object) -> object:
    if type(value) is not str or _ISO_DATE.fullmatch(value) is None:
        return _MALFORMED
    year, month, day = int(value[0:4]), int(value[5:7]), int(value[8:10])
    if year < 1 or not 1 <= month <= 12:
        return _MALFORMED
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = _DAYS_IN_MONTH[month - 1] + (1 if month == 2 and leap else 0)
    if not 1 <= day <= days:
        return _MALFORMED
    # Fixed-width ISO text orders the same as the calendar date, so the string is canonical.
    return value


def _canonical_filter(value: object) -> object:
    if type(value) is not dict or not all(type(key) is str for key in value):
        return _MALFORMED
    if not value.keys() <= _FILTER_KEYS:
        return _MALFORMED
    statuses: object = None
    if "status_in" in value:
        statuses = _identifier_set(value["status_in"], MAX_FILTER_STATUSES)
        if statuses is _MALFORMED:
            return _MALFORMED
    window: object = None
    if "date_range" in value:
        block = value["date_range"]
        if type(block) is not dict or set(block) != {"start", "end"}:
            return _MALFORMED
        start, end = _iso_date(block["start"]), _iso_date(block["end"])
        if start is _MALFORMED or end is _MALFORMED or start > end:
            return _MALFORMED
        window = (start, end)
    return (statuses, window)


def canonical_dimension(dimension: str, value: object) -> object:
    """Return a comparable canonical form, or the private malformed sentinel."""
    if dimension == "grain":
        return _identifier_set(value, MAX_GRAIN_COLUMNS)
    if dimension == "currency":
        if type(value) is str and (value == CURRENCY_NOT_APPLICABLE or _CURRENCY.fullmatch(value)):
            return value
        return _MALFORMED
    if dimension == "unit":
        return value if type(value) is str and value in UNITS else _MALFORMED
    if dimension == "filter":
        return _canonical_filter(value)
    if dimension == "null_policy":
        return value if type(value) is str and value in NULL_POLICIES else _MALFORMED
    if dimension == "aggregation":
        return value if type(value) is str and value in AGGREGATIONS else _MALFORMED
    if dimension == "time_basis":
        return value if is_identifier(value) else _MALFORMED
    raise ValueError("unknown dimension")


def _side(evidence: SideEvidence | None, side: str) -> tuple[dict[str, object], set[Reason]]:
    reasons: set[Reason] = set()
    declared: dict[str, object] = {}
    dimensions = None if evidence is None else evidence.dimensions
    if dimensions is not None and type(dimensions) is not dict:
        return declared, {("evidence_malformed", None)}
    dimensions = dimensions or {}
    if any(key not in DIMENSIONS for key in dimensions):
        reasons.add(("dimension_unknown", None))
    for dimension in DIMENSIONS:
        if dimension not in dimensions:
            reasons.add((f"dimension_absent_{side}", dimension))
            continue
        canonical = canonical_dimension(dimension, dimensions[dimension])
        if canonical is _MALFORMED:
            reasons.add(("dimension_malformed", dimension))
        else:
            declared[dimension] = canonical
    return declared, reasons


def compare_dimensions(
    source: SideEvidence | None, target: SideEvidence | None, metric: MetricPolicy
) -> tuple[Reason, ...]:
    """Return sorted, de-duplicated compatibility reasons; empty when fully comparable."""
    source_declared, reasons = _side(source, "source")
    target_declared, target_reasons = _side(target, "target")
    reasons |= target_reasons
    expected = {"unit": metric.unit, "aggregation": metric.aggregation}
    for dimension in DIMENSIONS:
        values = [d[dimension] for d in (source_declared, target_declared) if dimension in d]
        if len(values) == 2 and values[0] != values[1]:
            reasons.add((f"{dimension}_differs", None))
        if dimension in expected and any(value != expected[dimension] for value in values):
            reasons.add((f"{dimension}_differs", None))
    return tuple(sorted(reasons, key=lambda reason: (reason[0], reason[1] or "")))
