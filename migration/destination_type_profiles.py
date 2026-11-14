"""Explicit destination numeric type profiles for reviewed migration mapping casts.

Each profile states the integer range and decimal precision/scale that a mapped
``integer`` or ``numeric`` value must fit before it may reach the destination
loader. The bounds mirror the column types dlt 1.x creates for the loader's
``bigint`` and unparameterized ``decimal`` hints on each destination; offline
tests compare them with dlt's type mapper so library drift fails loudly.

Validation is fail-closed and value-private: violations raise
``DestinationTypeBoundError`` carrying only a fixed reason code. Rejected values,
column names, and bound arithmetic results are never placed in messages.

This module is intentionally standard-library only so it can be imported and
tested without dlt, SQLAlchemy, or warehouse credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

PROFILE_ALGORITHM = "destination-numeric-type-profile-v1"

INTEGER_OUT_OF_RANGE = "integer_out_of_destination_range"
DECIMAL_INTEGER_DIGITS_EXCEEDED = "decimal_integer_digits_exceed_destination_profile"
DECIMAL_SCALE_EXCEEDED = "decimal_scale_exceeds_destination_profile"
UNSUPPORTED_BOUND_VALUE = "destination_bound_value_unsupported"

REASON_CODES = frozenset({
    INTEGER_OUT_OF_RANGE,
    DECIMAL_INTEGER_DIGITS_EXCEEDED,
    DECIMAL_SCALE_EXCEEDED,
    UNSUPPORTED_BOUND_VALUE,
})

_INT64_MIN = -(2 ** 63)
_INT64_MAX = 2 ** 63 - 1


class DestinationTypeBoundError(ValueError):
    """A mapped value does not fit the destination profile; carries only a fixed code."""

    def __init__(self, reason_code: str):
        if reason_code not in REASON_CODES:
            raise ValueError("Unknown destination type bound reason code.")
        self.reason_code = reason_code
        super().__init__(reason_code)


class UnsupportedTypeProfileError(ValueError):
    """No reviewed numeric profile exists for the requested destination."""


@dataclass(frozen=True)
class IntegerBounds:
    minimum: int
    maximum: int
    destination_type: str
    note: str


@dataclass(frozen=True)
class DecimalBounds:
    precision: int
    scale: int
    destination_type: str
    note: str

    @property
    def max_integer_digits(self) -> int:
        return self.precision - self.scale


@dataclass(frozen=True)
class DestinationTypeProfile:
    name: str
    destination: str
    integer: IntegerBounds
    decimal: DecimalBounds

    def describe(self) -> dict[str, object]:
        """Return a JSON-safe description for dry-run output and run manifests."""
        return {
            "name": self.name,
            "algorithm": PROFILE_ALGORITHM,
            "destination": self.destination,
            "integer": {
                "mapping_cast": "integer",
                "destination_type": self.integer.destination_type,
                # Strings keep 64-bit bounds exact for JSON consumers limited to doubles.
                "minimum": str(self.integer.minimum),
                "maximum": str(self.integer.maximum),
                "note": self.integer.note,
            },
            "decimal": {
                "mapping_cast": "numeric",
                "destination_type": self.decimal.destination_type,
                "precision": self.decimal.precision,
                "scale": self.decimal.scale,
                "max_integer_digits": self.decimal.max_integer_digits,
                "note": self.decimal.note,
            },
            "enforcement": "mapped integer/numeric values are checked during extraction, before normalize and load",
            "not_enforced": [
                "unmapped source columns (dlt infers or reflects their types)",
                "string, date, timestamp, and boolean casts",
                "warehouse-side casts in dbt Silver/Gold models",
            ],
        }


_DECIMAL_NOTE = (
    "Significant fractional digits beyond the scale are rejected rather than rounded; "
    "trailing fractional zeros are not significant."
)

PROFILES: dict[str, DestinationTypeProfile] = {
    "bigquery": DestinationTypeProfile(
        name="bigquery_dlt_default_v1",
        destination="bigquery",
        integer=IntegerBounds(
            _INT64_MIN, _INT64_MAX, "INT64",
            "dlt bigint hint creates INT64, a signed 64-bit integer.",
        ),
        decimal=DecimalBounds(
            38, 9, "NUMERIC(38,9)",
            "dlt unparameterized decimal hint creates NUMERIC(38,9): at most 29 integer and 9 fractional digits. "
            + _DECIMAL_NOTE,
        ),
    ),
    "snowflake": DestinationTypeProfile(
        name="snowflake_dlt_default_v1",
        destination="snowflake",
        integer=IntegerBounds(
            _INT64_MIN, _INT64_MAX, "NUMBER(19,0)",
            "dlt bigint hint creates NUMBER(19,0); the enforced range is the stricter signed 64-bit range "
            "so values stay within dlt's int64 handling and match the BigQuery INT64 profile.",
        ),
        decimal=DecimalBounds(
            38, 9, "NUMBER(38,9)",
            "dlt unparameterized decimal hint creates NUMBER(38,9): at most 29 integer and 9 fractional digits. "
            + _DECIMAL_NOTE,
        ),
    ),
    "duckdb": DestinationTypeProfile(
        name="duckdb_dlt_default_v1",
        destination="duckdb",
        integer=IntegerBounds(
            _INT64_MIN, _INT64_MAX, "BIGINT",
            "Local rehearsal destination; dlt bigint hint creates BIGINT, a signed 64-bit integer.",
        ),
        decimal=DecimalBounds(
            38, 9, "DECIMAL(38,9)",
            "Local rehearsal destination; dlt unparameterized decimal hint creates DECIMAL(38,9). "
            + _DECIMAL_NOTE,
        ),
    ),
}


def resolve_type_profile(destination: object) -> DestinationTypeProfile:
    """Return the reviewed profile for a destination, or fail closed."""
    profile = PROFILES.get(destination) if isinstance(destination, str) else None
    if profile is None:
        raise UnsupportedTypeProfileError("No reviewed numeric type profile exists for the selected destination.")
    return profile


def _significant_digits(value: Decimal) -> tuple[int, int]:
    """Return (integer_digits, fractional_digits) for a finite Decimal's exact value."""
    if value.is_zero():
        return 0, 0
    _, digits, exponent = value.as_tuple()
    digits = list(digits)
    # Drop trailing zeros that sit after the decimal point; they carry no value.
    while exponent < 0 and len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    fractional = max(0, -exponent)
    integer = max(0, len(digits) + exponent)
    return integer, fractional


def check_integer(value: int, profile: DestinationTypeProfile) -> None:
    if type(value) is not int:
        raise DestinationTypeBoundError(UNSUPPORTED_BOUND_VALUE)
    if not profile.integer.minimum <= value <= profile.integer.maximum:
        raise DestinationTypeBoundError(INTEGER_OUT_OF_RANGE)


def check_decimal(value: Decimal, profile: DestinationTypeProfile) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise DestinationTypeBoundError(UNSUPPORTED_BOUND_VALUE)
    integer_digits, fractional_digits = _significant_digits(value)
    # Check scale first: a value that would be rounded is the more specific loss.
    if fractional_digits > profile.decimal.scale:
        raise DestinationTypeBoundError(DECIMAL_SCALE_EXCEEDED)
    if integer_digits > profile.decimal.max_integer_digits:
        raise DestinationTypeBoundError(DECIMAL_INTEGER_DIGITS_EXCEEDED)


def check_mapped_value(value: object, cast: str, profile: DestinationTypeProfile) -> None:
    """Validate an already-cast mapped value against the destination profile.

    ``None`` stays null. Casts without numeric bounds pass through unchanged.
    """
    if value is None:
        return
    if cast == "integer":
        check_integer(value, profile)  # type: ignore[arg-type]
    elif cast == "numeric":
        check_decimal(value, profile)  # type: ignore[arg-type]
