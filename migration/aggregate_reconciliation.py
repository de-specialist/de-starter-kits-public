#!/usr/bin/env python3
"""Allowlisted aggregate reconciliation across Source, Bronze, Silver, and Gold.

A versioned contract names each metric's logical definition (aggregation, unit, grain,
null and status rules) and maps it to a physical table per layer. The tool reads local
SQLite or DuckDB files only, keeps grain keys and row values in process memory, and writes
an owner-only report that contains counts, statuses, reason codes, and totals that meet a
minimum row threshold and do not differ from another emitted total by a small number of
rows or groups. Physical table and column names appear only through a SHA-256
contract fingerprint. The report never establishes cutover readiness.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import decimal
import hashlib
import json
import re
import sqlite3
import sys
import uuid
from decimal import Decimal
from pathlib import Path

from _private_json import create_private_json


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = ROOT / "run-manifests"

CONTRACT_VERSION = 1
REPORT_VERSION = 1
REPORT_TYPE = "aggregate_reconciliation"
EVIDENCE_SCOPE = "local_only"
DEFAULT_MIN_ROWS_FOR_TOTAL = 10
MAX_CONTRACT_BYTES = 1024 * 1024
FETCH_BATCH = 1000
DUCKDB_LOCKDOWN = {
    "enable_external_access": False,
    "autoload_known_extensions": False,
    "autoinstall_known_extensions": False,
    "python_enable_replacements": False,
    "lock_configuration": True,
}

LAYERS = ("source", "bronze", "silver", "gold")
AGGREGATIONS = ("count_rows", "count_distinct", "sum")
UNITS = ("count", "currency_major", "currency_minor", "quantity")
CURRENCY_UNITS = ("currency_major", "currency_minor")
NULL_MEASURE_RULES = ("ignore", "fail")
NULL_GRAIN_RULES = ("exclude", "group", "fail")
ZERO_VALUE_RULES = ("equivalent_to_absent", "significant")
LAYER_AGGREGATION_MODES = ("direct", "precounted")
FILTER_MODES = ("none", "column", "prefiltered")

# Highest precedence first.
STATUS_PRECEDENCE = ("mismatched", "null_policy_violation", "unavailable", "matched")
LAYER_STATUSES = ("available", "null_policy_violation", "unavailable")
LAYER_REASONS = {
    "available": {"ok"},
    "null_policy_violation": {"null_grain_present", "null_measure_present"},
    "unavailable": {
        "connection_unavailable", "table_unavailable", "column_unavailable", "query_failed",
        "measure_not_numeric", "measure_scale_exceeded", "measure_precision_exceeded",
        "precounted_value_invalid", "distinct_count_not_summable", "grain_value_unsupported",
    },
}
TOTAL_STATUSES = ("emitted", "suppressed_min_rows", "suppressed_small_difference", "not_summable", "unavailable")
ROW_FIELDS = ("scanned", "kept", "excluded_status", "null_measure", "null_grain")
COMPARISON_COUNT_FIELDS = ("groups_matched", "groups_value_mismatch", "groups_missing_right", "groups_extra_right")

_RUN_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,127}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_DECIMAL_TEXT = re.compile(r"^[+-]?[0-9]+(\.[0-9]+)?$")
_DECIMAL_OUT = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")

_DECIMAL_CONTEXT = decimal.Context(
    prec=76, rounding=decimal.ROUND_HALF_EVEN,
    traps=[decimal.Inexact, decimal.InvalidOperation, decimal.Overflow, decimal.DivisionByZero],
)


class _LayerUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- contract


def _require_object(value: object, label: str, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object.")
    unknown = set(value) - required - set(optional)
    if unknown:
        raise ValueError(f"{label} contains unsupported fields.")
    missing = required - set(value)
    if missing:
        raise ValueError(f"{label} is missing required fields: {', '.join(sorted(missing))}.")
    return value


def _choice(value: object, choices: tuple[str, ...], label: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{label} must be one of: {', '.join(choices)}.")
    return value


def _logical_name(value: object, label: str) -> str:
    if not isinstance(value, str) or not _NAME.match(value):
        raise ValueError(f"{label} must be a lowercase snake_case name.")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.match(value):
        raise ValueError(f"{label} must be a plain SQL identifier.")
    return value


def _table_name(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a table identifier.")
    parts = value.split(".")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"{label} must be a table identifier.")
    for part in parts:
        _identifier(part, label)
    return value


def _parse_status_spec(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object.")
    if "prefiltered" in value:
        _require_object(value, label, {"prefiltered"})
        if value["prefiltered"] is not True:
            raise ValueError(f"{label}.prefiltered must be true when present.")
        return {"prefiltered": True}
    _require_object(value, label, {"column"}, {"values"})
    column = _identifier(value["column"], f"{label}.column")
    values = value.get("values")
    if values is None:
        return {"column": column, "values": None}
    if not isinstance(values, dict) or not values:
        raise ValueError(f"{label}.values must be a non-empty object.")
    seen: set[str] = set()
    parsed: dict[str, list[str]] = {}
    for canonical in sorted(values):
        _logical_name(canonical, f"{label}.values key")
        raw_values = values[canonical]
        if (not isinstance(raw_values, list) or not raw_values
                or not all(isinstance(raw, str) and raw for raw in raw_values)):
            raise ValueError(f"{label}.values entries must be non-empty lists of strings.")
        if seen.intersection(raw_values) or len(set(raw_values)) != len(raw_values):
            raise ValueError(f"{label}.values maps a raw status more than once.")
        seen.update(raw_values)
        parsed[canonical] = sorted(raw_values)
    return {"column": column, "values": parsed}


def _parse_layer(value: object, label: str, metric: dict) -> dict:
    spec = _require_object(value, label, {"table", "grain_columns"}, {"measure", "aggregation", "status"})
    table = _table_name(spec["table"], f"{label}.table")
    columns = spec["grain_columns"]
    if not isinstance(columns, list) or len(columns) != len(metric["grain"]):
        raise ValueError(f"{label}.grain_columns must list one column per logical grain dimension.")
    columns = [_identifier(column, f"{label}.grain_columns") for column in columns]
    mode = _choice(spec.get("aggregation", "direct"), LAYER_AGGREGATION_MODES, f"{label}.aggregation")
    measure = spec.get("measure")
    if measure is not None:
        measure = _identifier(measure, f"{label}.measure")
    aggregation = metric["aggregation"]
    if aggregation == "count_rows" and mode == "direct":
        if measure is not None:
            raise ValueError(f"{label} counts rows directly and must not name a measure.")
    elif measure is None:
        raise ValueError(f"{label} requires a measure column.")
    if aggregation == "sum" and mode == "precounted":
        raise ValueError(f"{label}: precounted aggregation applies only to count metrics.")
    status_rule = metric["status_rule"]
    if status_rule is None:
        if spec.get("status") is not None:
            raise ValueError(f"{label}.status is only allowed when the metric has a status_rule.")
        status = None
    else:
        if spec.get("status") is None:
            raise ValueError(f"{label}.status is required because the metric has a status_rule.")
        status = _parse_status_spec(spec["status"], f"{label}.status")
    return {
        "table": table, "grain_columns": columns, "measure": measure,
        "aggregation": mode, "status": status,
    }


def _parse_metric(value: object, index: int) -> dict:
    label = f"metrics[{index}]"
    required = {
        "name", "aggregation", "unit", "scale", "grain", "null_measure", "null_grain",
        "zero_value_groups", "status_rule", "layers",
    }
    spec = _require_object(value, label, required, {"currency"})
    metric: dict = {"name": _logical_name(spec["name"], f"{label}.name")}
    metric["aggregation"] = _choice(spec["aggregation"], AGGREGATIONS, f"{label}.aggregation")
    metric["unit"] = _choice(spec["unit"], UNITS, f"{label}.unit")
    if metric["unit"] in CURRENCY_UNITS:
        currency = spec.get("currency")
        if not isinstance(currency, str) or not _CURRENCY.match(currency):
            raise ValueError(f"{label}.currency must be an ISO 4217 three-letter code.")
        metric["currency"] = currency
    else:
        if spec.get("currency") is not None:
            raise ValueError(f"{label}.currency is only allowed for currency units.")
        metric["currency"] = None
    if metric["aggregation"] in {"count_rows", "count_distinct"} and metric["unit"] != "count":
        raise ValueError(f"{label}: count aggregations require unit count.")
    scale = spec["scale"]
    if isinstance(scale, bool) or not isinstance(scale, int) or not 0 <= scale <= 9:
        raise ValueError(f"{label}.scale must be an integer from 0 to 9.")
    if metric["unit"] == "count" and scale != 0:
        raise ValueError(f"{label}: count units require scale 0.")
    metric["scale"] = scale
    grain = spec["grain"]
    if not isinstance(grain, list):
        raise ValueError(f"{label}.grain must be a list of logical dimension names.")
    metric["grain"] = [_logical_name(dimension, f"{label}.grain") for dimension in grain]
    if len(set(metric["grain"])) != len(metric["grain"]):
        raise ValueError(f"{label}.grain contains a duplicate dimension.")
    metric["null_measure"] = _choice(spec["null_measure"], NULL_MEASURE_RULES, f"{label}.null_measure")
    metric["null_grain"] = _choice(spec["null_grain"], NULL_GRAIN_RULES, f"{label}.null_grain")
    metric["zero_value_groups"] = _choice(spec["zero_value_groups"], ZERO_VALUE_RULES, f"{label}.zero_value_groups")
    rule = spec["status_rule"]
    if rule is None:
        metric["status_rule"] = None
    else:
        rule = _require_object(rule, f"{label}.status_rule", {"name", "include"})
        include = rule["include"]
        if not isinstance(include, list) or not include:
            raise ValueError(f"{label}.status_rule.include must be a non-empty list.")
        include = [_logical_name(status, f"{label}.status_rule.include") for status in include]
        if len(set(include)) != len(include):
            raise ValueError(f"{label}.status_rule.include contains a duplicate status.")
        metric["status_rule"] = {
            "name": _logical_name(rule["name"], f"{label}.status_rule.name"),
            "include": sorted(include),
        }
    layers = spec["layers"]
    if not isinstance(layers, dict) or len(layers) < 2:
        raise ValueError(f"{label}.layers must configure at least two layers.")
    if set(layers) - set(LAYERS):
        raise ValueError(f"{label}.layers may only contain: {', '.join(LAYERS)}.")
    metric["layers"] = {
        name: _parse_layer(layers[name], f"{label}.layers.{name}", metric)
        for name in LAYERS if name in layers
    }
    return metric


def parse_contract(obj: object) -> dict:
    """Validate a contract object and return its normalized form."""
    spec = _require_object(obj, "Contract", {"contract_version", "metrics"}, {"min_rows_for_total"})
    version = spec["contract_version"]
    if isinstance(version, bool) or version != CONTRACT_VERSION:
        raise ValueError(f"Contract version must be {CONTRACT_VERSION}.")
    minimum = spec.get("min_rows_for_total", DEFAULT_MIN_ROWS_FOR_TOTAL)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        raise ValueError("min_rows_for_total must be a positive integer.")
    metrics = spec["metrics"]
    if not isinstance(metrics, list) or not metrics:
        raise ValueError("Contract metrics must be a non-empty list.")
    parsed = [_parse_metric(metric, index) for index, metric in enumerate(metrics)]
    names = [metric["name"] for metric in parsed]
    if len(set(names)) != len(names):
        raise ValueError("Contract metric names must be unique.")
    return {"contract_version": CONTRACT_VERSION, "min_rows_for_total": minimum, "metrics": parsed}


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Contract JSON contains duplicate keys.")
        result[key] = value
    return result


def _reject_constant(_name):
    raise ValueError("Contract JSON contains a non-finite number.")


def load_contract(path: Path) -> dict:
    """Read and validate a JSON contract file, rejecting duplicate keys and unknown fields."""
    with open(path, "rb") as stream:
        raw = stream.read(MAX_CONTRACT_BYTES + 1)
    if len(raw) > MAX_CONTRACT_BYTES:
        raise ValueError("Contract file exceeds the supported size limit.")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Contract file is not valid UTF-8 JSON.") from None
    return parse_contract(value)


def contract_fingerprint(contract: dict) -> str:
    canonical = json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- values


def _quote_table(table: str) -> str:
    return ".".join(f'"{part}"' for part in table.split("."))


def _quote(column: str) -> str:
    return f'"{column}"'


def _to_decimal(value: object) -> Decimal | None:
    """Return a finite Decimal for numeric values or decimal text, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(repr(value))
    elif isinstance(value, str):
        if not _DECIMAL_TEXT.match(value):
            return None
        result = Decimal(value)
    else:
        return None
    return result if result.is_finite() else None


def _fits_scale(value: Decimal, scale: int) -> bool:
    if value.is_zero():
        return True
    return value.normalize(_DECIMAL_CONTEXT).as_tuple().exponent >= -scale


def format_decimal(value: Decimal, scale: int) -> str:
    """Canonical fixed-point text with exactly `scale` fractional digits."""
    if value.is_zero():
        value = Decimal(0)
    return format(value.quantize(Decimal(1).scaleb(-scale), context=_DECIMAL_CONTEXT), "f")


def _canonical_key_part(value: object) -> object:
    """Canonical comparable form of a grain value; kept in memory only."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, Decimal)):
        number = _to_decimal(value)
        if number is None:
            raise _LayerUnavailable("grain_value_unsupported")
        return "0" if number.is_zero() else format(number.normalize(_DECIMAL_CONTEXT), "f")
    if isinstance(value, _dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise _LayerUnavailable("grain_value_unsupported")


def _distinct_part(value: object) -> object:
    try:
        return _canonical_key_part(value)
    except _LayerUnavailable:
        raise _LayerUnavailable("measure_not_numeric") from None


def _raw_status(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (str, int)):
        return str(value)
    return None


# --------------------------------------------------------------------------- scanning


def _empty_layer_result(layer: str, filter_mode: str, mode: str) -> dict:
    return {
        "layer": layer, "status": "unavailable", "reason": "connection_unavailable",
        "filter_mode": filter_mode, "aggregation_mode": mode,
        "rows": None, "groups": None, "total": None, "total_status": "unavailable",
    }


def _status_lookup(status: dict | None) -> tuple[str, dict[str, str] | None]:
    if status is None:
        return "none", None
    if status.get("prefiltered"):
        return "prefiltered", None
    if status["values"] is None:
        return "column", None
    return "column", {raw: canonical for canonical, raws in status["values"].items() for raw in raws}


def _execute(connection, sql: str):
    cursor = connection.execute(sql)
    return cursor if cursor is not None else connection


def _scan_layer(metric: dict, layer: str, spec: dict, connection, min_rows: int):
    """Scan one layer, returning its report entry, in-memory group values, and provisional total."""
    filter_mode, status_map = _status_lookup(spec["status"])
    result = _empty_layer_result(layer, filter_mode, spec["aggregation"])
    if connection is None:
        return result, None, None
    table = _quote_table(spec["table"])
    try:
        probe = _execute(connection, f"SELECT * FROM {table} LIMIT 0")
        available_columns = {str(item[0]).casefold() for item in probe.description or ()}
        probe.fetchall()
    except Exception:
        result["reason"] = "table_unavailable"
        return result, None, None
    columns = list(spec["grain_columns"])
    measure_index = status_index = None
    if spec["measure"] is not None:
        measure_index = len(columns)
        columns.append(spec["measure"])
    if filter_mode == "column":
        status_index = len(columns)
        columns.append(spec["status"]["column"])
    # SQLite silently turns an unknown double-quoted identifier into a string literal,
    # so columns are checked against the probe's description before selecting them.
    if any(column.casefold() not in available_columns for column in columns):
        result["reason"] = "column_unavailable"
        return result, None, None
    select_list = ", ".join(_quote(column) for column in columns) if columns else "1"
    try:
        cursor = _execute(connection, f"SELECT {select_list} FROM {table}")
    except Exception:
        result["reason"] = "column_unavailable"
        return result, None, None

    aggregation = metric["aggregation"]
    mode = spec["aggregation"]
    scale = metric["scale"]
    include = set(metric["status_rule"]["include"]) if metric["status_rule"] else set()
    grain_width = len(spec["grain_columns"])
    rows = dict.fromkeys(ROW_FIELDS, 0)
    violation: str | None = None
    error: str | None = None
    values: dict[tuple, object] = {}
    rows_per_group: dict[tuple, int] = {}
    while True:
        try:
            batch = cursor.fetchmany(FETCH_BATCH)
        except Exception:
            error = error or "query_failed"
            break
        if not batch:
            break
        for row in batch:
            rows["scanned"] += 1
            if status_index is not None:
                raw = _raw_status(row[status_index])
                canonical = raw if status_map is None else status_map.get(raw) if raw is not None else None
                if canonical not in include:
                    rows["excluded_status"] += 1
                    continue
            grain_values = row[:grain_width]
            if any(value is None for value in grain_values):
                if metric["null_grain"] != "group":
                    rows["null_grain"] += 1
                    if metric["null_grain"] == "fail":
                        violation = violation or "null_grain_present"
                    continue
            measure = row[measure_index] if measure_index is not None else None
            if measure_index is not None and measure is None:
                rows["null_measure"] += 1
                if metric["null_measure"] == "fail":
                    violation = violation or "null_measure_present"
                continue
            rows["kept"] += 1
            if error is not None:
                # A data error makes the layer unavailable, but scanning continues so a null-policy
                # violation anywhere in the layer still takes precedence over the error.
                continue
            try:
                key = tuple(_canonical_key_part(value) for value in grain_values)
                if aggregation == "count_rows" and mode == "direct":
                    values[key] = values.get(key, 0) + 1
                elif mode == "precounted":
                    count = _to_decimal(measure)
                    if count is None or count < 0 or count != count.to_integral_value():
                        raise _LayerUnavailable("precounted_value_invalid")
                    rows_per_group[key] = rows_per_group.get(key, 0) + 1
                    if aggregation == "count_distinct" and rows_per_group[key] > 1:
                        raise _LayerUnavailable("distinct_count_not_summable")
                    values[key] = values.get(key, 0) + int(count)
                elif aggregation == "count_distinct":
                    values.setdefault(key, set()).add(_distinct_part(measure))
                else:
                    amount = _to_decimal(measure)
                    if amount is None:
                        raise _LayerUnavailable("measure_not_numeric")
                    if not _fits_scale(amount, scale):
                        raise _LayerUnavailable("measure_scale_exceeded")
                    try:
                        values[key] = _DECIMAL_CONTEXT.add(values.get(key, Decimal(0)), amount)
                    except decimal.DecimalException:
                        raise _LayerUnavailable("measure_precision_exceeded") from None
            except _LayerUnavailable as failure:
                error = failure.reason
                values.clear()
                rows_per_group.clear()

    if violation is not None:
        # Null-policy violations take precedence over data errors within a layer.
        result["rows"] = rows
        result["status"] = "null_policy_violation"
        result["reason"] = violation
        return result, None, None
    if error is not None:
        result["reason"] = error
        return result, None, None
    if aggregation == "count_distinct" and mode == "direct":
        values = {key: len(members) for key, members in values.items()}
    if not metric["grain"] and () not in values:
        values[()] = Decimal(0) if aggregation == "sum" else 0
    result["rows"] = rows
    result["status"] = "available"
    result["reason"] = "ok"
    result["groups"] = len(values)
    summable = aggregation != "count_distinct" or not metric["grain"]
    total = None
    if not summable:
        result["total_status"] = "not_summable"
    elif rows["kept"] < min_rows:
        result["total_status"] = "suppressed_min_rows"
    else:
        # Provisional: reconcile() emits the total only if no small difference would reveal rows.
        total = sum((Decimal(value) for value in values.values()), Decimal(0))
        result["total_status"] = "emitted"
    return result, values, total


# --------------------------------------------------------------------------- comparison


def comparison_pairs(layers) -> list[tuple[str, str]]:
    """Adjacent layer pairs in medallion order, plus first-to-last when not adjacent."""
    present = [layer for layer in LAYERS if layer in set(layers)]
    if len(set(layers)) != len(present):
        raise ValueError("Unknown layer name.")
    pairs = list(zip(present, present[1:]))
    if len(present) > 2:
        pairs.append((present[0], present[-1]))
    return pairs


def _worst(statuses) -> str:
    statuses = set(statuses)
    for status in STATUS_PRECEDENCE:
        if status in statuses:
            return status
    return "matched"


def _comparison_status(left_status: str, right_status: str, counts: dict | None) -> str:
    if "null_policy_violation" in {left_status, right_status}:
        return "null_policy_violation"
    if left_status != "available" or right_status != "available" or counts is None:
        return "unavailable"
    if any(counts[field] for field in COMPARISON_COUNT_FIELDS[1:]):
        return "mismatched"
    return "matched"


def _differing_groups(counts: dict) -> int:
    return counts["groups_value_mismatch"] + counts["groups_missing_right"] + counts["groups_extra_right"]


def _small_difference(difference: int, min_rows: int) -> bool:
    """A nonzero difference below the threshold could let two totals reveal individual rows."""
    return 0 < difference < min_rows


def _compare(left: dict, right: dict, zero_rule: str) -> dict:
    if zero_rule == "equivalent_to_absent":
        left = {key: value for key, value in left.items() if value != 0}
        right = {key: value for key, value in right.items() if value != 0}
    counts = dict.fromkeys(COMPARISON_COUNT_FIELDS, 0)
    for key, value in left.items():
        if key not in right:
            counts["groups_missing_right"] += 1
        elif Decimal(value) == Decimal(right[key]):
            counts["groups_matched"] += 1
        else:
            counts["groups_value_mismatch"] += 1
    counts["groups_extra_right"] = sum(1 for key in right if key not in left)
    return counts


def _definition(metric: dict) -> dict:
    return {
        "aggregation": metric["aggregation"], "unit": metric["unit"], "currency": metric["currency"],
        "scale": metric["scale"], "grain": list(metric["grain"]),
        "null_measure": metric["null_measure"], "null_grain": metric["null_grain"],
        "zero_value_groups": metric["zero_value_groups"],
        "status_rule": None if metric["status_rule"] is None else dict(metric["status_rule"]),
    }


def reconcile(contract: dict, connections: dict, *, run_id: str | None = None) -> dict:
    """Reconcile every metric across its configured layers using local DB connections."""
    contract = parse_contract(contract)
    if run_id is not None and (not isinstance(run_id, str) or not _RUN_ID.match(run_id)):
        raise ValueError("Run ID must be a lowercase RFC 4122 UUID.")
    min_rows = contract["min_rows_for_total"]
    metrics = []
    for metric in contract["metrics"]:
        layer_results = []
        layer_values = {}
        totals = {}
        for layer, spec in metric["layers"].items():
            entry, values, total = _scan_layer(metric, layer, spec, connections.get(layer), min_rows)
            layer_results.append(entry)
            layer_values[layer] = values
            totals[layer] = total
        by_layer = {entry["layer"]: entry for entry in layer_results}
        zero_rule = metric["zero_value_groups"]
        # Two totals are never emitted together when their layers differ by a nonzero number of kept
        # rows or differing groups below min_rows_for_total. Every pair is checked, not just the
        # reported comparisons, so totals from non-adjacent layers cannot be differenced either.
        eligible = [layer for layer in by_layer if totals[layer] is not None]
        conflicts: dict[str, set[str]] = {layer: set() for layer in eligible}
        for position, left in enumerate(eligible):
            for right in eligible[position + 1:]:
                kept_difference = abs(by_layer[left]["rows"]["kept"] - by_layer[right]["rows"]["kept"])
                group_difference = _differing_groups(_compare(layer_values[left], layer_values[right], zero_rule))
                if _small_difference(kept_difference, min_rows) or _small_difference(group_difference, min_rows):
                    conflicts[left].add(right)
                    conflicts[right].add(left)
        # Suppress layers until no two remaining totals conflict: most conflicts first, then the
        # most downstream layer, so a matching upstream pair keeps its (equal) totals.
        unsafe = set()
        while True:
            open_conflicts = {layer: peers - unsafe for layer, peers in conflicts.items() if layer not in unsafe}
            worst = max(open_conflicts, key=lambda layer: (len(open_conflicts[layer]), LAYERS.index(layer)),
                        default=None)
            if worst is None or not open_conflicts[worst]:
                break
            unsafe.add(worst)
        for layer in eligible:
            if layer in unsafe:
                by_layer[layer]["total_status"] = "suppressed_small_difference"
            else:
                by_layer[layer]["total"] = format_decimal(totals[layer], metric["scale"])
        comparisons = []
        for left, right in comparison_pairs(metric["layers"]):
            available = layer_values[left] is not None and layer_values[right] is not None
            counts = _compare(layer_values[left], layer_values[right], zero_rule) if available else None
            status = _comparison_status(by_layer[left]["status"], by_layer[right]["status"], counts)
            comparison = {"left": left, "right": right, "status": status}
            comparison.update(counts if counts is not None else dict.fromkeys(COMPARISON_COUNT_FIELDS))
            comparisons.append(comparison)
        metrics.append({
            "name": metric["name"],
            "definition": _definition(metric),
            "status": _worst(comparison["status"] for comparison in comparisons),
            "layers": layer_results,
            "comparisons": comparisons,
        })
        layer_values.clear()
        totals.clear()
    return {
        "report_type": REPORT_TYPE,
        "report_version": REPORT_VERSION,
        "run_id": run_id,
        "evidence_scope": EVIDENCE_SCOPE,
        "cutover_ready": False,
        "contract_fingerprint": contract_fingerprint(contract),
        "min_rows_for_total": min_rows,
        "status": _worst(metric["status"] for metric in metrics),
        "metrics": metrics,
    }


# --------------------------------------------------------------------------- validation


def _exact_keys(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"Aggregate report {label} has unexpected fields.")
    return value


def _count(value: object, label: str, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Aggregate report {label} must be a nonnegative integer.")
    return value


def _validate_definition(value: object, label: str) -> dict:
    definition = _exact_keys(value, {
        "aggregation", "unit", "currency", "scale", "grain", "null_measure", "null_grain",
        "zero_value_groups", "status_rule",
    }, label)
    _choice(definition["aggregation"], AGGREGATIONS, label)
    _choice(definition["unit"], UNITS, label)
    _choice(definition["null_measure"], NULL_MEASURE_RULES, label)
    _choice(definition["null_grain"], NULL_GRAIN_RULES, label)
    _choice(definition["zero_value_groups"], ZERO_VALUE_RULES, label)
    scale = definition["scale"]
    if isinstance(scale, bool) or not isinstance(scale, int) or not 0 <= scale <= 9:
        raise ValueError(f"Aggregate report {label} has an invalid scale.")
    currency = definition["currency"]
    if (definition["unit"] in CURRENCY_UNITS) != (isinstance(currency, str) and bool(_CURRENCY.match(currency))):
        raise ValueError(f"Aggregate report {label} has an invalid currency.")
    if definition["unit"] not in CURRENCY_UNITS and currency is not None:
        raise ValueError(f"Aggregate report {label} has an invalid currency.")
    grain = definition["grain"]
    if not isinstance(grain, list) or any(not isinstance(item, str) or not _NAME.match(item) for item in grain):
        raise ValueError(f"Aggregate report {label} has an invalid grain.")
    rule = definition["status_rule"]
    if rule is not None:
        rule = _exact_keys(rule, {"name", "include"}, label)
        _logical_name(rule["name"], label)
        if not isinstance(rule["include"], list) or not rule["include"]:
            raise ValueError(f"Aggregate report {label} has an invalid status rule.")
        for status in rule["include"]:
            _logical_name(status, label)
    return definition


def _validate_layer(value: object, label: str, definition: dict, min_rows: int) -> dict:
    entry = _exact_keys(value, {
        "layer", "status", "reason", "filter_mode", "aggregation_mode", "rows", "groups", "total", "total_status",
    }, label)
    _choice(entry["layer"], LAYERS, label)
    status = _choice(entry["status"], LAYER_STATUSES, label)
    if entry["reason"] not in LAYER_REASONS[status]:
        raise ValueError(f"Aggregate report {label} reason does not match its status.")
    filter_mode = _choice(entry["filter_mode"], FILTER_MODES, label)
    if (filter_mode == "none") != (definition["status_rule"] is None):
        raise ValueError(f"Aggregate report {label} filter mode contradicts the status rule.")
    aggregation_mode = _choice(entry["aggregation_mode"], LAYER_AGGREGATION_MODES, label)
    if aggregation_mode == "precounted" and definition["aggregation"] == "sum":
        raise ValueError(f"Aggregate report {label} precounts a sum metric.")
    rows = entry["rows"]
    if rows is not None:
        rows = _exact_keys(rows, set(ROW_FIELDS), f"{label}.rows")
        for field in ROW_FIELDS:
            _count(rows[field], f"{label}.rows.{field}")
        if rows["scanned"] != sum(rows[field] for field in ROW_FIELDS[1:]):
            raise ValueError(f"Aggregate report {label} row counts do not add up.")
        if filter_mode != "column" and rows["excluded_status"]:
            raise ValueError(f"Aggregate report {label} excludes statuses without a status column.")
        if definition["null_grain"] == "group" and rows["null_grain"]:
            raise ValueError(f"Aggregate report {label} excludes null grains under the group rule.")
    if status == "unavailable":
        if rows is not None or entry["groups"] is not None:
            raise ValueError(f"Aggregate report {label} reports counts for an unavailable layer.")
    elif rows is None:
        raise ValueError(f"Aggregate report {label} is missing row counts.")
    if status == "null_policy_violation":
        field = "null_grain" if entry["reason"] == "null_grain_present" else "null_measure"
        rule = definition["null_grain"] if field == "null_grain" else definition["null_measure"]
        if rule != "fail" or not rows[field] or entry["groups"] is not None:
            raise ValueError(f"Aggregate report {label} null policy violation is inconsistent.")
    if status == "available":
        groups = _count(entry["groups"], f"{label}.groups")
        if definition["null_grain"] == "fail" and rows["null_grain"]:
            raise ValueError(f"Aggregate report {label} hides a null grain violation.")
        if definition["null_measure"] == "fail" and rows["null_measure"]:
            raise ValueError(f"Aggregate report {label} hides a null measure violation.")
        if not definition["grain"] and groups != 1:
            raise ValueError(f"Aggregate report {label} total grain must have one group.")
        if groups > rows["kept"] and definition["grain"]:
            raise ValueError(f"Aggregate report {label} has more groups than kept rows.")
    total_status = _choice(entry["total_status"], TOTAL_STATUSES, label)
    if status != "available":
        expected = "unavailable"
    elif definition["aggregation"] == "count_distinct" and definition["grain"]:
        expected = "not_summable"
    elif rows["kept"] < min_rows:
        expected = "suppressed_min_rows"
    else:
        expected = "emitted"
    if total_status != expected and not (expected == "emitted" and total_status == "suppressed_small_difference"):
        raise ValueError(f"Aggregate report {label} total status is inconsistent.")
    total = entry["total"]
    if total_status == "emitted":
        if not isinstance(total, str) or not _DECIMAL_OUT.match(total):
            raise ValueError(f"Aggregate report {label} total is not a canonical decimal string.")
        if format_decimal(Decimal(total), definition["scale"]) != total:
            raise ValueError(f"Aggregate report {label} total is not canonical at the metric scale.")
        if (definition["aggregation"] == "count_rows" and aggregation_mode == "direct"
                and Decimal(total) != rows["kept"]):
            raise ValueError(f"Aggregate report {label} row-count total does not match kept rows.")
    elif total is not None:
        raise ValueError(f"Aggregate report {label} discloses a suppressed total.")
    return entry


def _validate_total_disclosure(by_layer: dict, min_rows: int, label: str) -> None:
    """Emitted totals must not differ by a small nonzero kept-row count; suppression needs a peer."""
    candidates = [entry for entry in by_layer.values()
                  if entry["total_status"] in {"emitted", "suppressed_small_difference"}]
    emitted = [entry for entry in candidates if entry["total_status"] == "emitted"]
    for position, left in enumerate(emitted):
        for right in emitted[position + 1:]:
            if _small_difference(abs(left["rows"]["kept"] - right["rows"]["kept"]), min_rows):
                raise ValueError(f"Aggregate report {label} discloses totals across a small row difference.")
    if any(entry["total_status"] == "suppressed_small_difference" for entry in candidates) and len(candidates) < 2:
        raise ValueError(f"Aggregate report {label} suppresses a total without a comparable layer.")


def validate_report(record: object, *, expected_run_id: str | None = None) -> dict:
    """Validate a report and recompute every comparison, metric, and overall status."""
    report = _exact_keys(record, {
        "report_type", "report_version", "run_id", "evidence_scope", "cutover_ready",
        "contract_fingerprint", "min_rows_for_total", "status", "metrics",
    }, "record")
    if report["report_type"] != REPORT_TYPE or report["report_version"] != REPORT_VERSION:
        raise ValueError("Aggregate report type or version is unsupported.")
    if report["evidence_scope"] != EVIDENCE_SCOPE or report["cutover_ready"] is not False:
        raise ValueError("Aggregate report must be local-only evidence that is not cutover-ready.")
    run_id = report["run_id"]
    if run_id is not None and (not isinstance(run_id, str) or not _RUN_ID.match(run_id)):
        raise ValueError("Aggregate report run ID is invalid.")
    if expected_run_id is not None and run_id != expected_run_id:
        raise ValueError("Aggregate report run ID does not match.")
    if not isinstance(report["contract_fingerprint"], str) or not _SHA256.match(report["contract_fingerprint"]):
        raise ValueError("Aggregate report contract fingerprint is invalid.")
    min_rows = report["min_rows_for_total"]
    if isinstance(min_rows, bool) or not isinstance(min_rows, int) or min_rows < 1:
        raise ValueError("Aggregate report minimum row threshold is invalid.")
    metrics = report["metrics"]
    if not isinstance(metrics, list) or not metrics:
        raise ValueError("Aggregate report must contain metrics.")
    names = set()
    metric_statuses = []
    for index, metric in enumerate(metrics):
        label = f"metrics[{index}]"
        metric = _exact_keys(metric, {"name", "definition", "status", "layers", "comparisons"}, label)
        name = _logical_name(metric["name"], label)
        if name in names:
            raise ValueError("Aggregate report metric names must be unique.")
        names.add(name)
        definition = _validate_definition(metric["definition"], f"{label}.definition")
        layers = metric["layers"]
        if not isinstance(layers, list) or len(layers) < 2:
            raise ValueError(f"Aggregate report {label} must contain at least two layers.")
        by_layer = {}
        for position, entry in enumerate(layers):
            entry = _validate_layer(entry, f"{label}.layers[{position}]", definition, min_rows)
            by_layer[entry["layer"]] = entry
        order = [entry["layer"] for entry in layers]
        if order != [layer for layer in LAYERS if layer in by_layer] or len(by_layer) != len(order):
            raise ValueError(f"Aggregate report {label} layers are duplicated or out of order.")
        _validate_total_disclosure(by_layer, min_rows, label)
        comparisons = metric["comparisons"]
        expected_pairs = comparison_pairs(order)
        if not isinstance(comparisons, list) or len(comparisons) != len(expected_pairs):
            raise ValueError(f"Aggregate report {label} comparisons do not match its layers.")
        comparison_statuses = []
        for position, (comparison, pair) in enumerate(zip(comparisons, expected_pairs)):
            clabel = f"{label}.comparisons[{position}]"
            comparison = _exact_keys(comparison, {"left", "right", "status", *COMPARISON_COUNT_FIELDS}, clabel)
            if (comparison["left"], comparison["right"]) != pair:
                raise ValueError(f"Aggregate report {clabel} compares the wrong layers.")
            left, right = by_layer[pair[0]], by_layer[pair[1]]
            both_available = left["status"] == "available" and right["status"] == "available"
            if both_available:
                counts = {field: _count(comparison[field], f"{clabel}.{field}") for field in COMPARISON_COUNT_FIELDS}
                if (counts["groups_matched"] + counts["groups_value_mismatch"] + counts["groups_missing_right"]
                        > left["groups"]
                        or counts["groups_matched"] + counts["groups_value_mismatch"] + counts["groups_extra_right"]
                        > right["groups"]):
                    raise ValueError(f"Aggregate report {clabel} group counts exceed layer groups.")
                if definition["zero_value_groups"] == "significant" and (
                        counts["groups_matched"] + counts["groups_value_mismatch"] + counts["groups_missing_right"]
                        != left["groups"]
                        or counts["groups_matched"] + counts["groups_value_mismatch"] + counts["groups_extra_right"]
                        != right["groups"]):
                    raise ValueError(f"Aggregate report {clabel} group counts do not cover layer groups.")
            else:
                if any(comparison[field] is not None for field in COMPARISON_COUNT_FIELDS):
                    raise ValueError(f"Aggregate report {clabel} reports counts for an unavailable comparison.")
                counts = None
            if counts is not None and left["total_status"] == right["total_status"] == "emitted":
                if _small_difference(_differing_groups(counts), min_rows):
                    raise ValueError(f"Aggregate report {clabel} discloses totals across a small group difference.")
                if not _differing_groups(counts) and left["total"] != right["total"]:
                    raise ValueError(f"Aggregate report {clabel} matched groups with different totals.")
            expected_status = _comparison_status(left["status"], right["status"], counts)
            if comparison["status"] != expected_status:
                raise ValueError(f"Aggregate report {clabel} status does not match its counts.")
            comparison_statuses.append(expected_status)
        expected_metric = _worst(comparison_statuses)
        if metric["status"] != expected_metric:
            raise ValueError(f"Aggregate report {label} status does not match its comparisons.")
        metric_statuses.append(expected_metric)
    if report["status"] != _worst(metric_statuses):
        raise ValueError("Aggregate report status does not match its metrics.")
    return report


# --------------------------------------------------------------------------- CLI


def report_filename(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
        raise ValueError("Run ID must be a lowercase RFC 4122 UUID.")
    return f"{run_id}.aggregate-reconciliation.local.json"


def _layer_argument(value: str) -> tuple[str, Path]:
    layer, separator, path = value.partition("=")
    if not separator or layer not in LAYERS or not path:
        raise argparse.ArgumentTypeError(f"--layer must be LAYER=PATH with LAYER one of: {', '.join(LAYERS)}.")
    return layer, Path(path)


def _open_local(path: Path):
    """Open an existing local SQLite or DuckDB file read-only; return None when unavailable."""
    if not path.is_file():
        return None
    suffix = path.suffix.lower()
    try:
        if suffix == ".duckdb":
            import duckdb

            # Views stored in a layer file are evaluated on read, so file-system access, extension
            # autoload/autoinstall, and Python replacement scans are disabled and the configuration
            # is locked before any query runs. A view that reads files or the network then fails
            # as an unavailable layer instead of being scanned.
            return duckdb.connect(str(path), read_only=True, config=DUCKDB_LOCKDOWN)
        if suffix in {".sqlite", ".sqlite3", ".db"}:
            return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except Exception:
        return None
    return None


def _parse_args(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True, help="Migration run UUID this evidence belongs to.")
    parser.add_argument("--contract", required=True, type=Path, help="Aggregate reconciliation contract JSON.")
    parser.add_argument(
        "--layer", action="append", required=True, type=_layer_argument, metavar="LAYER=PATH",
        help="Local SQLite (.sqlite/.sqlite3/.db) or DuckDB (.duckdb) file for a layer; repeat per layer.",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help="Owner-only directory for the report (default: migration/run-manifests).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        filename = report_filename(args.run_id)
        contract = load_contract(args.contract)
    except (OSError, ValueError) as error:
        message = str(error) if isinstance(error, ValueError) else "Contract file could not be read."
        print(f"Error: {message}", file=sys.stderr)
        return 2
    layers = dict()
    for layer, path in args.layer:
        if layer in layers:
            print(f"Error: --layer {layer} was given more than once.", file=sys.stderr)
            return 2
        layers[layer] = path
    if (args.output_dir / filename).exists() or (args.output_dir / filename).is_symlink():
        print(f"Error: {filename} already exists; refusing to overwrite.", file=sys.stderr)
        return 1
    connections: dict[str, object] = {}
    opened: dict[Path, object] = {}
    try:
        for layer, path in layers.items():
            key = path.resolve()
            if key not in opened:
                opened[key] = _open_local(path)
            connections[layer] = opened[key]
        report = reconcile(contract, connections, run_id=args.run_id)
    finally:
        for connection in opened.values():
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
    validate_report(report, expected_run_id=args.run_id)
    try:
        create_private_json(args.output_dir, filename, report)
    except FileExistsError:
        print(f"Error: {filename} already exists; refusing to overwrite.", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Error: could not write the report to a current-user-owned, owner-only directory.", file=sys.stderr)
        return 1
    print(f"Wrote {filename}: status {report['status']}; cutover_ready false.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
