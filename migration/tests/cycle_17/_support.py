"""Shared synthetic builders for cycle-17 tests. Not a test module (no ``test_`` prefix)."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

MIGRATION_DIR = Path(__file__).resolve().parents[2]
if str(MIGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(MIGRATION_DIR))

from comparison_policy.schema import ComparisonEvidence, MetricEvidence, SideEvidence  # noqa: E402

PACKAGE_DIR = MIGRATION_DIR / "comparison_policy"
SECRET = "secret_customer_label_x9"


def metric(metric_id="revenue", **overrides):
    """A summed currency metric with an exact comparison unless overridden."""
    base = {
        "metric_id": metric_id,
        "unit": "currency_amount",
        "aggregation": "sum",
        "scale": 2,
        "allow_negative": False,
        "comparison": "exact",
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not _DROP}


def count_metric(metric_id="order_count", **overrides):
    return metric(metric_id, **{"unit": "orders", "aggregation": "count", "scale": 0, **overrides})


def tolerance_metric(metric_id="revenue", absolute="0.01", relative=None, rationale="source_rounding_at_scale", **overrides):
    block = {"absolute": absolute, "relative": relative, "rationale_code": rationale}
    block = {key: value for key, value in block.items() if value is not None}
    return metric(metric_id, comparison="tolerance", tolerance=block, **overrides)


class _Drop:
    def __repr__(self):
        return "DROP"


_DROP = _Drop()
DROP = _DROP


def policy(*metrics, **overrides):
    document = {"version": 1, "evidence_scope": "local_synthetic", "metrics": list(metrics) or [metric()]}
    document.update(overrides)
    return {key: value for key, value in document.items() if value is not _DROP}


def dims(unit="currency_amount", aggregation="sum", **overrides):
    base = {
        "grain": ["order_id"],
        "currency": "AUD",
        "unit": unit,
        "filter": {"status_in": ["complete"], "date_range": {"start": "2024-01-01", "end": "2024-12-31"}},
        "null_policy": "ignore_nulls",
        "aggregation": aggregation,
        "time_basis": "order_created_date",
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not _DROP}


def side(value, dimensions=None, **dim_overrides):
    return SideEvidence(value=value, dimensions=copy.deepcopy(dimensions) if dimensions is not None else dims(**dim_overrides))


def entry(metric_id="revenue", source="10.00", target="10.00", source_dims=None, target_dims=None,
          upstream_status="available", duplicate_grain_rows=0):
    return MetricEvidence(
        metric_id=metric_id,
        source=side(source, source_dims),
        target=side(target, target_dims),
        upstream_status=upstream_status,
        duplicate_grain_rows=duplicate_grain_rows,
    )


def evidence(*entries):
    return ComparisonEvidence(metrics=tuple(entries))


def codes(row_or_reasons):
    reasons = row_or_reasons["reasons"] if isinstance(row_or_reasons, dict) else row_or_reasons
    if reasons and isinstance(reasons[0], dict):
        return {reason["code"] for reason in reasons}
    return {code for code, _ in reasons}
