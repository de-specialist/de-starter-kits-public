# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   }
# META }

# PARAMETERS CELL ********************

# Synthetic HR analytics: Bronze -> Silver -> Gold on Fabric Lakehouses.
# Proof of concept for MOCK fixture data only. Not production ready; it does not
# create or enforce any Fabric permission, suppression or disclosure control.
#
# Every setting below can instead come from an environment variable
# (HR_ANALYTICS_<NAME IN UPPER CASE>) or a Spark property
# (spark.hr_analytics.<name>). A non-empty notebook value wins, then the
# environment variable, then the Spark property.
#
# Use GUIDs for workspace and lakehouse ids. Names also work when both the
# workspace and the lakehouse are given by name. <layer>_tables_path overrides
# the derived OneLake path with a full ".../Tables" (or ".../Tables/<schema>") URI.
workspace_id = ""  # shared default for all three layers
bronze_workspace_id = ""
bronze_lakehouse_id = ""
bronze_schema = ""  # only for schema-enabled lakehouses, e.g. "dbo"
bronze_tables_path = ""
silver_workspace_id = ""
silver_lakehouse_id = ""
silver_schema = ""
silver_tables_path = ""
gold_workspace_id = ""
gold_lakehouse_id = ""
gold_schema = ""
gold_tables_path = ""
# "true" lets a changed Bronze row replace the retained Silver row with the same
# natural key. Default: reject the changed row and keep the retained one.
allow_retained_row_replacement = "false"
# "true" rejects any Bronze columns outside the source contract. Default warns
# and excludes them from Silver so system metadata never flows downstream.
strict_schema = "false"

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

import os
import re
import uuid
from datetime import datetime, timezone
from functools import reduce
from typing import NamedTuple

from delta.tables import DeltaTable
from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F

ONELAKE_HOST = "onelake.dfs.fabric.microsoft.com"
LAYERS = ("bronze", "silver", "gold")
GUID = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
ITEM_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
MOCK_KEY = r"^MOCK[-_][A-Za-z0-9][A-Za-z0-9._-]*$"
ISO_DATE = r"^\d{4}-\d{2}-\d{2}([ T].*)?$"
NUMBER = r"^[+-]?\d{1,16}(\.\d+)?$"
DECIMAL = "decimal(18,2)"

REJECTS_TABLE = "silver_hr_rejects"
MANIFEST_TABLE = "bronze_export_manifest"
LEADERSHIP_TABLE = "gold_monthly_hr_kpis"
ANALYST_TABLE = "gold_analyst_pseudonymous_detail"
QUALITY_TABLE = "gold_hr_quality_scorecard"
AUDIT_TABLE = "gold_hr_run_audit"


# ---------------------------------------------------------------- configuration


def setting(name: str) -> str:
    """Notebook parameter, then environment variable, then Spark property."""
    spark = SparkSession.getActiveSession()
    candidates = (
        globals().get(name),
        os.environ.get(f"HR_ANALYTICS_{name.upper()}"),
        spark.conf.get(f"spark.hr_analytics.{name}", "") if spark else "",
    )
    for candidate in candidates:
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return ""


def tables_root(layer: str) -> str:
    """OneLake 'Tables' folder of the lakehouse configured for a layer."""
    explicit = setting(f"{layer}_tables_path")
    if explicit:
        return explicit.rstrip("/")
    workspace = setting(f"{layer}_workspace_id") or setting("workspace_id")
    lakehouse = setting(f"{layer}_lakehouse_id")
    if not workspace or not lakehouse:
        raise ValueError(
            f"Set {layer}_lakehouse_id and a workspace id, or {layer}_tables_path."
        )
    if bool(GUID.match(workspace)) != bool(GUID.match(lakehouse)):
        raise ValueError(
            f"{layer}: give the workspace and the lakehouse both as GUIDs or both as names."
        )
    if GUID.match(lakehouse):
        item = lakehouse
    else:
        if not (ITEM_NAME.match(workspace) and ITEM_NAME.match(lakehouse)):
            raise ValueError(
                f"{layer}: names with spaces or symbols need GUIDs or {layer}_tables_path."
            )
        item = lakehouse if lakehouse.endswith(".Lakehouse") else f"{lakehouse}.Lakehouse"
    root = f"abfss://{workspace}@{ONELAKE_HOST}/{item}/Tables"
    schema = setting(f"{layer}_schema")
    return f"{root}/{schema}" if schema else root


# ------------------------------------------------------------- source contracts
#
# Field kinds
#   key         string that must look like a MOCK identifier
#   text        trimmed string
#   lower_code  lower-cased code; upper_code: upper-cased code
#   date        ISO date; month_end: ISO date that is the last day of its month
#   decimal     decimal(18,2); int: whole number


class Field(NamedTuple):
    name: str
    kind: str
    nullable: bool = False  # value may be null
    optional_column: bool = False  # column may be absent from Bronze
    allowed: tuple = ()  # approved normalized code values


class Source(NamedTuple):
    silver_table: str
    keys: tuple  # natural key = Silver grain
    fields: tuple
    lineage_only: tuple = ()  # validated, then dropped before deduplication
    derived: tuple = ()  # (column, Spark SQL expression)
    rules: tuple = ()  # (reject reason, Spark SQL condition that is true for a bad row)


# Bronze table name is "bronze_<source name>", matching fixtures/input/<source name>.csv.
SOURCES = {
    "employee_snapshot": Source(
        silver_table="silver_employee_snapshot",
        keys=("employee_key", "snapshot_month_end"),
        fields=(
            Field("snapshot_month_end", "month_end"),
            Field("employee_key", "key"),
            Field("company", "key"),
            Field("location", "key"),
            Field("location_hierarchy", "text", nullable=True, optional_column=True),
            Field("department", "text"),
            Field("worker_status", "lower_code",
                  allowed=("active", "terminated", "leave", "other")),
            Field("job_title", "text"),
            Field("employee_type", "text"),
            Field("employee_start_date", "date"),
            Field("alternate_start_date", "date", nullable=True, optional_column=True),
            Field("termination_date", "date", nullable=True),
        ),
    ),
    "requisition_snapshot": Source(
        silver_table="silver_requisition_snapshot",
        keys=("requisition_key", "snapshot_month_end"),
        fields=(
            Field("snapshot_month_end", "month_end"),
            Field("requisition_key", "key"),
            Field("job_title", "text"),
            Field("requisition_status", "lower_code",
                  allowed=("open", "filled", "closed", "other")),
            Field("number_of_openings", "int"),
            Field("applicant_count", "int"),
            Field("hired_applicant_count", "int"),
            Field("requisition_created_date", "date"),
            Field("closed_date", "date", nullable=True),
            Field("department", "text"),
        ),
    ),
    "leave_balance_snapshot": Source(
        silver_table="silver_leave_balance_snapshot",
        keys=("employee_key", "leave_type", "snapshot_month_end"),
        fields=(
            Field("snapshot_month_end", "month_end"),
            Field("employee_key", "key"),
            Field("leave_type", "upper_code"),
            Field("current_accrued_hours", "decimal"),
            Field("hours_taken_ytd", "decimal"),
            Field("hours_scheduled", "decimal"),
            Field("hours_remaining", "decimal"),
        ),
    ),
    # One row per hire event. A hire without a requisition stays a hire with a
    # null duration; a missing start date or a negative duration is rejected.
    "time_to_hire": Source(
        silver_table="silver_hire_event",
        keys=("hire_event_key",),
        fields=(
            Field("hire_event_key", "key"),
            Field("employee_key", "key"),
            Field("requisition_key", "key", nullable=True),
            Field("requisition_created_date", "date", nullable=True),
            Field("employee_start_date", "date"),
        ),
        derived=(
            ("time_to_hire_days", "datediff(employee_start_date, requisition_created_date)"),
        ),
        rules=(
            ("negative_time_to_hire", "employee_start_date < requisition_created_date"),
        ),
    ),
    # Repeated monthly exports of one event collapse to one row: the export
    # month is validated and then dropped, so it never multiplies the event.
    "termination_event": Source(
        silver_table="silver_termination_event",
        keys=("termination_event_key",),
        fields=(
            Field("snapshot_month_end", "month_end"),
            Field("termination_event_key", "key"),
            Field("employee_key", "key"),
            Field("termination_date", "date"),
        ),
        lineage_only=("snapshot_month_end",),
    ),
    "schedule_actual": Source(
        silver_table="silver_schedule_actual",
        keys=("employee_key", "work_date"),
        fields=(
            Field("employee_key", "key"),
            Field("work_date", "date"),
            Field("scheduled_hours", "decimal"),
            Field("actual_hours", "decimal"),
        ),
        derived=(("hours_variance", "actual_hours - scheduled_hours"),),
    ),
    "time_entry": Source(
        silver_table="silver_time_entry",
        keys=("employee_key", "entry_date", "entry_type_code"),
        fields=(
            Field("employee_key", "key"),
            Field("entry_date", "date"),
            Field("entry_type_code", "key"),
            Field("entry_category", "upper_code",
                  allowed=("ORDINARY_TIME", "OVERTIME", "ABSENCE", "PAID_LEAVE")),
            Field("hours", "decimal"),
            Field("reported_date", "date", nullable=True),
        ),
    ),
}


# ----------------------------------------------------------------------- Silver


def _typed(field: Field):
    """Normalized, typed value of a cleaned string column; null when unparseable."""
    name = f"`{field.name}`"
    value = F.col(name)
    if field.kind == "lower_code":
        return F.lower(value)
    if field.kind == "upper_code":
        return F.upper(value)
    if field.kind in ("date", "month_end"):
        return F.when(value.rlike(ISO_DATE), F.expr(f"try_cast(substring({name}, 1, 10) as date)"))
    if field.kind in ("decimal", "int"):
        number = F.when(value.rlike(NUMBER), F.expr(f"try_cast({name} as {DECIMAL})"))
        if field.kind == "decimal":
            return number
        return F.when(number == F.floor(number), number.cast("int"))
    return value


def _reject_reasons(source: Source):
    """Semicolon-joined reasons; an empty string means the row is valid."""
    reasons = []

    def add(condition, label):
        reasons.append(F.when(condition, F.lit(label)))

    for field in source.fields:
        raw, value = F.col(f"_raw.{field.name}"), F.col(field.name)
        if not field.nullable:
            add(raw.isNull(), f"missing_required:{field.name}")
        if field.kind in ("date", "month_end", "decimal", "int"):
            add(raw.isNotNull() & value.isNull(), f"invalid_{field.kind}:{field.name}")
        if field.kind == "month_end":
            add(value != F.last_day(value), f"not_month_end:{field.name}")
        if field.kind == "key":
            add(~value.rlike(MOCK_KEY), f"non_mock_identifier:{field.name}")
        if field.allowed:
            add(~value.isin(*field.allowed), f"unapproved_code:{field.name}")
    for label, condition in source.rules:
        add(F.expr(condition), label)
    # array_join skips nulls, so only the triggered reasons remain.
    return F.array_join(F.array(*reasons), ";")


def _as_rejects(df: DataFrame, name: str, source: Source, reasons) -> DataFrame:
    """Quarantine shape. row_json holds cleaned Bronze values: keep it restricted."""
    key = [F.coalesce(F.col(f"_raw.{k}"), F.lit("<null>")) for k in source.keys]
    return df.select(
        F.lit(name).alias("source_table"),
        reasons.alias("reject_reasons"),
        F.concat_ws("|", *key).alias("natural_key"),
        F.to_json(F.col("_raw")).alias("row_json"),
    ).distinct()


def stage_source(spark: SparkSession, name: str, source: Source, loaded_at: str):
    """Read one Bronze table and return (accepted rows, rejected rows), both lazy.

    Only contract columns are read, so any other Bronze column never reaches
    Silver or Gold. A missing contract column fails the run before any write.
    """
    bronze = spark.read.format("delta").load(f"{tables_root('bronze')}/bronze_{name}")
    by_lower = {column.lower(): column for column in bronze.columns}
    missing = [f.name for f in source.fields
               if f.name not in by_lower and not f.optional_column]
    if missing:
        raise ValueError(f"bronze_{name} is missing contract columns: {missing}")
    contract_columns = {f.name.lower() for f in source.fields}
    unexpected = sorted(column for column in bronze.columns
                        if column.lower() not in contract_columns)
    if unexpected:
        message = f"bronze_{name} has columns outside its allowlist: {unexpected}"
        if setting("strict_schema").lower() == "true":
            raise ValueError(message)
        print(f"SCHEMA WARNING: {message}")

    def cleaned(field: Field):
        if field.name not in by_lower:
            return F.lit(None).cast("string").alias(field.name)
        text = F.trim(F.col(f"`{by_lower[field.name]}`").cast("string"))
        return F.when(text != "", text).alias(field.name)

    raw = bronze.select(*[cleaned(f) for f in source.fields])
    checked = raw.select(
        F.struct(*[F.col(f"`{f.name}`") for f in source.fields]).alias("_raw"),
        *[_typed(f).alias(f.name) for f in source.fields],
    ).withColumn("_reject_reasons", _reject_reasons(source))

    invalid = checked.filter(F.col("_reject_reasons") != "")
    value_columns = [f.name for f in source.fields if f.name not in source.lineage_only]

    # Exact duplicates (after normalization) collapse to one row. Rows that still
    # share a natural key disagree on some value: all of them are rejected,
    # because nothing in the data says which revision is right.
    deduplicated = (
        checked.filter(F.col("_reject_reasons") == "")
        .dropDuplicates(value_columns)
        .withColumn("_versions", F.count(F.lit(1)).over(Window.partitionBy(*source.keys)))
    )
    conflicting = deduplicated.filter(F.col("_versions") > 1)

    accepted = deduplicated.filter(F.col("_versions") == 1).select("_raw", *value_columns)
    for column, expression in source.derived:
        accepted = accepted.withColumn(column, F.expr(expression))
    row_hash = F.sha2(
        F.concat_ws("\u001f", *[F.coalesce(F.col(c).cast("string"), F.lit("<null>"))
                                for c in value_columns]),
        256,
    )
    accepted = (
        accepted.withColumn("_row_hash", row_hash)
        .withColumn("_silver_loaded_at_utc", F.lit(loaded_at).cast("timestamp"))
    )
    rejected = _as_rejects(invalid, name, source, F.col("_reject_reasons")).unionByName(
        _as_rejects(conflicting, name, source, F.lit("conflicting_same_grain_revision"))
    )
    return accepted, rejected


def validate_snapshot_manifest(spark: SparkSession) -> None:
    """Require explicit complete snapshot evidence before writing any Silver data.

    Row counts are only a consistency check; a non-empty completeness_evidence
    value must come from a producer control total or an owner attestation.
    """
    path = f"{tables_root('bronze')}/{MANIFEST_TABLE}"
    if not DeltaTable.isDeltaTable(spark, path):
        raise ValueError(f"Missing required Bronze manifest table: {MANIFEST_TABLE}")
    manifest = spark.read.format("delta").load(path)
    required = {
        "source_name", "extract_id", "extracted_at_utc", "covered_start_date",
        "covered_end_date", "snapshot_month_end", "is_complete_snapshot",
        "row_count", "completeness_evidence",
    }
    missing = sorted(required - set(manifest.columns))
    if missing:
        raise ValueError(f"{MANIFEST_TABLE} is missing required columns: {missing}")
    manifest = (manifest
                .withColumn("snapshot_month_end", F.to_date("snapshot_month_end"))
                .withColumn("row_count", F.col("row_count").cast("long"))
                .withColumn(
                    "is_complete_snapshot",
                    F.lower(F.trim(F.col("is_complete_snapshot").cast("string"))).isin("true", "1", "yes"),
                ))
    snapshots = ("employee_snapshot", "requisition_snapshot", "leave_balance_snapshot")
    expected = manifest.filter(F.col("source_name").isin(*snapshots))
    if expected.filter(
        F.col("extract_id").isNull() | (F.trim("extract_id") == "")
        | F.col("completeness_evidence").isNull()
        | (F.trim("completeness_evidence") == "")
        | F.col("is_complete_snapshot").isNull()
        | (F.col("is_complete_snapshot") != F.lit(True))
        | F.col("snapshot_month_end").isNull()
        | F.col("row_count").isNull()
        | (F.col("row_count") < 0)
    ).limit(1).count():
        raise ValueError("Snapshot manifest has missing evidence, incomplete periods, or invalid counts")
    duplicates = expected.groupBy("source_name", "extract_id").count().filter(F.col("count") > 1)
    if duplicates.limit(1).count():
        raise ValueError("Snapshot manifest has duplicate source/extract identities")

    for name in snapshots:
        bronze_path = f"{tables_root('bronze')}/bronze_{name}"
        bronze = spark.read.format("delta").load(bronze_path)
        if "snapshot_month_end" not in bronze.columns:
            raise ValueError(f"bronze_{name} has no snapshot_month_end for manifest reconciliation")
        actual = bronze.groupBy(F.to_date("snapshot_month_end").alias("snapshot_month_end")).count()
        declared = (expected.filter(F.col("source_name") == name)
                    .groupBy("snapshot_month_end")
                    .agg(F.sum("row_count").alias("declared_count")))
        mismatches = (actual.join(declared, "snapshot_month_end", "full")
                      .filter(F.coalesce("count", F.lit(0)) != F.coalesce("declared_count", F.lit(0))))
        if mismatches.limit(1).count():
            raise ValueError(f"bronze_{name} row counts do not match its explicit manifest")
        if declared.limit(1).count() == 0:
            raise ValueError(f"Snapshot manifest has no complete periods for {name}")


def write_silver(spark: SparkSession, name: str, source: Source, accepted: DataFrame):
    """Insert-only merge on the natural key; returns rows rejected against history.

    Month-end history survives a Bronze table that only holds the latest export.
    Exact normalized duplicates are collapsed; conflicting history is preserved
    but blocks affected metrics until an approved source rule resolves it.
    """
    path = f"{tables_root('silver')}/{source.silver_table}"
    incoming = accepted.drop("_raw")
    value_columns = [field.name for field in source.fields if field.name not in source.lineage_only]
    if not DeltaTable.isDeltaTable(spark, path):
        incoming.write.format("delta").save(path)
        return None

    history = spark.read.format("delta").load(path)
    # Upgrade older POC tables in place while preserving every retained row.
    # Existing implementations may lack newly added optional/derived and audit
    # columns. Derive deterministic columns from stored values where possible.
    for column in value_columns:
        if column not in history.columns:
            history = history.withColumn(column, F.lit(None).cast(incoming.schema[column].dataType))
    for column, expression in source.derived:
        if column not in history.columns:
            history = history.withColumn(column, F.expr(expression))
    hash_values = [
        F.coalesce(F.col(column).cast("string"), F.lit("<null>"))
        for column in value_columns
    ]
    history = history.withColumn("_row_hash", F.sha2(F.concat_ws("\u001f", *hash_values), 256))
    if "_silver_loaded_at_utc" not in history.columns:
        history = history.withColumn("_silver_loaded_at_utc", F.lit(None).cast("timestamp"))
    if history.columns != incoming.columns:
        history = history.select(*incoming.columns)

    # Collapse byte-for-byte equivalent normalized history, which can remain
    # after an older ingestion version keyed repeated exports by snapshot date.
    # Distinct rows sharing a business key are retained and quarantined: without
    # an approved revision rule, selecting a winner would invent policy.
    history = history.dropDuplicates(value_columns)
    conflicting_keys = (history.groupBy(*source.keys).count()
                        .filter(F.col("count") > 1).select(*source.keys))
    history_conflicts = history.join(conflicting_keys, list(source.keys), "inner")
    history_conflict_rejects = history_conflicts.select(
        F.lit(name).alias("source_table"),
        F.lit("conflicting_retained_silver_history").alias("reject_reasons"),
        F.concat_ws("|", *[F.coalesce(F.col(k).cast("string"), F.lit("<null>"))
                            for k in source.keys]).alias("natural_key"),
        F.to_json(F.struct(*[F.col(c) for c in value_columns])).alias("row_json"),
    )
    (history.write.format("delta").mode("overwrite").option("overwriteSchema", "true")
     .save(path))

    replace = setting("allow_retained_row_replacement").lower() == "true"
    match = " AND ".join(f"t.`{k}` <=> s.`{k}`" for k in source.keys)
    safe_incoming = incoming.join(conflicting_keys, list(source.keys), "left_anti")
    merge = DeltaTable.forPath(spark, path).alias("t").merge(safe_incoming.alias("s"), match)
    if replace:
        merge = merge.whenMatchedUpdateAll(condition="t._row_hash <> s._row_hash")
    merge.whenNotMatchedInsertAll().execute()
    conflict_input = incoming.join(conflicting_keys, list(source.keys), "inner")
    input_conflict_rejects = conflict_input.select(
        F.lit(name).alias("source_table"),
        F.lit("incoming_key_conflicts_with_retained_silver_history").alias("reject_reasons"),
        F.concat_ws("|", *[F.coalesce(F.col(k).cast("string"), F.lit("<null>"))
                            for k in source.keys]).alias("natural_key"),
        F.to_json(F.struct(*[F.col(c) for c in value_columns])).alias("row_json"),
    )

    # Same natural key, different values than the retained row: the retained row
    # was left untouched by the insert-only merge, so report the changed one.
    retained = spark.read.format("delta").load(path).select(
        *source.keys, F.col("_row_hash").alias("_retained_hash")
    )
    changed = safe_incoming.join(retained, list(source.keys)).filter(
        F.col("_row_hash") != F.col("_retained_hash")
    )
    changed_rejects = changed.select(
        F.lit(name).alias("source_table"),
        F.lit("conflicts_with_retained_silver_row").alias("reject_reasons"),
        F.concat_ws("|", *[F.coalesce(F.col(k).cast("string"), F.lit("<null>"))
                            for k in source.keys]).alias("natural_key"),
        F.to_json(F.struct(*[F.col(c) for c in value_columns])).alias("row_json"),
    ) if not replace else history_conflict_rejects.limit(0)
    return reduce(DataFrame.unionByName, [
        history_conflict_rejects, input_conflict_rejects, changed_rejects,
    ])


# ------------------------------------------------------------------------- Gold

# kpi, unit, status when computed, behaviour when no value exists, Silver sources.
# Names, units and statuses follow fixtures/expected/monthly_hr_kpis.csv.
# absence_hours and absence_rate are deliberately absent: absence reporting is
# blocked until the entry-type map, grain and formula are approved.
#   zero              event count: no rows in the month means 0 (draft DAX default)
#   missing_snapshot  snapshot measure: no snapshot observed for the month
#   no_valid_rows     sum or median with nothing to aggregate
LEADERSHIP_KPIS = (
    ("active_headcount", "employees", "synthetic_fixture", "missing_snapshot", ("employee_snapshot",)),
    ("hires", "employees", "synthetic_fixture", "zero", ("time_to_hire",)),
    ("terminations", "employees", "synthetic_fixture", "zero", ("termination_event",)),
    ("open_requisitions", "requisitions", "synthetic_fixture", "missing_snapshot", ("requisition_snapshot",)),
    ("open_positions", "positions", "synthetic_fixture", "missing_snapshot", ("requisition_snapshot",)),
    ("applicants", "applicants", "synthetic_fixture", "missing_snapshot", ("requisition_snapshot",)),
    ("hired_applicants", "applicants", "synthetic_fixture", "missing_snapshot", ("requisition_snapshot",)),
    ("turnover_rate", "ratio", "synthetic_fixture", "missing_snapshot", ("employee_snapshot", "termination_event")),
    ("median_time_to_hire", "calendar_days", "synthetic_fixture", "no_valid_rows", ("time_to_hire",)),
    ("scheduled_hours", "hours", "synthetic_fixture", "no_valid_rows", ("schedule_actual",)),
    ("actual_hours", "hours", "synthetic_fixture", "no_valid_rows", ("schedule_actual",)),
    ("hours_variance", "hours", "synthetic_fixture", "no_valid_rows", ("schedule_actual",)),
)
KPI_DEFINITIONS = {
    "active_headcount": "Distinct active employees at the month-end snapshot.",
    "hires": "Accepted hire events by employee start month.",
    "terminations": "Distinct termination events by termination month.",
    "open_requisitions": "Distinct requisitions with open status at month end.",
    "open_positions": "Openings on requisitions with open status at month end.",
    "applicants": "Applicants reported in the requisition snapshot.",
    "hired_applicants": "Hired applicants reported in the requisition snapshot.",
    "turnover_rate": "Terminations divided by average opening and closing active headcount.",
    "median_time_to_hire": "Median calendar days from requisition creation to employee start.",
    "scheduled_hours": "Sum of scheduled hours in the work month.",
    "actual_hours": "Sum of actual hours in the work month.",
    "hours_variance": "Actual hours minus scheduled hours.",
}
LEADERSHIP_COLUMNS = ["period", "kpi", "value", "unit", "definition", "status"]
ANALYST_COLUMNS = [
    "period", "subject_key", "metric", "numeric_value", "text_value", "unit", "group_label",
]


def _period(column: str):
    return F.to_date(F.concat(F.date_format(F.col(column), "yyyy-MM"), F.lit("-01")))


def _long(df: DataFrame, kpis) -> DataFrame:
    """Wide per-period measures -> (period, kpi, value, status_override)."""
    measures = F.array(*[
        F.struct(F.lit(k).alias("kpi"), F.col(k).cast("double").alias("value")) for k in kpis
    ])
    return df.select("period", F.explode(measures).alias("m")).select(
        "period", "m.kpi", "m.value", F.lit(None).cast("string").alias("status_override")
    )


def build_leadership(spark: SparkSession, silver: dict, blocked_sources: set) -> DataFrame:
    """Organization-wide monthly KPIs: no keys, no detail rows, no exact dates."""
    is_open = F.col("requisition_status") == "open"
    headcount = silver["employee_snapshot"].groupBy("snapshot_month_end").agg(
        F.countDistinct(
            F.when(F.col("worker_status") == "active", F.col("employee_key"))
        ).alias("active_headcount")
    )
    requisitions = silver["requisition_snapshot"].groupBy(
        _period("snapshot_month_end").alias("period")
    ).agg(
        F.countDistinct(F.when(is_open, F.col("requisition_key"))).alias("open_requisitions"),
        F.coalesce(F.sum(F.when(is_open, F.col("number_of_openings"))), F.lit(0)).alias("open_positions"),
        F.sum("applicant_count").alias("applicants"),
        F.sum("hired_applicant_count").alias("hired_applicants"),
    )
    leave = silver["leave_balance_snapshot"].groupBy(
        _period("snapshot_month_end").alias("period")
    ).agg(
        F.coalesce(
            F.sum(F.when(F.col("leave_type") == "ANNUAL", F.col("hours_remaining"))), F.lit(0)
        ).alias("annual_leave_hours_remaining")
    )
    hires = silver["time_to_hire"].groupBy(_period("employee_start_date").alias("period")).agg(
        F.count(F.lit(1)).alias("hires"),
        # Exact median; averages the two middle values and ignores null durations.
        F.expr("percentile(time_to_hire_days, 0.5)").alias("median_time_to_hire"),
    )
    terminations = silver["termination_event"].groupBy(
        _period("termination_date").alias("period")
    ).agg(F.count(F.lit(1)).alias("terminations"))
    schedule = silver["schedule_actual"].groupBy(_period("work_date").alias("period")).agg(
        F.sum("scheduled_hours").alias("scheduled_hours"),
        F.sum("actual_hours").alias("actual_hours"),
        F.sum("hours_variance").alias("hours_variance"),
    )

    # Turnover = terminations / average of opening and closing active headcount.
    # Opening headcount is the previous month-end snapshot. When that snapshot
    # was never observed the rate is reported as not computable, never estimated.
    opening = headcount.select(
        F.col("snapshot_month_end").alias("prior_month_end"),
        F.col("active_headcount").alias("opening_headcount"),
    )
    denominator = (F.col("opening_headcount") + F.col("active_headcount")) / 2
    turnover = (
        headcount
        .withColumn("prior_month_end", F.last_day(F.add_months("snapshot_month_end", -1)))
        .join(opening, "prior_month_end", "left")
        .withColumn("period", _period("snapshot_month_end"))
        .join(terminations, "period", "left")
        .select(
            "period",
            F.lit("turnover_rate").alias("kpi"),
            F.when(
                denominator > 0,
                F.round(F.coalesce(F.col("terminations"), F.lit(0)) / denominator, 6),
            ).alias("value"),
            F.when(F.col("opening_headcount").isNull(), F.lit("not_computable_missing_snapshot"))
            .when(denominator == 0, F.lit("not_computable_zero_denominator"))
            .alias("status_override"),
        )
    )

    values = reduce(DataFrame.unionByName, [
        _long(headcount.withColumn("period", _period("snapshot_month_end")), ["active_headcount"]),
        _long(requisitions, ["open_requisitions", "open_positions", "applicants", "hired_applicants"]),
        _long(leave, ["annual_leave_hours_remaining"]),
        _long(hires, ["hires", "median_time_to_hire"]),
        _long(terminations, ["terminations"]),
        _long(schedule, ["scheduled_hours", "actual_hours", "hours_variance"]),
        turnover,
    ])
    catalog = spark.createDataFrame(
        [(kpi, unit, status, missing, bool(blocked_sources.intersection(sources)))
         for kpi, unit, status, missing, sources in LEADERSHIP_KPIS],
        "kpi string, unit string, computed_status string, when_missing string, blocked boolean",
    )
    value, override = F.col("value"), F.col("status_override")
    # One row per observed month and KPI, so a KPI that cannot be computed is
    # visible with a status instead of silently missing.
    return (
        values.select("period").distinct().crossJoin(catalog)
        .join(values, ["period", "kpi"], "left")
        .withColumn(
            "value",
            F.when(F.col("blocked"), F.lit(None).cast("double"))
            .when(value.isNull() & override.isNull() & (F.col("when_missing") == "zero"), F.lit(0.0))
            .otherwise(value),
        )
        .withColumn(
            "status",
            F.when(F.col("blocked"), F.lit("blocked_source_rows_rejected"))
            .when(override.isNotNull(), override)
            .when(F.col("value").isNotNull(), F.col("computed_status"))
            .when(F.col("when_missing") == "missing_snapshot", F.lit("not_computable_missing_snapshot"))
            .otherwise(F.lit("not_computable_no_valid_rows")),
        )
        .withColumn(
            "definition",
            F.create_map(*[
                item for key, definition in KPI_DEFINITIONS.items()
                for item in (F.lit(key), F.lit(definition))
            ])[F.col("kpi")],
        )
        .select(*LEADERSHIP_COLUMNS)
    )


def _detail(df: DataFrame, fact: str, date_column: str, numeric=(), text=(),
            employee=True, requisition=False, code=None, release_status=None) -> DataFrame:
    """One row per record attribute; numbers and text never share a column."""
    no_text = F.lit(None).cast("string")
    attributes = [
        F.struct(F.lit(name).alias("attribute_name"), F.col(name).cast(DECIMAL).alias("numeric_value"),
                 no_text.alias("text_value"), F.lit(unit).alias("unit"))
        for name, unit in numeric
    ] + [
        F.struct(F.lit(name).alias("attribute_name"), F.lit(None).cast(DECIMAL).alias("numeric_value"),
                 F.col(name).cast("string").alias("text_value"), no_text.alias("unit"))
        for name in text
    ]
    status = release_status if release_status is not None else F.lit("restricted_analyst_unvalidated")
    return (
        df.select(
            _period(date_column).alias("period"),
            (F.col("employee_key") if employee else F.col("requisition_key")).alias("subject_key"),
            F.explode(F.array(*attributes)).alias("a"),
            status.alias("group_label"),
        )
        .select("period", "subject_key", F.concat(F.lit(fact + "."), F.col("a.attribute_name")).alias("metric"),
                "a.numeric_value", "a.text_value", "a.unit", "group_label")
        .filter(F.col("numeric_value").isNotNull() | F.col("text_value").isNotNull())
        .select(*ANALYST_COLUMNS)
    )


def build_analyst_detail(silver: dict) -> DataFrame:
    """Pseudonymous record-level detail for the restricted analyst audience."""
    is_active = (F.col("worker_status") == "active").cast("int")
    absence_status = F.when(
        F.col("entry_category") == "ABSENCE", F.lit("blocked_absence_definition_unapproved")
    ).otherwise(F.lit("restricted_analyst_unvalidated"))
    hours = "hours_unit_unapproved"
    return reduce(DataFrame.unionByName, [
        _detail(
            silver["employee_snapshot"].withColumn("is_active", is_active),
            "workforce_snapshot", "snapshot_month_end",
            numeric=[("is_active", "flag")],
            text=["worker_status", "employee_type", "company", "location", "department", "job_title"],
        ),
        _detail(
            silver["requisition_snapshot"], "requisition_snapshot", "snapshot_month_end",
            numeric=[("number_of_openings", "positions"), ("applicant_count", "applicants"),
                     ("hired_applicant_count", "applicants")],
            text=["requisition_status", "department", "job_title"],
            employee=False, requisition=True,
        ),
        _detail(
            silver["time_to_hire"].withColumn("hire_count", F.lit(1)),
            "hire", "employee_start_date",
            numeric=[("hire_count", "events"), ("time_to_hire_days", "calendar_days")],
            requisition=True,
        ),
        _detail(
            silver["termination_event"].withColumn("termination_count", F.lit(1)),
            "termination", "termination_date", numeric=[("termination_count", "events")],
        ),
        _detail(
            silver["schedule_actual"], "schedule_actual", "work_date",
            numeric=[("scheduled_hours", "hours"), ("actual_hours", "hours"), ("hours_variance", "hours")],
        ),
        _detail(
            silver["time_entry"], "time_entry", "entry_date",
            numeric=[("hours", "hours")], text=["entry_category"],
            code="entry_type_code", release_status=absence_status,
        ),
        _detail(
            silver["leave_balance_snapshot"], "leave_balance_snapshot", "snapshot_month_end",
            numeric=[("current_accrued_hours", hours), ("hours_taken_ytd", hours),
                     ("hours_scheduled", hours), ("hours_remaining", hours)],
            code="leave_type",
        ),
    ])


def write_gold(df: DataFrame, table: str, expected_columns: list, key_columns=()) -> None:
    """Fail closed on an unexpected column or a key that is not a MOCK identifier."""
    if df.columns != expected_columns:
        raise ValueError(f"{table}: unexpected columns {df.columns}")
    for column in key_columns:
        if df.filter(F.col(column).isNotNull() & ~F.col(column).rlike(MOCK_KEY)).limit(1).count():
            raise ValueError(f"{table}: {column} holds a non-MOCK identifier; nothing written")
    # Gold is rebuilt in full from Silver, so a rerun yields the same tables.
    (df.write.format("delta").mode("overwrite").option("overwriteSchema", "true")
     .save(f"{tables_root('gold')}/{table}"))


def build_snapshot_quality(spark: SparkSession) -> DataFrame:
    """Compare manifest-declared and observed rows for monthly snapshot sources."""
    manifest = spark.read.format("delta").load(f"{tables_root('bronze')}/{MANIFEST_TABLE}")
    names = ("employee_snapshot", "requisition_snapshot", "leave_balance_snapshot")
    declared = (manifest.filter(F.col("source_name").isin(*names))
                .withColumn("snapshot_month_end", F.to_date("snapshot_month_end"))
                .groupBy("source_name", "snapshot_month_end")
                .agg(F.sum(F.col("row_count").cast("long")).alias("declared_rows")))
    actuals = []
    for name in names:
        path = f"{tables_root('bronze')}/bronze_{name}"
        actuals.append(
            spark.read.format("delta").load(path)
            .groupBy(F.lit(name).alias("source_name"), F.to_date("snapshot_month_end").alias("snapshot_month_end"))
            .count().withColumnRenamed("count", "bronze_rows")
        )
    actual = reduce(DataFrame.unionByName, actuals)
    return (declared.join(actual, ["source_name", "snapshot_month_end"], "full")
            .withColumn("declared_rows", F.coalesce("declared_rows", F.lit(0)).cast("long"))
            .withColumn("bronze_rows", F.coalesce("bronze_rows", F.lit(0)).cast("long"))
            .withColumn("is_complete", F.col("declared_rows") == F.col("bronze_rows"))
            .withColumn("status", F.when(F.col("is_complete"), "row_count_match")
                        .otherwise("row_count_mismatch"))
            .select("source_name", "snapshot_month_end", "bronze_rows", "declared_rows", "is_complete", "status"))


def append_run_audit(spark: SparkSession, rows: list) -> None:
    """Append run-state and aggregate source counts without row-level values."""
    schema = ("run_id string, run_started_at_utc timestamp, run_finished_at_utc timestamp, "
              "run_status string, source_name string, bronze_rows long, accepted_silver_rows long, "
              "rejected_rows long, details string")
    df = spark.createDataFrame(rows, schema)
    path = f"{tables_root('gold')}/{AUDIT_TABLE}"
    df.write.format("delta").mode("append").save(path) if DeltaTable.isDeltaTable(spark, path) \
        else df.write.format("delta").save(path)


# -------------------------------------------------------------------------- run


def main() -> None:
    spark = SparkSession.builder.getOrCreate()
    loaded_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    run_id = str(uuid.uuid4())
    run_started = datetime.now(timezone.utc)
    for layer in LAYERS:
        print(f"{layer}: {tables_root(layer)}")  # also fails fast on missing settings
    append_run_audit(spark, [(run_id, run_started, None, "started", "__run__", None,
                              None, None, "Synthetic HR template run")])

    # Validate explicit period evidence before staging can mutate Silver.
    validate_snapshot_manifest(spark)

    # Stage every source first so a contract failure stops the run before any write.
    staged = {name: stage_source(spark, name, source, loaded_at)
              for name, source in SOURCES.items()}
    rejects = []
    for name, (accepted, rejected) in staged.items():
        rejects.append(rejected)
        against_history = write_silver(spark, name, SOURCES[name], accepted)
        if against_history is not None:
            rejects.append(against_history)

    # The quarantine table describes the current run only and is replaced each run.
    rejects_path = f"{tables_root('silver')}/{REJECTS_TABLE}"
    (reduce(DataFrame.unionByName, rejects)
     .withColumn("run_started_at_utc", F.lit(loaded_at).cast("timestamp"))
     .write.format("delta").mode("overwrite").option("overwriteSchema", "true")
     .save(rejects_path))
    reject_summary = (
        spark.read.format("delta").load(rejects_path)
        .groupBy("source_table", "reject_reasons").count().collect()
    )
    blocked_sources = {row["source_table"] for row in reject_summary}

    # Persist a compact source scorecard; reject row contents remain in the
    # restricted quarantine table and are never copied to Gold.
    rejected_counts = {}
    for row in reject_summary:
        rejected_counts[row["source_table"]] = rejected_counts.get(row["source_table"], 0) + row["count"]
    scorecard_rows = []
    for name, source in SOURCES.items():
        silver_path = f"{tables_root('silver')}/{source.silver_table}"
        bronze_path = f"{tables_root('bronze')}/bronze_{name}"
        bronze_count = spark.read.format("delta").load(bronze_path).count()
        accepted_count = spark.read.format("delta").load(silver_path).count()
        scorecard_rows.append((run_id, name, None, bronze_count, accepted_count,
                               rejected_counts.get(name, 0), None, None,
                               "blocked_by_rejects" if rejected_counts.get(name, 0) else "source_rows_accepted"))
    score_schema = ("run_id string, source_name string, snapshot_month_end date, bronze_rows long, "
                    "accepted_silver_rows long, rejected_rows long, declared_rows long, "
                    "is_complete boolean, status string")
    scorecard = spark.createDataFrame(scorecard_rows, score_schema)
    snapshot_quality = build_snapshot_quality(spark).withColumn("run_id", F.lit(run_id))
    snapshot_quality = (snapshot_quality
                        .withColumn("accepted_silver_rows", F.lit(None).cast("long"))
                        .withColumn("rejected_rows", F.lit(None).cast("long"))
                        .withColumnRenamed("bronze_rows", "bronze_rows")
                        .withColumnRenamed("status", "status"))
    snapshot_quality = snapshot_quality.select(
        "run_id", "source_name", "snapshot_month_end", "bronze_rows",
        "accepted_silver_rows", "rejected_rows", "declared_rows", "is_complete", "status",
    )
    quality = scorecard.unionByName(snapshot_quality)
    quality_path = f"{tables_root('gold')}/{QUALITY_TABLE}"
    quality.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(quality_path)

    silver = {
        name: spark.read.format("delta").load(f"{tables_root('silver')}/{source.silver_table}")
        for name, source in SOURCES.items()
    }
    write_gold(build_leadership(spark, silver, blocked_sources), LEADERSHIP_TABLE, LEADERSHIP_COLUMNS)
    write_gold(build_analyst_detail(silver), ANALYST_TABLE, ANALYST_COLUMNS,
               key_columns=("subject_key",))

    finished = datetime.now(timezone.utc)
    audit_rows = [(run_id, run_started, finished, "completed", name,
                   spark.read.format("delta").load(f"{tables_root('bronze')}/bronze_{name}").count(),
                   silver[name].count(), rejected_counts.get(name, 0), "")
                  for name, source in SOURCES.items()]
    audit_rows.append((run_id, run_started, finished, "completed", "__run__",
                       sum(row[5] or 0 for row in audit_rows),
                       sum(row[6] or 0 for row in audit_rows),
                       sum(row[7] or 0 for row in audit_rows),
                       "blocked_sources=" + ",".join(sorted(blocked_sources))))
    append_run_audit(spark, audit_rows)

    # Summary: counts and aggregates only; rejected row values are never printed.
    for name, source in SOURCES.items():
        print(f"{source.silver_table}: {silver[name].count()} retained rows")
    for row in sorted(reject_summary, key=lambda r: (r["source_table"], r["reject_reasons"])):
        print(f"REJECTED {row['source_table']}: {row['count']} x {row['reject_reasons']}")
    if blocked_sources:
        print(f"Leadership KPIs blocked for sources with rejected rows: {sorted(blocked_sources)}")
    leadership = spark.read.format("delta").load(f"{tables_root('gold')}/{LEADERSHIP_TABLE}")
    leadership.orderBy("period", "kpi").show(200, truncate=False)


if __name__ == "__main__":
    main()

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
