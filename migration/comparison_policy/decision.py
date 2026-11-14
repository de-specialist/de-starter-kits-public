"""Deterministic, value-free decision composition (card 270).

``decide(policy_document, evidence)`` returns a report dict; ``render(report)`` gives its
canonical JSON text (byte-identical for identical inputs).

Per metric, every applicable reason is collected and the outcome is the highest-precedence
outcome among them (``schema.PRECEDENCE``):

``policy_rejected > evidence_invalid > incompatible > evidence_missing > not_equal >
within_tolerance > equal_exact``

Values are compared only when no policy, evidence, compatibility or missing-evidence reason
exists, so missing or incompatible evidence can never become ``equal_exact`` or
``within_tolerance``. The rollup is the highest outcome across metrics and report-level
reasons, with a count per outcome so a lower blocker stays visible. An empty comparison is
never ``equal_exact``.

The output never contains source or target values, differences or ratios (D2 default), only
policy identifiers, closed codes and canonical policy tolerances. Metric identifiers that appear
only in the evidence are never echoed: they raise the report-level reason
``evidence_metric_not_in_policy`` and are counted in ``evidence_metrics_not_in_policy``.
Policy ``metric_id`` values cannot contain a ``BANNED_OUTPUT_TOKENS`` substring (rejected by
``schema.parse_policy``), so the banned-word guarantee holds structurally, not just in tests. ``cutover_ready`` is the
constant ``False``: this module never reads, derives or changes cutover status, and does not
import ``acceptance_report`` or ``reconcile_bronze``.
"""

from __future__ import annotations

import json

from .compatibility import compare_dimensions
from .exact import Reason, check_value, compare_exact
from .schema import (
    BANNED_OUTPUT_TOKENS,  # noqa: F401  re-exported for callers and tests
    DIMENSION_REASON_CODES,
    MAX_METRICS,
    PRECEDENCE,
    REASON_CODES,
    REASON_OUTCOME,
    UPSTREAM_STATUSES,
    ComparisonEvidence,
    ComparisonOutcome,
    MetricEvidence,
    MetricPolicy,
    PolicyError,
    SideEvidence,
    highest,
    is_identifier,
    parse_policy,
)
from .tolerance import evaluate_tolerance, resolve_tolerance

REPORT_KIND = "aggregate_comparison_decision"
LIMITATIONS = (
    "Aggregate agreement covers only the declared metrics; it does not establish row, key or "
    "source-snapshot equality, warehouse behaviour, or migration go-live suitability.",
)


def _sorted_reasons(reasons) -> list[dict[str, str | None]]:
    unique = sorted(set(reasons), key=lambda reason: (reason[0], reason[1] or ""))
    rendered = []
    for code, dimension in unique:
        if code not in REASON_CODES:
            raise ValueError("internal reason code outside the closed set")
        rendered.append({"code": code, "dimension": dimension if code in DIMENSION_REASON_CODES else None})
    return rendered


def _outcome(reasons) -> ComparisonOutcome:
    outcome = highest(REASON_OUTCOME[code] for code, _ in reasons)
    if outcome is None:
        raise ValueError("internal decision produced no reason")
    return outcome


def _row(metric_id: str, reasons, metric: MetricPolicy | None, bounds, scope: str) -> dict:
    tolerance = None
    if bounds is not None and metric is not None and metric.tolerance is not None:
        tolerance = {
            "absolute": metric.tolerance.absolute,
            "relative": metric.tolerance.relative,
            "rationale_code": metric.tolerance.rationale_code,
        }
    return {
        "metric_id": metric_id,
        "outcome": _outcome(reasons).value,
        "reasons": _sorted_reasons(reasons),
        "comparison_mode": None if metric is None else metric.comparison,
        "tolerance": tolerance,
        "evidence_scope": scope,
    }


def _upstream_reasons(entry: MetricEvidence) -> set[Reason]:
    reasons: set[Reason] = set()
    status = entry.upstream_status
    if type(status) is not str or status not in UPSTREAM_STATUSES:
        reasons.add(("evidence_malformed", None))
    elif status == "unavailable":
        reasons.add(("upstream_metric_unavailable", None))
    duplicates = entry.duplicate_grain_rows
    if type(duplicates) is not int or duplicates < 0:
        reasons.add(("evidence_malformed", None))
    elif duplicates > 0:
        reasons.add(("upstream_grain_duplicates", None))
    return reasons


def _evaluate(metric: MetricPolicy, entry: MetricEvidence, scope: str) -> dict:
    bounds, policy_reasons = resolve_tolerance(metric)
    if policy_reasons:
        return _row(metric.metric_id, policy_reasons, metric, None, scope)

    reasons = _upstream_reasons(entry)
    sides = {}
    for side in ("source", "target"):
        evidence = getattr(entry, side)
        if evidence is not None and type(evidence) is not SideEvidence:
            reasons.add(("evidence_malformed", None))
            evidence = None
        sides[side] = evidence
    values = {}
    for side, evidence in sides.items():
        value, value_reasons = check_value(None if evidence is None else evidence.value, side, metric)
        values[side] = value
        reasons.update(value_reasons)
    reasons.update(compare_dimensions(sides["source"], sides["target"], metric))

    if not reasons:
        if bounds is None:
            reasons.update(compare_exact(values["source"], values["target"]))
        else:
            reasons.update(evaluate_tolerance(values["source"], values["target"], bounds))
    return _row(metric.metric_id, reasons, metric, bounds, scope)


def _report(version, scope, report_reasons: set[Reason], rows: list[dict], undeclared: int = 0) -> dict:
    rows = sorted(rows, key=lambda row: row["metric_id"])
    if not rows and not report_reasons:
        report_reasons = {("no_metrics_compared", None)}
    outcomes = [ComparisonOutcome(row["outcome"]) for row in rows]
    outcomes += [REASON_OUTCOME[code] for code, _ in report_reasons]
    counts = {outcome.value: 0 for outcome in PRECEDENCE}
    for row in rows:
        counts[row["outcome"]] += 1
    return {
        "report_kind": REPORT_KIND,
        "policy_version": version,
        "evidence_scope": scope,
        "cutover_ready": False,
        "rollup": highest(outcomes).value,
        "report_reasons": _sorted_reasons(report_reasons),
        "outcome_counts": counts,
        "evidence_metrics_not_in_policy": undeclared,
        "metrics": rows,
        "limitations": list(LIMITATIONS),
    }


def decide(policy_document: object, evidence: ComparisonEvidence | None) -> dict:
    """Decide every metric under the policy. Never raises for bad policy or evidence input."""
    try:
        policy = parse_policy(policy_document)
    except PolicyError as exc:
        return _report(None, None, {(exc.code, None)}, [])
    version, scope = policy.version, policy.evidence_scope

    if evidence is None:
        rows = [_row(m.metric_id, {("upstream_report_missing", None)}, m, None, scope) for m in policy.metrics]
        return _report(version, scope, {("upstream_report_missing", None)}, rows)
    if type(evidence) is not ComparisonEvidence or type(evidence.metrics) is not tuple:
        return _report(version, scope, {("evidence_malformed", None)}, [])
    if len(evidence.metrics) > MAX_METRICS:
        return _report(version, scope, {("evidence_limit_exceeded", None)}, [])

    report_reasons: set[Reason] = set()
    entries: dict[str, MetricEvidence] = {}
    duplicated: set[str] = set()
    undeclared = 0
    for entry in evidence.metrics:
        if type(entry) is not MetricEvidence:
            report_reasons.add(("evidence_malformed", None))
        elif not is_identifier(entry.metric_id):
            report_reasons.add(("evidence_metric_id_invalid", None))
        elif policy.metric(entry.metric_id) is None:
            # Evidence-only identifiers come from the upstream report, not the policy author,
            # so they are counted but never echoed.
            undeclared += 1
        elif entry.metric_id in entries:
            duplicated.add(entry.metric_id)
        else:
            entries[entry.metric_id] = entry

    if undeclared:
        report_reasons.add(("evidence_metric_not_in_policy", None))
    rows = []
    for metric_id in sorted(duplicated):
        report_reasons.add(("evidence_duplicate_metric", None))
        rows.append(_row(metric_id, {("evidence_duplicate_metric", None)}, policy.metric(metric_id), None, scope))
    for metric_id, entry in entries.items():
        if metric_id in duplicated:
            continue
        rows.append(_evaluate(policy.metric(metric_id), entry, scope))
    for metric in policy.metrics:
        if metric.metric_id not in entries:
            rows.append(_row(metric.metric_id, {("evidence_metric_absent", None)}, metric, None, scope))
    return _report(version, scope, report_reasons, rows, undeclared)


def render(report: dict) -> str:
    """Canonical JSON text of a report: sorted keys, fixed separators, ASCII only."""
    return json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
