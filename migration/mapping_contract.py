"""Dependency-free version 1 mapping-file parser shared by the loader and the scaffolder.

This module imports only the standard library. It must never import dlt, SQLAlchemy, dbt,
profiles, or destination configuration, so offline tools (for example the Silver scaffolder)
can apply exactly the loader's mapping rules without side effects (scaffolder contract U4).

Every error message is fixed text that never interpolates caller-supplied names or contents.
`MappingConfigurationError.code` adds a stable machine-readable category for callers that need
reason codes; `run_migration.py` keeps using the message text unchanged.
"""

from __future__ import annotations

import json
import os
import re
import stat
from hashlib import sha256
from pathlib import Path


SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAPPING_CASTS = frozenset({"string", "integer", "numeric", "date", "timestamp", "boolean"})
MAX_MAPPING_FILE_BYTES = 1024 * 1024
MAX_MAPPING_SOURCE_NAME_LENGTH = 255

# Error categories carried by MappingConfigurationError.code.
MAPPING_UNREADABLE = "unreadable"
MAPPING_NOT_REGULAR_OR_OVERSIZED = "not_regular_or_oversized"
MAPPING_MALFORMED_JSON = "malformed_json"
MAPPING_UNSUPPORTED_VERSION = "unsupported_version"
MAPPING_INVALID_RULES = "invalid_rules"

_MSG_PLATFORM = "Mapping file cannot be opened safely on this platform."
_MSG_SIZE = "Mapping file must be a regular, non-symlink file no larger than 1 MiB."
_MSG_UTF8 = "Mapping file could not be read as UTF-8 JSON."
_MSG_JSON = "Mapping file is not valid JSON."
_MSG_DUPLICATE = "Mapping JSON object contains duplicate keys."
_MSG_VERSION = "Mapping file must use the supported version 1 format with only version and tables keys."
_MSG_TABLES = "Mapping file tables must be a JSON object."
_MSG_TABLE_NAMES = "Mapping table names must be safe identifiers with an array of column rules."
_MSG_TABLE_UNIQUE = "Mapping table names must be unique ignoring case."
_MSG_RULE_KEYS = "Each mapping rule must contain only source, target, and cast."
_MSG_RULE_VALUES = (
    "Mapping source names must be non-empty raw column names no longer than 255 characters, "
    "without control characters or newlines. Targets must be safe identifiers and casts must be supported."
)
_MSG_RULE_UNIQUE = "Mapping source and target columns must be unique within each table."


# Canonical retail Bronze/Silver contract: table -> ordered {column: cast}. Read from both
# editions' migration Silver SQL and schema.yml tests; raw Bronze names before Silver casts.
RETAIL_CONTRACT: dict[str, dict[str, str]] = {
    "customers": {
        "customer_id": "string",
        "created_at": "timestamp",
        "country": "string",
    },
    "products": {
        "product_id": "string",
        "product_name": "string",
        "category": "string",
    },
    "orders": {
        "order_id": "string",
        "customer_id": "string",
        "order_status": "string",
        "order_date": "date",
        "updated_at": "timestamp",
    },
    "order_items": {
        "order_item_id": "string",
        "order_id": "string",
        "product_id": "string",
        "quantity": "integer",
        "unit_price": "numeric",
    },
}
RETAIL_TESTS: dict[str, list[str]] = {
    "unique_not_null": ["customers.customer_id", "products.product_id", "orders.order_id", "order_items.order_item_id"],
    "relationships": [
        "orders.customer_id -> customers.customer_id",
        "order_items.order_id -> orders.order_id",
        "order_items.product_id -> products.product_id",
    ],
}


class MappingConfigurationError(ValueError):
    """A fixed, safe-to-display mapping configuration error with a stable category code."""

    def __init__(self, message: str, code: str = MAPPING_INVALID_RULES):
        super().__init__(message)
        self.code = code


class _MappingError(ValueError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def valid_mapping_source_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_MAPPING_SOURCE_NAME_LENGTH
        and not any(
            ord(char) < 32 or 0x7F <= ord(char) <= 0x9F or char in "\u2028\u2029"
            for char in value
        )
    )


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _MappingError(_MSG_DUPLICATE, MAPPING_MALFORMED_JSON)
        result[key] = value
    return result


def load_mapping_file(path: Path | None) -> dict[str, list[dict[str, str]]]:
    """Parse a version 1 mapping file; table keys are case-folded and rules keep file order."""
    try:
        return _load_mapping_file(path)
    except json.JSONDecodeError:
        raise MappingConfigurationError(_MSG_JSON, MAPPING_MALFORMED_JSON) from None
    except _MappingError as error:
        raise MappingConfigurationError(str(error), error.code) from None
    except ValueError as error:
        # Defensive: every parser error above uses fixed text.
        raise MappingConfigurationError(str(error), MAPPING_INVALID_RULES) from None


def _load_mapping_file(path: Path | None) -> dict[str, list[dict[str, str]]]:
    if path is None:
        return {}
    try:
        if not hasattr(os, "O_NOFOLLOW"):
            raise _MappingError(_MSG_PLATFORM, MAPPING_UNREADABLE)
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0),
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MAPPING_FILE_BYTES:
                raise _MappingError(_MSG_SIZE, MAPPING_NOT_REGULAR_OR_OVERSIZED)
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                raw_payload = stream.read(MAX_MAPPING_FILE_BYTES + 1)
            if len(raw_payload) > MAX_MAPPING_FILE_BYTES:
                raise _MappingError(_MSG_SIZE, MAPPING_NOT_REGULAR_OR_OVERSIZED)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            text_payload = raw_payload.decode("utf-8")
        except UnicodeDecodeError:
            raise _MappingError(_MSG_UTF8, MAPPING_MALFORMED_JSON) from None
        payload = json.loads(text_payload, object_pairs_hook=unique_json_object)
    except ValueError:
        raise
    except Exception:
        raise _MappingError(_MSG_UTF8, MAPPING_UNREADABLE) from None
    if (not isinstance(payload, dict) or set(payload) != {"version", "tables"}
            or type(payload.get("version")) is not int or payload.get("version") != 1):
        raise _MappingError(_MSG_VERSION, MAPPING_UNSUPPORTED_VERSION)
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise _MappingError(_MSG_TABLES, MAPPING_INVALID_RULES)
    normalized: dict[str, list[dict[str, str]]] = {}
    for table, entries in tables.items():
        if not isinstance(table, str) or not SAFE_IDENTIFIER.fullmatch(table) or not isinstance(entries, list):
            raise _MappingError(_MSG_TABLE_NAMES, MAPPING_INVALID_RULES)
        if table.casefold() in normalized:
            raise _MappingError(_MSG_TABLE_UNIQUE, MAPPING_INVALID_RULES)
        rules = []
        seen_sources: set[str] = set()
        seen_targets: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"source", "target", "cast"}:
                raise _MappingError(_MSG_RULE_KEYS, MAPPING_INVALID_RULES)
            source, target, cast = entry["source"], entry["target"], entry["cast"]
            if (not valid_mapping_source_name(source)
                    or not isinstance(target, str) or not SAFE_IDENTIFIER.fullmatch(target)
                    or not isinstance(cast, str) or cast not in MAPPING_CASTS):
                raise _MappingError(_MSG_RULE_VALUES, MAPPING_INVALID_RULES)
            if source.casefold() in seen_sources or target.casefold() in seen_targets:
                raise _MappingError(_MSG_RULE_UNIQUE, MAPPING_INVALID_RULES)
            seen_sources.add(source.casefold())
            seen_targets.add(target.casefold())
            rules.append({"source": source, "target": target, "cast": cast})
        normalized[table.casefold()] = rules
    return normalized


def mappings_fingerprint(mappings: dict[str, list[dict[str, str]]]) -> str:
    payload = json.dumps(mappings, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return sha256(payload).hexdigest()
