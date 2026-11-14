"""Strict, versioned comparison-policy schema and the closed vocabularies (card 266).

Every enum, reason code and dimension used by the package is declared here; the other modules
import these constants and never re-declare them.

Policy document (version 1)
---------------------------
::

    {
      "version": 1,
      "evidence_scope": "local_synthetic",
      "metrics": [
        {
          "metric_id": "gross_revenue",
          "unit": "currency_amount",
          "aggregation": "sum",
          "scale": 2,
          "allow_negative": false,
          "comparison": "tolerance",
          "tolerance": {"absolute": "0.01", "rationale_code": "source_rounding_at_scale"}
        }
      ]
    }

Structural problems reject the whole document with :class:`PolicyError`: unsupported version,
unknown fields at any level, duplicate ``metric_id``, duplicate JSON keys, missing required
fields, wrong types (including ``bool`` where an int is expected and any ``float``), values
outside the closed vocabularies, or input beyond the size limits. Missing ``comparison`` and
every tolerance-semantics problem are *per-metric* rejections decided later by ``tolerance``
and ``decision``, so one bad tolerance does not hide the other metrics.

Errors carry a closed ``code`` and a fixed message. They never interpolate input values,
keys, labels or paths, and never chain the underlying exception.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


SCHEMA_VERSION = 1

# Bounded parsing limits.
MAX_POLICY_BYTES = 64 * 1024
MAX_METRICS = 256
MAX_SCALE = 18
MAX_TOLERANCE_TEXT = 32
MAX_VALUE_TEXT = 128
MAX_GRAIN_COLUMNS = 16
MAX_FILTER_STATUSES = 64
DECIMAL_PRECISION = 60

IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")

# Tokens that must never appear in decision output, apart from the constant ``cutover_ready``
# key. The fixed vocabulary is checked by tests; the only author-chosen text that reaches the
# output is a policy ``metric_id``, so ``parse_policy`` rejects any ``metric_id`` containing one
# of these as a substring (``policy_metric_id_reserved``). This deliberately also rejects
# innocent names such as ``passenger_count``: rename the metric (for example ``rider_count``).
BANNED_OUTPUT_TOKENS: tuple[str, ...] = ("pass", "ready", "verified", "parity", "equivalent", "cutover")


class ComparisonOutcome(str, Enum):
    """Per-metric and rollup outcome. Listed from highest to lowest precedence."""

    POLICY_REJECTED = "policy_rejected"
    EVIDENCE_INVALID = "evidence_invalid"
    INCOMPATIBLE = "incompatible"
    EVIDENCE_MISSING = "evidence_missing"
    NOT_EQUAL = "not_equal"
    WITHIN_TOLERANCE = "within_tolerance"
    EQUAL_EXACT = "equal_exact"


# Highest precedence first. Structural blockers dominate any numeric agreement (D1 default).
PRECEDENCE: tuple[ComparisonOutcome, ...] = tuple(ComparisonOutcome)
_RANK = {outcome: index for index, outcome in enumerate(PRECEDENCE)}


def highest(outcomes) -> ComparisonOutcome | None:
    """Return the highest-precedence outcome in ``outcomes`` (``None`` when empty)."""
    ranked = sorted(outcomes, key=_RANK.__getitem__)
    return ranked[0] if ranked else None


# Closed reason codes, each owned by exactly one outcome.
_REASONS_BY_OUTCOME: dict[ComparisonOutcome, tuple[str, ...]] = {
    ComparisonOutcome.POLICY_REJECTED: (
        "policy_version_unsupported",
        "policy_unknown_field",
        "policy_duplicate_metric",
        "policy_duplicate_field",
        "policy_metric_id_reserved",
        "policy_metric_not_declared",
        "evidence_metric_not_in_policy",
        "policy_required_field_missing",
        "policy_malformed",
        "policy_limit_exceeded",
        "policy_evidence_scope_unsupported",
        "policy_tolerance_invalid",
        "policy_tolerance_not_permitted_for_unit",
        "policy_tolerance_exceeds_cap",
        "policy_tolerance_rationale_missing",
    ),
    ComparisonOutcome.EVIDENCE_INVALID: (
        "value_type_not_permitted",
        "value_not_finite",
        "value_not_canonical_decimal",
        "value_scale_exceeds_policy",
        "value_negative_not_permitted",
        "decimal_precision_exceeded",
        "dimension_malformed",
        "dimension_unknown",
        "evidence_malformed",
        "evidence_metric_id_invalid",
        "evidence_duplicate_metric",
        "evidence_limit_exceeded",
    ),
    ComparisonOutcome.INCOMPATIBLE: (
        "grain_differs",
        "currency_differs",
        "unit_differs",
        "filter_differs",
        "null_policy_differs",
        "aggregation_differs",
        "time_basis_differs",
    ),
    ComparisonOutcome.EVIDENCE_MISSING: (
        "value_absent_source",
        "value_absent_target",
        "dimension_absent_source",
        "dimension_absent_target",
        "upstream_metric_unavailable",
        "upstream_grain_duplicates",
        "upstream_report_missing",
        "evidence_metric_absent",
        "no_metrics_compared",
    ),
    ComparisonOutcome.NOT_EQUAL: ("not_equal_exact", "outside_tolerance"),
    ComparisonOutcome.WITHIN_TOLERANCE: ("within_absolute_tolerance", "within_relative_tolerance"),
    ComparisonOutcome.EQUAL_EXACT: ("exact_equal",),
}

REASON_OUTCOME: dict[str, ComparisonOutcome] = {
    code: outcome for outcome, codes in _REASONS_BY_OUTCOME.items() for code in codes
}
REASON_CODES = frozenset(REASON_OUTCOME)

# Codes that carry a ``dimension`` field (always from DIMENSIONS, never free text).
DIMENSION_REASON_CODES = frozenset(
    {"dimension_absent_source", "dimension_absent_target", "dimension_malformed"}
)

# Fixed, value-free messages per code.
REASON_MESSAGES: dict[str, str] = {
    "policy_version_unsupported": "The policy version is not supported.",
    "policy_unknown_field": "The policy contains a field outside the schema.",
    "policy_duplicate_metric": "The policy declares the same metric more than once.",
    "policy_duplicate_field": "The policy repeats a field name within one object.",
    "policy_metric_not_declared": "No usable comparison is declared for this metric.",
    "policy_metric_id_reserved": "A policy metric identifier contains a reserved output term.",
    "evidence_metric_not_in_policy": "The evidence contains metrics that the policy does not declare.",
    "policy_required_field_missing": "The policy is missing a required field.",
    "policy_malformed": "The policy has a field of the wrong type or outside its vocabulary.",
    "policy_limit_exceeded": "The policy exceeds a size limit.",
    "policy_evidence_scope_unsupported": "The policy evidence scope is not available in this version.",
    "policy_tolerance_invalid": "The tolerance declaration is not a canonical positive decimal.",
    "policy_tolerance_not_permitted_for_unit": "A tolerance is not permitted for this unit or aggregation.",
    "policy_tolerance_exceeds_cap": "The tolerance exceeds the cap for this schema version.",
    "policy_tolerance_rationale_missing": "The tolerance has no rationale code.",
    "value_type_not_permitted": "A metric value has a type that is not permitted.",
    "value_not_finite": "A metric value is not finite.",
    "value_not_canonical_decimal": "A metric value is not a canonical decimal string.",
    "value_scale_exceeds_policy": "A metric value has more decimal places than the policy scale.",
    "value_negative_not_permitted": "A metric value is negative where the policy forbids it.",
    "decimal_precision_exceeded": "A metric value or computation exceeds the fixed decimal precision.",
    "dimension_malformed": "A compatibility dimension is malformed.",
    "dimension_unknown": "A side declares a dimension outside the closed set.",
    "evidence_malformed": "The evidence structure is malformed.",
    "evidence_metric_id_invalid": "An evidence metric identifier is not a safe identifier.",
    "evidence_duplicate_metric": "The evidence lists the same metric more than once.",
    "evidence_limit_exceeded": "The evidence exceeds a size limit.",
    "grain_differs": "Source and target grains differ.",
    "currency_differs": "Source and target currencies differ.",
    "unit_differs": "Source, target or policy units differ.",
    "filter_differs": "Source and target filters differ.",
    "null_policy_differs": "Source and target null policies differ.",
    "aggregation_differs": "Source, target or policy aggregations differ.",
    "time_basis_differs": "Source and target time bases differ.",
    "value_absent_source": "The source value is absent.",
    "value_absent_target": "The target value is absent.",
    "dimension_absent_source": "The source does not declare a compatibility dimension.",
    "dimension_absent_target": "The target does not declare a compatibility dimension.",
    "upstream_metric_unavailable": "The upstream report marks this metric unavailable.",
    "upstream_grain_duplicates": "The upstream report found duplicate rows at the metric grain.",
    "upstream_report_missing": "No upstream evidence was supplied.",
    "evidence_metric_absent": "The policy declares a metric that the evidence does not contain.",
    "no_metrics_compared": "No metric was compared.",
    "exact_equal": "The values are numerically equal.",
    "not_equal_exact": "The values differ under an exact comparison.",
    "within_absolute_tolerance": "The difference is within the absolute tolerance.",
    "within_relative_tolerance": "The difference is within the relative tolerance.",
    "outside_tolerance": "The difference is outside the declared tolerance.",
}

# Compatibility dimensions (closed set; includes aggregation and time_basis per D6 default).
DIMENSIONS: tuple[str, ...] = (
    "grain",
    "currency",
    "unit",
    "filter",
    "null_policy",
    "aggregation",
    "time_basis",
)

COUNT_UNITS = frozenset({"rows", "orders", "items", "customers", "count"})
AMOUNT_UNITS = frozenset({"currency_amount", "decimal_measure"})
TEMPORAL_UNITS = frozenset({"date", "timestamp"})
UNITS = COUNT_UNITS | AMOUNT_UNITS | TEMPORAL_UNITS

AGGREGATIONS = frozenset({"sum", "count", "count_distinct", "max", "min"})
# Which aggregations each unit family may declare.
UNIT_AGGREGATIONS: dict[str, frozenset[str]] = {
    **{unit: frozenset({"count", "count_distinct", "sum"}) for unit in COUNT_UNITS},
    **{unit: frozenset({"sum", "max", "min"}) for unit in AMOUNT_UNITS},
    **{unit: frozenset({"max", "min"}) for unit in TEMPORAL_UNITS},
}
# Tolerance is only ever permitted for summed amounts; counts and temporal metrics are exact.
TOLERANCE_UNITS = AMOUNT_UNITS
TOLERANCE_AGGREGATIONS = frozenset({"sum"})

NULL_POLICIES = frozenset({"ignore_nulls", "null_as_zero", "reject_nulls"})
CURRENCY_NOT_APPLICABLE = "not_applicable"
RATIONALE_CODES = frozenset({"source_rounding_at_scale", "decimal_scale_conversion", "source_float_type"})
COMPARISON_MODES = frozenset({"exact", "tolerance"})

# Only local synthetic evidence is producible in cycle 17 (D8 default): no policy can declare
# customer or warehouse scope, so no non-synthetic tolerance can be set.
EVIDENCE_SCOPES = frozenset({"local_synthetic"})
RESERVED_EVIDENCE_SCOPES = frozenset({"local_customer_shaped", "ci", "warehouse_isolated"})

UPSTREAM_STATUSES = frozenset({"available", "unavailable"})

# Tolerance caps for schema v1 (D3 default); raising them needs a new schema version.
RELATIVE_TOLERANCE_CAP = "0.0001"  # absolute cap is one unit in the last place: 10**-scale


class PolicyError(ValueError):
    """Value-free policy rejection: a closed ``code`` and a fixed message."""

    def __init__(self, code: str) -> None:
        if code not in REASON_CODES or REASON_OUTCOME[code] is not ComparisonOutcome.POLICY_REJECTED:
            code = "policy_malformed"
        self.code = code
        super().__init__(REASON_MESSAGES[code])

    def __repr__(self) -> str:
        return f"PolicyError({self.code!r})"


@dataclass(frozen=True)
class TolerancePolicy:
    """Raw tolerance declaration; semantics are validated by ``tolerance.resolve_tolerance``."""

    absolute: str | None
    relative: str | None
    rationale_code: str | None


@dataclass(frozen=True)
class MetricPolicy:
    metric_id: str
    unit: str
    aggregation: str
    scale: int
    allow_negative: bool
    comparison: str | None  # None means not declared: a per-metric rejection, never a default
    tolerance: TolerancePolicy | None


@dataclass(frozen=True)
class Policy:
    version: int
    evidence_scope: str
    metrics: tuple[MetricPolicy, ...]

    def metric(self, metric_id: str) -> MetricPolicy | None:
        for metric in self.metrics:
            if metric.metric_id == metric_id:
                return metric
        return None


# Typed evidence input. A converter from the item-224 aggregate report is a follow-up; until
# then callers build these directly. Absent values are ``None``; absent dimensions are missing
# keys. Values are int, finite Decimal, or canonical decimal strings.
@dataclass(frozen=True)
class SideEvidence:
    value: object
    dimensions: Mapping[str, object]


@dataclass(frozen=True)
class MetricEvidence:
    metric_id: str
    source: SideEvidence | None
    target: SideEvidence | None
    upstream_status: str
    duplicate_grain_rows: int


@dataclass(frozen=True)
class ComparisonEvidence:
    metrics: tuple[MetricEvidence, ...]


_TOP_FIELDS = frozenset({"version", "evidence_scope", "metrics"})
_METRIC_FIELDS = frozenset(
    {"metric_id", "unit", "aggregation", "scale", "allow_negative", "comparison", "tolerance"}
)
_METRIC_REQUIRED = frozenset({"metric_id", "unit", "aggregation", "scale", "allow_negative"})
_TOLERANCE_FIELDS = frozenset({"absolute", "relative", "rationale_code"})


def is_identifier(value: object) -> bool:
    return type(value) is str and IDENTIFIER_PATTERN.fullmatch(value) is not None


def _reject_constant(_token: str) -> object:
    raise PolicyError("policy_malformed")


def _reject_float(_token: str) -> object:
    raise PolicyError("policy_malformed")


def _bounded_int(token: str) -> int:
    if len(token) > 20:
        raise PolicyError("policy_limit_exceeded")
    return int(token)


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError("policy_duplicate_field")
        result[key] = value
    return result


def _decode(text: str | bytes) -> object:
    # Encode and decode failures (for example an unpaired surrogate in ``str`` input) are mapped
    # to a fixed code outside the ``except`` block, so the error has no cause or context that
    # could carry the offending character or its offset.
    raw: bytes | None = None
    if type(text) is str:
        try:
            raw = text.encode("utf-8")
        except UnicodeEncodeError:
            pass
    else:
        raw = text
    if raw is None:
        raise PolicyError("policy_malformed")
    if len(raw) > MAX_POLICY_BYTES:
        raise PolicyError("policy_limit_exceeded")
    decoded: str | None = None
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if decoded is None:
        raise PolicyError("policy_malformed")
    error: str | None = None
    try:
        return json.loads(
            decoded,
            object_pairs_hook=_unique_pairs,
            parse_float=_reject_float,
            parse_int=_bounded_int,
            parse_constant=_reject_constant,
        )
    except PolicyError as exc:
        error = exc.code
    except (ValueError, RecursionError):
        error = "policy_malformed"
    raise PolicyError(error)


def _object(value: object, allowed: frozenset[str]) -> dict:
    if type(value) is not dict:
        raise PolicyError("policy_malformed")
    for key in value:
        if type(key) is not str:
            raise PolicyError("policy_malformed")
        if key not in allowed:
            raise PolicyError("policy_unknown_field")
    return value


def _closed(value: object, vocabulary: frozenset[str]) -> str:
    if type(value) is not str or value not in vocabulary:
        raise PolicyError("policy_malformed")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise PolicyError("policy_malformed")
    if len(value) > MAX_TOLERANCE_TEXT:
        raise PolicyError("policy_limit_exceeded")
    return value


def _parse_tolerance(value: object) -> TolerancePolicy:
    block = _object(value, _TOLERANCE_FIELDS)
    rationale = block.get("rationale_code")
    if rationale is not None and type(rationale) is not str:
        raise PolicyError("policy_malformed")
    if rationale is not None and len(rationale) > 64:
        raise PolicyError("policy_limit_exceeded")
    return TolerancePolicy(
        absolute=_optional_text(block.get("absolute")),
        relative=_optional_text(block.get("relative")),
        rationale_code=rationale,
    )


def _parse_metric(value: object) -> MetricPolicy:
    block = _object(value, _METRIC_FIELDS)
    if not _METRIC_REQUIRED <= block.keys():
        raise PolicyError("policy_required_field_missing")
    if not is_identifier(block["metric_id"]):
        raise PolicyError("policy_malformed")
    if any(token in block["metric_id"] for token in BANNED_OUTPUT_TOKENS):
        raise PolicyError("policy_metric_id_reserved")
    unit = _closed(block["unit"], UNITS)
    aggregation = _closed(block["aggregation"], AGGREGATIONS)
    if aggregation not in UNIT_AGGREGATIONS[unit]:
        raise PolicyError("policy_malformed")
    scale = block["scale"]
    if type(scale) is not int or not 0 <= scale <= MAX_SCALE:
        raise PolicyError("policy_malformed")
    if unit not in AMOUNT_UNITS and scale != 0:
        raise PolicyError("policy_malformed")
    allow_negative = block["allow_negative"]
    if type(allow_negative) is not bool:
        raise PolicyError("policy_malformed")
    comparison = block.get("comparison")
    if comparison is not None:
        comparison = _closed(comparison, COMPARISON_MODES)
    tolerance = block.get("tolerance")
    return MetricPolicy(
        metric_id=block["metric_id"],
        unit=unit,
        aggregation=aggregation,
        scale=scale,
        allow_negative=allow_negative,
        comparison=comparison,
        tolerance=None if tolerance is None else _parse_tolerance(tolerance),
    )


def parse_policy(document: object) -> Policy:
    """Parse a policy from JSON text/bytes or an already-decoded ``dict``.

    Raises :class:`PolicyError` (value-free) for any structural problem.
    """
    if type(document) in (str, bytes):
        document = _decode(document)
    if type(document) is not dict:
        raise PolicyError("policy_malformed")
    # Version first, so a newer document is reported as unsupported, not as unknown fields.
    if "version" not in document:
        raise PolicyError("policy_required_field_missing")
    version = document["version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise PolicyError("policy_version_unsupported")
    top = _object(document, _TOP_FIELDS)
    if not {"evidence_scope", "metrics"} <= top.keys():
        raise PolicyError("policy_required_field_missing")
    scope = top["evidence_scope"]
    if type(scope) is not str:
        raise PolicyError("policy_malformed")
    if scope not in EVIDENCE_SCOPES:
        raise PolicyError(
            "policy_evidence_scope_unsupported" if scope in RESERVED_EVIDENCE_SCOPES else "policy_malformed"
        )
    metrics = top["metrics"]
    if type(metrics) is not list or not metrics:
        raise PolicyError("policy_malformed")
    if len(metrics) > MAX_METRICS:
        raise PolicyError("policy_limit_exceeded")
    parsed = tuple(_parse_metric(item) for item in metrics)
    ids = [metric.metric_id for metric in parsed]
    if len(set(ids)) != len(ids):
        raise PolicyError("policy_duplicate_metric")
    return Policy(version=version, evidence_scope=scope, metrics=parsed)
