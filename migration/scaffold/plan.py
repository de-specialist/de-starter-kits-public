"""Validated, deterministic scaffold plan and the edition renderer hook.

The plan is the only object an edition renderer receives. It is built from inputs that
already passed `scaffold.inputs` (admission, field tables, and binding steps 1-10), then the
canonical retail scope rules of `docs/scaffolder-contract-proposal.md` section 3.2. Every
identifier in it is either a fixed canonical name or a Bronze name that matched
`SAFE_IDENTIFIER`; raw mapping `source` strings never reach the plan.

Renderer contract (implemented by `scaffold/templates/bigquery.py` and
`scaffold/templates/snowflake.py`). This module imports the shared constants from
`scaffold/templates/contract.py`; only the edition renderer modules are optional
(`resolve_renderer` returns None when one is missing):

    render_<edition>_silver(silver_plan: Mapping) -> Mapping[str, str]

- The core passes `ScaffoldPlan.renderer_input()`: plain dicts and lists of exactly

      SilverPlan {edition, source_id, bronze_namespace,
                  tables: [TablePlan {entity, bronze_table,
                                      columns: [ColumnPlan {canonical_name, bronze_column,
                                                            cast, nullable}]}]}

  Bronze identifiers and the namespace are lower-case safe identifiers (Snowflake's
  upper-cased dlt names are folded; BigQuery names must already be lower-case).
- Pure function: no filesystem, environment, network, clock, randomness, or global state.
- Returns exactly `plan.renderer_files` as keys (bare file names inside
  `models/migration/generated/<source_id>/`): one `silver_<source_id>__<entity>.sql` per
  table and one `_silver_<source_id>__models.yml`. The core owns `.scaffold-manifest.json`;
  renderers must not return it.
- Values are complete UTF-8 text with LF line endings, ending in exactly one newline, no NUL
  or CR, at most `MAX_RENDERED_FILE_BYTES` bytes, and identical for identical plans.
- Any exception (for example a renderer's own `TemplatePlanError`) becomes the fixed code
  `renderer_output_invalid`; exception text is never reported.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from mapping_contract import RETAIL_CONTRACT, RETAIL_TESTS, SAFE_IDENTIFIER

from .reasons import (
    MAPPING_INVALID,
    MISSING_CONTRACT_COLUMN,
    MISSING_PRIMARY_KEY,
    NON_CANONICAL_TABLE,
    NON_CANONICAL_TARGET,
    RENDERER_OUTPUT_INVALID,
    UNSAFE_INVENTORY_IDENTIFIER,
    Refusal,
)

# Shared with the edition renderers: provenance columns every Silver model passes through
# from Bronze, the renderer-compatible Bronze identifier pattern, and the per-edition
# Bronze namespace limits (BigQuery dataset 1024, Snowflake 255).
from .templates.contract import BRONZE_IDENTIFIER as RENDER_IDENTIFIER
from .templates.contract import NAMESPACE_PATTERN, PROVENANCE_COLUMNS


PLAN_VERSION = 1
EDITIONS = ("bigquery", "snowflake")
# Canonical primary keys, fixed by the retail contract (never read from the mapping).
PRIMARY_KEYS: dict[str, str] = {
    table: key for table, key in (value.split(".") for value in RETAIL_TESTS["unique_not_null"])
}
OUTPUT_PARENT = ("models", "migration", "generated")
STAGING_ROOT = ".scaffold-staging"
MANIFEST_FILE = ".scaffold-manifest.json"
MAX_IDENTIFIER_LENGTH = 255
MAX_RENDERED_FILE_BYTES = 1024 * 1024
MANIFEST_SCHEMA_VERSION = 1
GENERATOR_VERSION = "scaffold-core-v1"


@dataclass(frozen=True)
class ColumnPlan:
    """One canonical Silver column and the Bronze column it is read from.

    Renderer fields (the integration seam):
    canonical_name:  canonical Silver column name, e.g. "order_id".
    bronze_column:   dlt-normalized Bronze column recorded in the dry run, as a lower-case
                     safe identifier. Never a raw mapping `source` string.
    cast:            mapping cast vocabulary: string, integer, numeric, date, timestamp, boolean.
    nullable:        False only for the canonical primary key (item 136: blank keys dropped);
                     blank foreign keys map to NULL.

    Core-only fields (not passed to renderers):
    is_primary_key:  True for the table's canonical key.
    mapped:          True when a mapping rule produced the column at load (already cast by the
                     loader); False when an identically named Bronze column is reused.
    """

    canonical_name: str
    bronze_column: str
    cast: str
    nullable: bool
    is_primary_key: bool = False
    mapped: bool = True

    def renderer_input(self) -> dict[str, Any]:
        return {
            "canonical_name": self.canonical_name,
            "bronze_column": self.bronze_column,
            "cast": self.cast,
            "nullable": self.nullable,
        }


@dataclass(frozen=True)
class TablePlan:
    """One generated Silver model. `columns` follow canonical contract order."""

    entity: str
    bronze_table: str
    columns: tuple[ColumnPlan, ...]
    primary_key: str = ""

    def renderer_input(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "bronze_table": self.bronze_table,
            "columns": [column.renderer_input() for column in self.columns],
        }


@dataclass(frozen=True)
class Relationship:
    """A canonical relationship test, emitted only when both sides are generated."""

    entity: str
    column: str
    parent_entity: str
    parent_column: str


@dataclass(frozen=True)
class ScaffoldPlan:
    """Deterministic plan for one source and one edition. Tables are sorted by entity."""

    edition: str
    source_id: str
    bronze_namespace: str
    tables: tuple[TablePlan, ...]
    relationships: tuple[Relationship, ...]
    # Integrity markers only (unkeyed SHA-256), never provenance or authentication.
    source_mapping_fingerprint: str
    schema_confirmation_fingerprint: str
    provenance_columns: tuple[str, ...] = PROVENANCE_COLUMNS
    plan_version: int = PLAN_VERSION

    @property
    def source_name(self) -> str:
        return f"migration_bronze_{self.source_id}"

    def model_name(self, entity: str) -> str:
        return f"silver_{self.source_id}__{entity}"

    @property
    def models_yaml_file(self) -> str:
        return f"_silver_{self.source_id}__models.yml"

    @property
    def renderer_files(self) -> tuple[str, ...]:
        names = [f"{self.model_name(table.entity)}.sql" for table in self.tables]
        names.append(self.models_yaml_file)
        return tuple(sorted(names))

    @property
    def output_dir(self) -> str:
        """Project-relative output directory (POSIX separators)."""
        return "/".join((*OUTPUT_PARENT, self.source_id))

    @property
    def planned_files(self) -> tuple[str, ...]:
        """Project-relative paths of every file a final generation would publish."""
        return tuple(sorted(f"{self.output_dir}/{name}" for name in (*self.renderer_files, MANIFEST_FILE)))

    def renderer_input(self) -> dict[str, Any]:
        """The exact SilverPlan mapping handed to an edition renderer (fresh copy per call)."""
        return {
            "edition": self.edition,
            "source_id": self.source_id,
            "bronze_namespace": self.bronze_namespace,
            "tables": [table.renderer_input() for table in self.tables],
        }


@dataclass(frozen=True)
class BronzeTable:
    """Validated Bronze evidence for one selected entity (built by scaffold.inputs)."""

    entity: str
    bronze_table: str
    # (raw source column, normalized destination column) pairs from the dry run.
    normalized_columns: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ValidatedEvidence:
    """Everything the plan builder may use after the input contract passed."""

    edition: str
    mapping: Mapping[str, tuple[Mapping[str, str], ...]] = field(hash=False)
    bronze: Mapping[str, BronzeTable] = field(hash=False)
    bronze_namespace: str = ""
    source_mapping_fingerprint: str = ""
    schema_confirmation_fingerprint: str = ""


def _safe_bronze_identifier(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= MAX_IDENTIFIER_LENGTH
        and SAFE_IDENTIFIER.fullmatch(value) is not None
        and not value.casefold().startswith("_dlt")
    )


def _render_identifier(value: str, edition: str) -> str:
    """Fold a recorded Bronze name to the renderer's lower-case form, or refuse.

    Snowflake stores unquoted dlt names upper-cased and resolves them case-insensitively, so
    folding is lossless. BigQuery names are case-sensitive, so they must already be lower-case.
    """
    folded = value.lower() if edition == "snowflake" else value
    if RENDER_IDENTIFIER.fullmatch(folded) is None:
        raise Refusal(UNSAFE_INVENTORY_IDENTIFIER)
    return folded


def _resolve_column(table: BronzeTable, canonical: str, rule: Mapping[str, str] | None) -> str | None:
    candidates = [
        (source, destination) for source, destination in table.normalized_columns
        if destination.casefold() == canonical.casefold()
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        raise Refusal(UNSAFE_INVENTORY_IDENTIFIER)
    source, destination = candidates[0]
    if rule is not None and source not in {rule["target"], rule["source"]}:
        raise Refusal(UNSAFE_INVENTORY_IDENTIFIER)
    if not _safe_bronze_identifier(destination):
        raise Refusal(UNSAFE_INVENTORY_IDENTIFIER)
    return destination


def build_plan(evidence: ValidatedEvidence, source_id: str) -> ScaffoldPlan:
    """Apply the canonical retail scope rules (contract 3.2) and return a frozen plan."""
    for table in sorted(evidence.mapping):
        if table not in RETAIL_CONTRACT:
            raise Refusal(NON_CANONICAL_TABLE)
    for table in sorted(evidence.mapping):
        canonical = RETAIL_CONTRACT[table]
        for rule in evidence.mapping[table]:
            if rule["target"].casefold() not in canonical:
                raise Refusal(NON_CANONICAL_TARGET)
            if rule["cast"] != canonical[rule["target"].casefold()]:
                raise Refusal(MAPPING_INVALID)

    namespace = evidence.bronze_namespace
    namespace = namespace.lower() if evidence.edition == "snowflake" else namespace
    if NAMESPACE_PATTERN[evidence.edition].fullmatch(namespace) is None:
        raise Refusal(UNSAFE_INVENTORY_IDENTIFIER)

    tables = []
    for entity in sorted(evidence.mapping):
        bronze = evidence.bronze[entity]
        if not _safe_bronze_identifier(bronze.bronze_table):
            raise Refusal(UNSAFE_INVENTORY_IDENTIFIER)
        bronze_table = _render_identifier(bronze.bronze_table, evidence.edition)
        rules = {rule["target"].casefold(): rule for rule in evidence.mapping[entity]}
        primary_key = PRIMARY_KEYS[entity]
        if _resolve_column(bronze, primary_key, rules.get(primary_key)) is None:
            raise Refusal(MISSING_PRIMARY_KEY)
        columns = []
        for canonical, declared_type in RETAIL_CONTRACT[entity].items():
            source_column = _resolve_column(bronze, canonical, rules.get(canonical))
            if source_column is None:
                raise Refusal(MISSING_CONTRACT_COLUMN)
            columns.append(ColumnPlan(
                canonical_name=canonical,
                bronze_column=_render_identifier(source_column, evidence.edition),
                cast=declared_type,
                nullable=canonical != primary_key,
                is_primary_key=canonical == primary_key,
                mapped=canonical in rules,
            ))
        if len({column.bronze_column for column in columns}) != len(columns):
            raise Refusal(UNSAFE_INVENTORY_IDENTIFIER)
        tables.append(TablePlan(entity, bronze_table, tuple(columns), primary_key))
    if len({table.bronze_table for table in tables}) != len(tables):
        raise Refusal(UNSAFE_INVENTORY_IDENTIFIER)

    selected = {table.entity for table in tables}
    relationships = []
    for value in RETAIL_TESTS["relationships"]:
        child, parent = (side.strip().split(".") for side in value.split("->"))
        if child[0] in selected and parent[0] in selected:
            relationships.append(Relationship(child[0], child[1], parent[0], parent[1]))
    return ScaffoldPlan(
        edition=evidence.edition,
        source_id=source_id,
        bronze_namespace=namespace,
        tables=tuple(tables),
        relationships=tuple(sorted(relationships, key=lambda item: (item.entity, item.column))),
        source_mapping_fingerprint=evidence.source_mapping_fingerprint,
        schema_confirmation_fingerprint=evidence.schema_confirmation_fingerprint,
    )


# --------------------------------------------------------------------------------------------
# Renderer registry hook
# --------------------------------------------------------------------------------------------

Renderer = Callable[[Mapping[str, Any]], Mapping[str, str]]

# Edition -> (module, function). Each function takes `ScaffoldPlan.renderer_input()`.
RENDERER_MODULES: dict[str, tuple[str, str]] = {
    "bigquery": ("scaffold.templates.bigquery", "render_bigquery_silver"),
    "snowflake": ("scaffold.templates.snowflake", "render_snowflake_silver"),
}
_REGISTERED: dict[str, Renderer] = {}


def register_renderer(edition: str, renderer: Renderer | None) -> Renderer | None:
    """Override (or with None, clear) the renderer for an edition; returns the previous override."""
    if edition not in EDITIONS:
        raise ValueError("Unknown edition.")
    previous = _REGISTERED.pop(edition, None)
    if renderer is not None:
        if not callable(renderer):
            raise TypeError("Renderer must be callable.")
        _REGISTERED[edition] = renderer
    return previous


def resolve_renderer(edition: str) -> Renderer | None:
    """Return the edition renderer, or None when its module is not installed yet."""
    if edition in _REGISTERED:
        return _REGISTERED[edition]
    module_name, function_name = RENDERER_MODULES[edition]
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name is not None and module_name.startswith(error.name):
            return None
        raise
    renderer = getattr(module, function_name, None)
    return renderer if callable(renderer) else None


_FILE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.]*")


def render_plan(plan: ScaffoldPlan, renderer: Renderer) -> dict[str, str]:
    """Run a renderer twice in memory and enforce the renderer contract. Writes nothing.

    Returns `{file name: text}` sorted by name, relative to `plan.output_dir`.
    """
    outputs = []
    for _ in range(2):
        try:
            result = renderer(plan.renderer_input())
        except Exception:
            raise Refusal(RENDERER_OUTPUT_INVALID) from None
        if not isinstance(result, Mapping):
            raise Refusal(RENDERER_OUTPUT_INVALID)
        outputs.append(dict(result))
    first, second = outputs
    if not all(isinstance(name, str) for name in first):
        raise Refusal(RENDERER_OUTPUT_INVALID)
    if first != second or tuple(sorted(first)) != plan.renderer_files:
        raise Refusal(RENDERER_OUTPUT_INVALID)
    for name, text in first.items():
        if (
            not isinstance(name, str) or _FILE_NAME.fullmatch(name) is None
            or not isinstance(text, str) or not text.endswith("\n") or text.endswith("\n\n")
            or "\r" in text or "\x00" in text
        ):
            raise Refusal(RENDERER_OUTPUT_INVALID)
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError:
            raise Refusal(RENDERER_OUTPUT_INVALID) from None
        if len(encoded) > MAX_RENDERED_FILE_BYTES:
            raise Refusal(RENDERER_OUTPUT_INVALID)
    return {name: first[name] for name in sorted(first)}


def build_manifest(plan: ScaffoldPlan, rendered: Mapping[str, str]) -> str:
    """Deterministic `.scaffold-manifest.json` text (contract proposal section 4).

    Holds the manifest schema and generator versions, edition, source_id, the mapping and
    schema-confirmation fingerprints (integrity markers only), and for each rendered file its
    project-relative path and SHA-256. No row values, credentials, source URLs, Bronze names,
    timestamps, or absolute paths.
    """
    if tuple(sorted(rendered)) != plan.renderer_files:
        raise Refusal(RENDERER_OUTPUT_INVALID)
    document = {
        "edition": plan.edition,
        "files": [
            {
                "path": f"{plan.output_dir}/{name}",
                "sha256": hashlib.sha256(rendered[name].encode("utf-8")).hexdigest(),
            }
            for name in sorted(rendered)
        ],
        "generator_version": GENERATOR_VERSION,
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "plan_version": plan.plan_version,
        "schema_confirmation_fingerprint": plan.schema_confirmation_fingerprint,
        "source_id": plan.source_id,
        "source_mapping_fingerprint": plan.source_mapping_fingerprint,
    }
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def render_outputs(plan: ScaffoldPlan, renderer: Renderer) -> dict[str, str]:
    """Every file a final generation would publish: `{project-relative path: text}`, sorted.

    In memory only. The future publisher (atomic no-replace, contract 6.2) stages exactly
    this mapping; `tuple(result) == plan.planned_files`.
    """
    rendered = render_plan(plan, renderer)
    files = {f"{plan.output_dir}/{name}": text for name, text in rendered.items()}
    files[f"{plan.output_dir}/{MANIFEST_FILE}"] = build_manifest(plan, rendered)
    return {path: files[path] for path in sorted(files)}
