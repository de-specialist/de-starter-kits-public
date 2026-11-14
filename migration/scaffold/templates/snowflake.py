"""Snowflake Silver starter-model renderer for the item-145 scaffolder.

``render_snowflake_silver(plan)`` is a pure function. It takes a column plan that the
scaffolder core has already validated against the mapping, dry-run, and run-manifest
evidence, and returns ``{file name: text}`` for the files that belong directly under the
output directory ``models/migration/generated/<source_id>/`` of the Snowflake dbt project
(``<workspace>/kit``, project ``reliable_pipeline``; see ``output_dir``).

It never reads files, environment variables, profiles, or a warehouse. It re-checks the
plan shape defensively and raises ``TemplatePlanError`` with a fixed reason code. Error
messages contain only the reason code and, where useful, a canonical table name; they
never echo source, Bronze, or namespace values.

The plan shape, reason codes, canonical contract, opt-in gate, and file layout are
shared with the BigQuery renderer (``scaffold.templates.bigquery``) through
``scaffold.templates.contract``. Only the dialect differs:

- Casts use Snowflake ``::`` syntax with edition-local types (``varchar``,
  ``number(38, 0)``, ``number(28, 6)``, ``date``, ``timestamp_ntz``, ``boolean``). The
  string and numeric types are what the shipped Snowflake migration Silver models get
  from ``dbt.type_string()`` and ``dbt.type_numeric()`` on dbt-snowflake. No
  ``try_cast``: a value that does not cast fails the model rather than becoming NULL.
- Blank foreign keys use ``iff()``.
- Bronze identifiers pass a lowercase safe-identifier check and are emitted unquoted, so
  Snowflake resolves them case-insensitively against dlt's upper-cased Bronze names. A
  name that is a Snowflake reserved keyword is the one case that needs quoting; it is
  emitted as a double-quoted upper-case identifier, which is how dlt stores it.
- The source block reads the database and Bronze schema through the edition's
  ``SNOWFLAKE_DATABASE`` and ``MIGRATION_BRONZE_NAMESPACE`` mechanism, with the verified
  namespace as the default.

Output contract (``docs/scaffolder-contract-proposal.md`` sections 4 and 5):

- ``silver_<source_id>__<table>.sql`` per planned canonical table, with an explicit
  ``config(materialized='view', schema='silver', tags=['scaffolded'], enabled=<gate>)``.
  The gate repeats the migration opt-in (``--target migration`` plus
  ``enable_migration_models: true``) so the model stays disabled by default even when
  the workspace's ``dbt_project.yml`` has no ``models/migration`` folder config.
- ``_silver_<source_id>__models.yml`` with the ``migration_bronze_<source_id>`` source
  block and dbt 1.10 ``data_tests:`` (``arguments:`` for relationships).
- Only canonical columns plus the ``_migration_run_id`` and ``_ingested_at_utc``
  provenance columns are selected.
- Item 136 key policy: string primary keys drop NULL, empty, and ASCII-space-only values;
  string foreign keys map those values to NULL. No deduplication.
- Bytes are deterministic: canonical table and column order, LF endings, ASCII, no
  timestamps. The ``.scaffold-manifest.json`` provenance file is the core's
  responsibility.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, TypedDict

from .contract import (
    CANONICAL,
    GATE,
    NO_DEDUP_NOTE,
    PROVENANCE_COLUMNS,
    REVIEW_CASTS,
    CastName,
    ColumnPlan,
    TablePlan,
    TemplatePlanError,
    header,
    model_config_sql,
    model_name,
    model_tests_yaml,
    output_dir,
    source_name,
    validate_plan,
)

__all__ = [
    "EDITION",
    "TEMPLATE_ID",
    "CastName",
    "ColumnPlan",
    "SilverPlan",
    "TablePlan",
    "TemplatePlanError",
    "model_name",
    "output_dir",
    "render_snowflake_silver",
    "source_name",
]

TEMPLATE_ID = "snowflake-silver-v1"
EDITION = "snowflake"


class SilverPlan(TypedDict):
    """Validated plan for one source and one edition.

    The renderer reads fields by key from a ``Mapping`` or by attribute from any other
    object, so the core may pass plain dicts or frozen dataclasses with these names.
    """

    edition: Literal["snowflake"]
    source_id: str  # generic non-sensitive alias, ``^[a-z][a-z0-9_]{0,39}$``
    bronze_namespace: str  # verified Bronze schema, starts with ``starterkit_migration_``
    tables: Sequence[TablePlan]


# Edition-local Snowflake types. varchar and number(28, 6) are what dbt.type_string()
# and dbt.type_numeric() resolve to on dbt-snowflake; number(38, 0) is Snowflake's
# integer; timestamp_ntz is the dbt-snowflake default timestamp type.
_SNOWFLAKE_TYPES: dict[str, str] = {
    "string": "varchar",
    "integer": "number(38, 0)",
    "numeric": "number(28, 6)",
    "date": "date",
    "timestamp": "timestamp_ntz",
    "boolean": "boolean",
}

# Snowflake reserved keywords (Snowflake SQL reference, "Reserved & limited keywords"),
# plus ANSI-reserved words Snowflake rejects as unquoted identifiers. A safe Bronze
# identifier in this set must be quoted to be read as a name.
_RESERVED = frozenset(
    """
    account all alter and any as asof between by case cast check column connect
    connection constraint create cross current current_date current_time
    current_timestamp current_user database delete distinct drop else exists false
    following for from full grant group gscluster having ilike in increment inner insert
    intersect into is issue join lateral left like localtime localtimestamp
    match_condition minus natural not null of on or order organization qualify regexp
    revoke right rlike row rows sample schema select set some start table tablesample
    then to trigger true try_cast union unique update using values view when whenever
    where window with
    """.split()
)


def render_snowflake_silver(plan: Any) -> dict[str, str]:
    """Render the Snowflake Silver starter files for one validated plan."""
    source_id, namespace, tables = validate_plan(plan, EDITION, _SNOWFLAKE_TYPES)
    files: dict[str, str] = {
        f"_silver_{source_id}__models.yml": _render_yaml(source_id, namespace, tables),
    }
    for entity, bronze_table, columns in tables:
        del bronze_table  # referenced through the source block
        files[f"{model_name(source_id, entity)}.sql"] = _render_sql(source_id, entity, columns)
    return {name: files[name] for name in sorted(files)}


# -- SQL ----------------------------------------------------------------------------------


def _needs_quoting(identifier: str) -> bool:
    return identifier in _RESERVED


def _sql_identifier(identifier: str) -> str:
    """Unquoted when safe; reserved words become dlt's upper-cased quoted name."""
    if _needs_quoting(identifier):
        return f'"{identifier.upper()}"'
    return identifier


def _cast(column: Mapping[str, Any]) -> str:
    return f"{_sql_identifier(column['bronze_column'])}::{_SNOWFLAKE_TYPES[column['cast']]}"


def _render_sql(source_id: str, entity: str, columns: Mapping[str, Mapping[str, Any]]) -> str:
    contract = CANONICAL[entity]
    primary_key = contract["primary_key"]
    review = [
        f"{name} ({columns[name]['cast']} -> {_SNOWFLAKE_TYPES[columns[name]['cast']]})"
        for name in contract["columns"]
        if columns[name]["cast"] in REVIEW_CASTS
    ]
    lines = header("--", TEMPLATE_ID, source_id, review)
    if any(columns[name]["cast"] == "timestamp" for name in contract["columns"]):
        lines += [
            "-- timestamp_ntz keeps wall-clock values without a time zone and its conversion",
            "-- depends on the session time zone; confirm UTC handling before relying on it.",
        ]
    lines += [*NO_DEDUP_NOTE, "", *model_config_sql(), "", "select"]
    for name in contract["columns"]:
        column = columns[name]
        expression = _cast(column)
        if name in contract["foreign_keys"] and column["cast"] == "string":
            lines.append(f"    iff(replace({expression}, ' ', '') = '', null::varchar, {expression}) as {name},")
        else:
            lines.append(f"    {expression} as {name},")
    lines += [f"    {name}," for name in PROVENANCE_COLUMNS]
    lines[-1] = lines[-1].rstrip(",")
    key = columns[primary_key]
    key_expression = _cast(key)
    lines += [
        f"from {{{{ source('{source_name(source_id)}', '{entity}') }}}}",
        f"where {key_expression} is not null",
    ]
    if key["cast"] == "string":
        lines.append(f"  and replace({key_expression}, ' ', '') <> ''")
    return "\n".join(lines) + "\n"


# -- YAML ---------------------------------------------------------------------------------


def _render_yaml(
    source_id: str,
    namespace: str,
    tables: Sequence[tuple[str, str, Mapping[str, Mapping[str, Any]]]],
) -> str:
    lines = header("#", TEMPLATE_ID, source_id, ())
    lines += [
        "version: 2",
        "",
        "sources:",
        f"  - name: {source_name(source_id)}",
        f'    description: "Bronze tables loaded by the optional dlt migration command for source alias {source_id}."',
        "    database: \"{{ env_var('SNOWFLAKE_DATABASE', target.database) }}\"",
        "    # REVIEW: if MIGRATION_BRONZE_NAMESPACE is set it must equal the verified default below.",
        f"    schema: \"{{{{ env_var('MIGRATION_BRONZE_NAMESPACE', '{namespace.upper()}') }}}}\"",
        "    config:",
        f'      enabled: "{{{{ {GATE} }}}}"',
        "    tables:",
    ]
    for entity, bronze_table, _ in tables:
        lines += [f"      - name: {entity}"]
        if _needs_quoting(bronze_table):
            lines += [
                f'        identifier: "{bronze_table.upper()}"',
                "        quoting:",
                "          identifier: true",
            ]
        else:
            # Quoted so YAML never reads a legal identifier such as null/on/no as a non-string.
            lines += [f'        identifier: "{bronze_table}"']
    lines += model_tests_yaml(source_id, tables)
    return "\n".join(lines) + "\n"
