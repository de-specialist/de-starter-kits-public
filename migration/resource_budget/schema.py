"""Strict local resource envelope schema (card 316).

An envelope is built only from configuration the caller supplies. There are no silent
defaults: a limit that is not configured is missing, and asking for it is an error.

Accepted shape (a plain ``dict``, or JSON text of the same shape)::

    {
      "schema_version": 1,
      "limits": {
        "install.scratch_bytes": {"unit": "bytes", "limit": 2147483648}
      }
    }

Rules:

* ``limit`` is a plain ``int`` in ``1 .. 2**63 - 1``. Booleans, floats, strings (including
  numeric strings such as ``"1024"`` or ``"1 GiB"``), ``None`` and zero or negative values
  are refused. JSON integer literals longer than 19 digits are refused before conversion.
* ``unit`` is one of ``bytes``, ``rows``, ``files``, ``members``, ``depth``, ``seconds``.
  The last segment of the limit key must name the unit (``scratch_bytes`` or ``bytes``),
  so a key and its unit cannot disagree silently.
* Limit keys are 2-4 lowercase dotted ``snake_case`` segments, at most 64 characters.
* Unknown or duplicate keys, extra nesting, more than 64 limits, and JSON text over 64 KiB
  are refused. Error messages are fixed and never echo the supplied value.
* ``seconds`` is a schema-only dimension (owner decision D1, assumed default): callers
  enforce it themselves, for example with a subprocess timeout. This package never starts a
  subprocess.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterator

from .result import MAX_VALUE, UNITS, is_strict_int, is_valid_limit_key

SCHEMA_VERSION = 1
MAX_LIMITS = 64
MAX_CONFIG_TEXT_CHARS = 65_536
MAX_JSON_DEPTH = 3
MAX_INT_DIGITS = 19

_TOP_LEVEL_KEYS = frozenset({"schema_version", "limits"})
_ENTRY_KEYS = frozenset({"unit", "limit"})


class BudgetSchemaError(ValueError):
    """The supplied budget configuration is invalid. Messages never echo supplied values."""


@dataclass(frozen=True)
class Limit:
    """A configured cap (claim kind ``limit``). It refuses work; it never describes usage."""

    key: str
    unit: str
    value: int

    def __post_init__(self) -> None:
        if not is_valid_limit_key(self.key):
            raise BudgetSchemaError("A limit key is not a valid dotted snake_case key.")
        if type(self.unit) is not str or self.unit not in UNITS:
            raise BudgetSchemaError("A limit unit is not one of the supported base units.")
        last_segment = self.key.rsplit(".", 1)[1]
        if last_segment != self.unit and not last_segment.endswith("_" + self.unit):
            raise BudgetSchemaError("A limit key does not name its unit in its last segment.")
        if not is_strict_int(self.value):
            raise BudgetSchemaError("A limit value must be a plain integer.")
        if not 1 <= self.value <= MAX_VALUE:
            raise BudgetSchemaError("A limit value must be positive and at most 2**63 - 1.")


class BudgetEnvelope:
    """Immutable set of supplied limits. Missing limits are errors, never defaults."""

    __slots__ = ("_limits",)

    def __init__(self, limits: tuple[Limit, ...]) -> None:
        if type(limits) is not tuple or not limits:
            raise BudgetSchemaError("An envelope needs at least one limit.")
        if len(limits) > MAX_LIMITS:
            raise BudgetSchemaError("An envelope has too many limits.")
        by_key: dict[str, Limit] = {}
        for item in limits:
            if type(item) is not Limit:
                raise BudgetSchemaError("Envelope entries must be Limit objects.")
            if item.key in by_key:
                raise BudgetSchemaError("An envelope cannot repeat a limit key.")
            by_key[item.key] = item
        object.__setattr__(self, "_limits", MappingProxyType(by_key))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("BudgetEnvelope is immutable.")

    def __contains__(self, key: object) -> bool:
        return key in self._limits

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._limits))

    def __len__(self) -> int:
        return len(self._limits)

    def limit(self, key: str, unit: str) -> Limit:
        """Return the supplied limit for ``key``, requiring the caller's expected ``unit``.

        There is no default argument on purpose: a missing limit is an error.
        """
        if not is_valid_limit_key(key):
            raise BudgetSchemaError("A limit key is not a valid dotted snake_case key.")
        found = self._limits.get(key)
        if found is None:
            raise BudgetSchemaError(f"Required limit `{key}` was not supplied.")
        if found.unit != unit:
            raise BudgetSchemaError(f"Limit `{key}` does not use the expected unit.")
        return found


def load_envelope(config: object) -> BudgetEnvelope:
    """Validate a supplied ``dict`` config and return an immutable envelope."""
    if type(config) is not dict:
        raise BudgetSchemaError("Budget config must be a JSON object.")
    if set(config) != _TOP_LEVEL_KEYS:
        raise BudgetSchemaError("Budget config must have exactly schema_version and limits.")
    version = config["schema_version"]
    if not is_strict_int(version) or version != SCHEMA_VERSION:
        raise BudgetSchemaError("Budget config schema_version must be the integer 1.")
    raw_limits = config["limits"]
    if type(raw_limits) is not dict or not raw_limits:
        raise BudgetSchemaError("Budget config limits must be a non-empty object.")
    if len(raw_limits) > MAX_LIMITS:
        raise BudgetSchemaError("Budget config has too many limits.")
    limits = []
    for key, entry in raw_limits.items():
        if not is_valid_limit_key(key):
            raise BudgetSchemaError("A limit key is not a valid dotted snake_case key.")
        if type(entry) is not dict or set(entry) != _ENTRY_KEYS:
            raise BudgetSchemaError(f"Limit `{key}` must have exactly unit and limit.")
        try:
            limits.append(Limit(key=key, unit=entry["unit"], value=entry["limit"]))
        except BudgetSchemaError as exc:
            raise BudgetSchemaError(f"Limit `{key}` is invalid: {exc}") from None
    return BudgetEnvelope(tuple(limits))


def parse_envelope_json(text: object) -> BudgetEnvelope:
    """Parse bounded JSON text into an envelope, refusing huge or ambiguous literals."""
    if type(text) is not str:
        raise BudgetSchemaError("Budget config text must be a string.")
    if len(text) > MAX_CONFIG_TEXT_CHARS:
        raise BudgetSchemaError("Budget config text is too large.")
    if _json_depth(text) > MAX_JSON_DEPTH:
        raise BudgetSchemaError("Budget config is nested too deeply.")
    try:
        config = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_int=_bounded_int,
            parse_float=_refuse_float,
            parse_constant=_refuse_constant,
        )
    except BudgetSchemaError:
        raise
    except (ValueError, RecursionError):
        raise BudgetSchemaError("Budget config is not valid JSON.") from None
    return load_envelope(config)


def _json_depth(text: str) -> int:
    """Maximum bracket depth outside strings; computed before any parsing."""
    depth = deepest = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif char in "]}":
            depth -= 1
    return deepest


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BudgetSchemaError("Budget config repeats a key.")
        result[key] = value
    return result


def _bounded_int(literal: str) -> int:
    if len(literal.lstrip("-")) > MAX_INT_DIGITS:
        raise BudgetSchemaError("Budget config has an integer literal that is too long.")
    return int(literal)


def _refuse_float(literal: str) -> float:
    raise BudgetSchemaError("Budget config limits must be integers, not decimals.")


def _refuse_constant(literal: str) -> float:
    raise BudgetSchemaError("Budget config cannot contain NaN or Infinity.")


# PROVISIONAL EXAMPLE ONLY. These limits are illustrative and unmeasured,
# not defaults. Callers must pass an explicitly reviewed configuration.
# Install step durations in particular are unmeasured estimates.
_PROVISIONAL_EXAMPLE_CONFIG = {
    "schema_version": 1,
    "limits": {
        "archive.compressed_bytes": {"unit": "bytes", "limit": 16_777_216},
        "archive.uncompressed_bytes": {"unit": "bytes", "limit": 67_108_864},
        "archive.members": {"unit": "members", "limit": 2_000},
        "archive.member_bytes": {"unit": "bytes", "limit": 8_388_608},
        "install.scratch_bytes": {"unit": "bytes", "limit": 2_147_483_648},
        "install.scratch_files": {"unit": "files", "limit": 60_000},
        "install.pip_step_seconds": {"unit": "seconds", "limit": 900},
        "install.dbt_parse_step_seconds": {"unit": "seconds", "limit": 300},
        "disk.reserved_headroom_bytes": {"unit": "bytes", "limit": 1_073_741_824},
        "evidence.file_bytes": {"unit": "bytes", "limit": 10_485_760},
        "tree.depth": {"unit": "depth", "limit": 64},
    },
}


def provisional_example_config() -> dict[str, object]:
    """Return a fresh copy of the PROVISIONAL, unmeasured example config.

    For documentation and review only. It is not a default, not a measured value set, and not
    owner-approved. Callers that use it must say so in their own output.
    """
    return copy.deepcopy(_PROVISIONAL_EXAMPLE_CONFIG)
