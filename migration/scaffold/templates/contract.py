"""Shared Silver scaffold contract for the edition renderers and the scaffolder core.

Holds the plan shape, the canonical retail Silver contract, the opt-in gate, the
identifier and namespace patterns, and the plan validation that both edition renderers
(``scaffold.templates.bigquery`` and ``scaffold.templates.snowflake``) apply. Only the
dialect (types, casts, quoting, source block) lives in the edition modules.

Pure: no filesystem, environment, network, clock, or global state.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, TypedDict

CastName = Literal["string", "integer", "numeric", "date", "timestamp", "boolean"]


class ColumnPlan(TypedDict):
    """One canonical Silver column."""

    canonical_name: str  # canonical target column, for example ``customer_id``
    bronze_column: str  # dlt-normalized Bronze column from the saved dry run
    cast: CastName  # declared mapping cast
    nullable: bool  # False adds a ``not_null`` test; must be False for the primary key


class TablePlan(TypedDict):
    """One canonical table to scaffold."""

    entity: str  # canonical table: customers, products, orders, or order_items
    bronze_table: str  # dlt-normalized Bronze table name from the dry run
    columns: Sequence[ColumnPlan]  # exactly the canonical columns of ``entity``


class TemplatePlanError(ValueError):
    """The plan cannot be rendered. ``reason`` is a fixed, privacy-safe code."""

    def __init__(self, reason: str, table: str | None = None) -> None:
        self.reason = reason
        self.table = table
        super().__init__(reason if table is None else f"{reason}: {table}")


# Canonical retail Silver contract (item 145 D2; editions/Snowflake/docs/migration.md).
# columns are in canonical order; foreign keys map column -> (parent table, parent key).
CANONICAL: dict[str, dict[str, Any]] = {
    "customers": {
        "primary_key": "customer_id",
        "columns": ("customer_id", "created_at", "country"),
        "foreign_keys": {},
    },
    "order_items": {
        "primary_key": "order_item_id",
        "columns": ("order_item_id", "order_id", "product_id", "quantity", "unit_price"),
        "foreign_keys": {"order_id": ("orders", "order_id"), "product_id": ("products", "product_id")},
    },
    "orders": {
        "primary_key": "order_id",
        "columns": ("order_id", "customer_id", "order_status", "order_date", "updated_at"),
        "foreign_keys": {"customer_id": ("customers", "customer_id")},
    },
    "products": {
        "primary_key": "product_id",
        "columns": ("product_id", "product_name", "category"),
        "foreign_keys": {},
    },
}

# Casts whose semantics wait on items 138 (temporal) and 139 (type profiles) [U17].
REVIEW_CASTS = frozenset({"timestamp", "date", "numeric"})
# Provenance columns every Silver model passes through from Bronze.
PROVENANCE_COLUMNS = ("_migration_run_id", "_ingested_at_utc")

SOURCE_ID = re.compile(r"[a-z][a-z0-9_]{0,39}")
# Renderer-compatible Bronze identifiers (shared by both edition templates).
BRONZE_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,127}")
# Edition limits for the verified Bronze namespace (BigQuery dataset 1024, Snowflake 255).
NAMESPACE_PATTERN: dict[str, re.Pattern[str]] = {
    "bigquery": re.compile(r"starterkit_migration_[a-z0-9_]{1,1003}"),
    "snowflake": re.compile(r"starterkit_migration_[a-z0-9_]{1,234}"),
}

GATE = "target.name == 'migration' and (var('enable_migration_models', false) | as_bool)"

ValidatedTables = list[tuple[str, str, dict[str, dict[str, Any]]]]


def output_dir(source_id: str) -> str:
    """dbt-project-relative output directory for ``source_id`` (POSIX separators)."""
    check_source_id(source_id)
    return f"models/migration/generated/{source_id}"


def model_name(source_id: str, entity: str) -> str:
    return f"silver_{source_id}__{entity}"


def source_name(source_id: str) -> str:
    return f"migration_bronze_{source_id}"


def field(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        if key not in obj:
            raise TemplatePlanError("plan_invalid")
        return obj[key]
    try:
        return getattr(obj, key)
    except AttributeError:
        raise TemplatePlanError("plan_invalid") from None


def check_source_id(value: Any) -> str:
    if not isinstance(value, str) or not SOURCE_ID.fullmatch(value):
        raise TemplatePlanError("invalid_source_id")
    return value


def check_identifier(value: Any, table: str) -> str:
    if not isinstance(value, str) or not BRONZE_IDENTIFIER.fullmatch(value):
        raise TemplatePlanError("unsafe_identifier", table)
    return value


def validate_plan(
    plan: Any, edition: str, casts: Mapping[str, str]
) -> tuple[str, str, ValidatedTables]:
    """Re-check ``plan`` for ``edition``; ``casts`` is the edition's supported cast map."""
    if field(plan, "edition") != edition:
        raise TemplatePlanError("edition_mismatch")
    source_id = check_source_id(field(plan, "source_id"))
    namespace = field(plan, "bronze_namespace")
    if not isinstance(namespace, str) or not NAMESPACE_PATTERN[edition].fullmatch(namespace):
        raise TemplatePlanError("invalid_bronze_namespace")
    raw_tables = field(plan, "tables")
    if isinstance(raw_tables, (str, bytes, Mapping)) or not isinstance(raw_tables, Sequence) or not raw_tables:
        raise TemplatePlanError("plan_invalid")

    tables: dict[str, tuple[str, dict[str, dict[str, Any]]]] = {}
    for raw_table in raw_tables:
        entity = field(raw_table, "entity")
        if not isinstance(entity, str) or entity not in CANONICAL:
            raise TemplatePlanError("non_canonical_table")
        if entity in tables:
            raise TemplatePlanError("duplicate_table", entity)
        contract = CANONICAL[entity]
        bronze_table = check_identifier(field(raw_table, "bronze_table"), entity)
        raw_columns = field(raw_table, "columns")
        if isinstance(raw_columns, (str, bytes, Mapping)) or not isinstance(raw_columns, Sequence):
            raise TemplatePlanError("plan_invalid", entity)

        columns: dict[str, dict[str, Any]] = {}
        for raw_column in raw_columns:
            name = field(raw_column, "canonical_name")
            if not isinstance(name, str) or name not in contract["columns"]:
                raise TemplatePlanError("non_canonical_target", entity)
            if name in columns:
                raise TemplatePlanError("duplicate_column", entity)
            cast = field(raw_column, "cast")
            if not isinstance(cast, str) or cast not in casts:
                raise TemplatePlanError("unsupported_cast", entity)
            nullable = field(raw_column, "nullable")
            if not isinstance(nullable, bool):
                raise TemplatePlanError("plan_invalid", entity)
            columns[name] = {
                "bronze_column": check_identifier(field(raw_column, "bronze_column"), entity),
                "cast": cast,
                "nullable": nullable,
            }

        primary_key = contract["primary_key"]
        if primary_key not in columns:
            raise TemplatePlanError("missing_primary_key", entity)
        if set(columns) != set(contract["columns"]):
            raise TemplatePlanError("missing_contract_column", entity)
        if columns[primary_key]["nullable"]:
            raise TemplatePlanError("nullable_primary_key", entity)
        tables[entity] = (bronze_table, columns)

    ordered = [(entity, *tables[entity]) for entity in sorted(tables)]
    return source_id, namespace, ordered


def header(comment: str, template_id: str, source_id: str, review: Sequence[str]) -> list[str]:
    lines = [
        f"{comment} scaffolded: editable Silver starter model ({template_id}, source alias {source_id}).",
        f"{comment} Ownership: operator-owned after publication. The scaffolder never reads, rewrites,",
        f"{comment} renames, or deletes this file; a new scaffold needs a fresh client workspace.",
        f"{comment} Opt-in: enabled only with --target migration and --vars '{{enable_migration_models: true}}'.",
    ]
    if review:
        lines.append(
            f"{comment} REVIEW: cast placeholders pending items 138 (temporal contract) and 139 "
            "(destination type profiles):"
        )
        lines.append(f"{comment}   {', '.join(review)}.")
    return lines


def model_tests_yaml(
    source_id: str, tables: Sequence[tuple[str, str, Mapping[str, Mapping[str, Any]]]]
) -> list[str]:
    """The ``models:`` YAML block with canonical key and relationship tests."""
    planned = {entity for entity, _, _ in tables}
    lines = ["", "models:"]
    for entity, _, columns in tables:
        contract = CANONICAL[entity]
        primary_key = contract["primary_key"]
        lines += [
            f"  - name: {model_name(source_id, entity)}",
            f'    description: "Scaffolded Silver starter for canonical {entity}. Editable; not deduplicated."',
            "    columns:",
        ]
        for name in contract["columns"]:
            tests: list[str] = []
            if name == primary_key:
                tests += ["          - not_null", "          - unique"]
            elif not columns[name]["nullable"]:
                tests.append("          - not_null")
            parent = contract["foreign_keys"].get(name)
            if parent is not None and parent[0] in planned:
                parent_table, parent_key = parent
                tests += [
                    "          - relationships:",
                    "              arguments:",
                    f"                to: ref('{model_name(source_id, parent_table)}')",
                    f"                field: {parent_key}",
                ]
            lines.append(f"      - name: {name}")
            if tests:
                lines.append("        data_tests:")
                lines += tests
    return lines


def model_config_sql() -> list[str]:
    """The explicit Silver ``config()`` block shared by every generated model."""
    return [
        "{{",
        "    config(",
        "        materialized='view',",
        "        schema='silver',",
        "        tags=['scaffolded'],",
        f"        enabled=({GATE})",
        "    )",
        "}}",
    ]


NO_DEDUP_NOTE = [
    "-- No deduplication: Bronze rows pass through one to one. Deduplication or latest-row",
    "-- logic needs a source row-version field; the unique test on the primary key exposes",
    "-- duplicate keys. Key policy (item 136): string primary keys drop NULL, empty, and",
    "-- ASCII-space-only values; string foreign keys map those values to NULL.",
]
