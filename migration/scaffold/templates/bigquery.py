"""BigQuery Silver starter-model renderer for the item-145 scaffolder.

``render_bigquery_silver(plan)`` is a pure function. It takes a column plan that the
scaffolder core has already validated against the mapping, dry-run, and run-manifest
evidence, and returns ``{file name: text}`` for the files that belong directly under the
output directory ``models/migration/generated/<source_id>/`` (see ``output_dir``).

It never reads files, environment variables, profiles, or a warehouse. It re-checks the
plan shape defensively and raises ``TemplatePlanError`` with a fixed reason code. Error
messages contain only the reason code and, where useful, a canonical table name; they
never echo source, Bronze, or namespace values.

The plan shape, reason codes, canonical contract, and opt-in gate are shared with the
Snowflake renderer through ``scaffold.templates.contract``; this module holds only the
BigQuery dialect.

Output contract (``docs/scaffolder-contract-proposal.md`` sections 4 and 5):

- ``silver_<source_id>__<table>.sql`` per planned canonical table, with an explicit
  ``config(materialized='view', schema='silver', tags=['scaffolded'], enabled=<gate>)``.
  The gate repeats the migration opt-in (``--target migration`` plus
  ``enable_migration_models: true``) so the model stays disabled by default even when
  the workspace's ``dbt_project.yml`` has no ``models/migration`` folder config.
- ``_silver_<source_id>__models.yml`` with the ``migration_bronze_<source_id>`` source
  block and dbt 1.10 ``data_tests:`` (``arguments:`` for relationships). The source
  dataset is read through the edition's ``MIGRATION_BRONZE_NAMESPACE`` mechanism with the
  verified namespace as the default, never as a bare literal
  (``docs/scaffolder-input-contract.md``).
- Only canonical columns plus the ``_migration_run_id`` and ``_ingested_at_utc``
  provenance columns are selected. Bronze identifiers are backtick-quoted.
- Item 136 key policy: string primary keys drop NULL, empty, and ASCII-space-only values;
  string foreign keys map those values to NULL. No deduplication.
- Bytes are deterministic: canonical table and column order, LF endings, no timestamps.
  The ``.scaffold-manifest.json`` provenance file is the core's responsibility.
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
    "render_bigquery_silver",
    "source_name",
]

TEMPLATE_ID = "bigquery-silver-v1"
EDITION = "bigquery"


class SilverPlan(TypedDict):
    """Validated plan for one source and one edition.

    The renderer reads fields by key from a ``Mapping`` or by attribute from any other
    object, so the core may pass plain dicts or frozen dataclasses with these names.
    """

    edition: Literal["bigquery"]
    source_id: str  # generic non-sensitive alias, ``^[a-z][a-z0-9_]{0,39}$``
    bronze_namespace: str  # verified Bronze dataset, starts with ``starterkit_migration_``
    tables: Sequence[TablePlan]


_BIGQUERY_TYPES: dict[str, str] = {
    "string": "string",
    "integer": "int64",
    "numeric": "numeric",
    "date": "date",
    "timestamp": "timestamp",
    "boolean": "bool",
}


def render_bigquery_silver(plan: Any) -> dict[str, str]:
    """Render the BigQuery Silver starter files for one validated plan."""
    source_id, namespace, tables = validate_plan(plan, EDITION, _BIGQUERY_TYPES)
    files: dict[str, str] = {
        f"_silver_{source_id}__models.yml": _render_yaml(source_id, namespace, tables),
    }
    for entity, bronze_table, columns in tables:
        del bronze_table  # referenced through the source block
        files[f"{model_name(source_id, entity)}.sql"] = _render_sql(source_id, entity, columns)
    return {name: files[name] for name in sorted(files)}


# -- SQL ----------------------------------------------------------------------------------


def _cast(column: Mapping[str, Any]) -> str:
    return f"cast(`{column['bronze_column']}` as {_BIGQUERY_TYPES[column['cast']]})"


def _render_sql(source_id: str, entity: str, columns: Mapping[str, Mapping[str, Any]]) -> str:
    contract = CANONICAL[entity]
    primary_key = contract["primary_key"]
    review = [
        f"{name} ({columns[name]['cast']})"
        for name in contract["columns"]
        if columns[name]["cast"] in REVIEW_CASTS
    ]
    lines = header("--", TEMPLATE_ID, source_id, review)
    lines += [*NO_DEDUP_NOTE, "", *model_config_sql(), "", "select"]
    for name in contract["columns"]:
        column = columns[name]
        expression = _cast(column)
        if name in contract["foreign_keys"] and column["cast"] == "string":
            lines += [
                "    case",
                f"        when replace({expression}, ' ', '') = '' then cast(null as string)",
                f"        else {expression}",
                f"    end as {name},",
            ]
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
        "    # REVIEW: if MIGRATION_BRONZE_NAMESPACE is set it must equal the verified default below.",
        f"    schema: \"{{{{ env_var('MIGRATION_BRONZE_NAMESPACE', '{namespace}') }}}}\"",
        "    config:",
        f'      enabled: "{{{{ {GATE} }}}}"',
        "    tables:",
    ]
    for entity, bronze_table, _ in tables:
        # Quoted so YAML never reads a legal identifier such as null/on/no as a non-string.
        lines += [f"      - name: {entity}", f'        identifier: "{bronze_table}"']
    lines += model_tests_yaml(source_id, tables)
    return "\n".join(lines) + "\n"
