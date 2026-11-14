"""Offline tests for explicit destination numeric type profiles.

Nothing here connects to BigQuery or Snowflake. Bound checks are pure Python;
the drift guard asks the installed dlt type mappers which column types they would
emit; the end-to-end case loads only a temporary local DuckDB file.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "migration"))

# Standard-library only: these tests never skip for missing migration dependencies.
profiles = importlib.import_module("destination_type_profiles")

try:
    migration = importlib.import_module("migration.run_migration")
    import dlt
    import duckdb
except ModuleNotFoundError as error:
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name not in {"dlt", "sqlalchemy", "duckdb"}:
        raise
    migration = dlt = duckdb = None

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1
# Literals, not arithmetic: Decimal negation/addition would round to the 28-digit default context.
MAX_DECIMAL = Decimal("99999999999999999999999999999.999999999")  # 29 integer + 9 fractional digits
MIN_DECIMAL = Decimal("-99999999999999999999999999999.999999999")
CLOUD_DESTINATIONS = ("bigquery", "snowflake")
ALL_DESTINATIONS = (*CLOUD_DESTINATIONS, "duckdb")


class ProfileDefinitionTests(unittest.TestCase):
    def test_cloud_profiles_declare_explicit_integer_and_decimal_bounds(self):
        expected_types = {
            "bigquery": ("INT64", "NUMERIC(38,9)"),
            "snowflake": ("NUMBER(19,0)", "NUMBER(38,9)"),
            "duckdb": ("BIGINT", "DECIMAL(38,9)"),
        }
        for destination in ALL_DESTINATIONS:
            with self.subTest(destination=destination):
                profile = profiles.resolve_type_profile(destination)
                self.assertEqual(profile.destination, destination)
                self.assertEqual((profile.integer.minimum, profile.integer.maximum), (INT64_MIN, INT64_MAX))
                self.assertEqual((profile.decimal.precision, profile.decimal.scale), (38, 9))
                self.assertEqual(profile.decimal.max_integer_digits, 29)
                self.assertEqual(
                    (profile.integer.destination_type, profile.decimal.destination_type),
                    expected_types[destination],
                )

    def test_snowflake_integer_bound_is_within_its_number_19_0_column(self):
        profile = profiles.resolve_type_profile("snowflake")
        self.assertLessEqual(profile.integer.maximum, 10 ** 19 - 1)
        self.assertGreaterEqual(profile.integer.minimum, -(10 ** 19 - 1))

    def test_unknown_or_non_string_destination_fails_closed(self):
        for destination in ("postgres", "BigQuery", "", None, 1, ["bigquery"]):
            with self.subTest(destination=destination):
                with self.assertRaises(profiles.UnsupportedTypeProfileError):
                    profiles.resolve_type_profile(destination)

    def test_describe_is_json_safe_and_exact_for_64_bit_bounds(self):
        for destination in ALL_DESTINATIONS:
            with self.subTest(destination=destination):
                description = json.loads(json.dumps(profiles.resolve_type_profile(destination).describe()))
                self.assertEqual(description["algorithm"], profiles.PROFILE_ALGORITHM)
                self.assertEqual(int(description["integer"]["maximum"]), INT64_MAX)
                self.assertEqual(int(description["integer"]["minimum"]), INT64_MIN)
                self.assertEqual(description["decimal"]["max_integer_digits"], 29)

    def test_unknown_reason_codes_cannot_be_constructed(self):
        with self.assertRaises(ValueError):
            profiles.DestinationTypeBoundError("value 123 too large")


class IntegerBoundTests(unittest.TestCase):
    def test_boundaries_are_accepted_and_one_past_is_rejected(self):
        for destination in ALL_DESTINATIONS:
            profile = profiles.resolve_type_profile(destination)
            for accepted in (INT64_MIN, INT64_MIN + 1, -1, 0, 1, INT64_MAX - 1, INT64_MAX):
                with self.subTest(destination=destination, value=accepted):
                    profiles.check_mapped_value(accepted, "integer", profile)
            for rejected in (INT64_MIN - 1, INT64_MAX + 1, 10 ** 19 - 1, -(10 ** 19), 10 ** 40):
                with self.subTest(destination=destination, value=rejected):
                    with self.assertRaises(profiles.DestinationTypeBoundError) as caught:
                        profiles.check_mapped_value(rejected, "integer", profile)
                    self.assertEqual(caught.exception.reason_code, profiles.INTEGER_OUT_OF_RANGE)

    def test_non_int_values_are_rejected_not_coerced(self):
        profile = profiles.resolve_type_profile("bigquery")
        for rejected in (True, 1.0, Decimal("1"), "1"):
            with self.subTest(value=rejected):
                with self.assertRaises(profiles.DestinationTypeBoundError) as caught:
                    profiles.check_mapped_value(rejected, "integer", profile)
                self.assertEqual(caught.exception.reason_code, profiles.UNSUPPORTED_BOUND_VALUE)


class DecimalBoundTests(unittest.TestCase):
    def assertDecimalRejected(self, value: Decimal, reason: str, profile) -> None:
        with self.assertRaises(profiles.DestinationTypeBoundError) as caught:
            profiles.check_mapped_value(value, "numeric", profile)
        self.assertEqual(caught.exception.reason_code, reason)

    def test_precision_and_scale_boundaries(self):
        for destination in ALL_DESTINATIONS:
            profile = profiles.resolve_type_profile(destination)
            accepted = (
                MAX_DECIMAL, MIN_DECIMAL,
                Decimal("99999999999999999999999999999"),   # 29 integer digits
                Decimal("1E+28"),                            # 29 integer digits in exponent form
                Decimal("0.000000001"), Decimal("-0.000000001"),  # scale 9
                Decimal("0"), Decimal("-0"), Decimal("0E-20"), Decimal("0E+40"),
                Decimal("12.50"),
            )
            for value in accepted:
                with self.subTest(destination=destination, value=str(value)):
                    profiles.check_mapped_value(value, "numeric", profile)
            integer_overflow = (
                Decimal("100000000000000000000000000000"),  # 30 integer digits
                -Decimal("100000000000000000000000000000"),
                Decimal("1E+29"),
                Decimal("1E+400"),
            )
            for value in integer_overflow:
                with self.subTest(destination=destination, value=str(value)):
                    self.assertDecimalRejected(value, profiles.DECIMAL_INTEGER_DIGITS_EXCEEDED, profile)
            scale_overflow = (
                Decimal("0.0000000001"),                    # scale 10
                Decimal("-0.0000000001"),
                Decimal("1.0000000001"),
                Decimal("1E-400"),
                Decimal("999999999999999999999999999999.9999999999"),  # both exceeded: scale reported first
            )
            for value in scale_overflow:
                with self.subTest(destination=destination, value=str(value)):
                    self.assertDecimalRejected(value, profiles.DECIMAL_SCALE_EXCEEDED, profile)

    def test_trailing_fractional_zeros_are_not_significant(self):
        profile = profiles.resolve_type_profile("snowflake")
        for value in ("1.5000000000000", "1.000000000000000000000", "-7.10000000000"):
            with self.subTest(value=value):
                profiles.check_mapped_value(Decimal(value), "numeric", profile)

    def test_non_finite_or_non_decimal_values_are_rejected(self):
        profile = profiles.resolve_type_profile("bigquery")
        for value in (Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), 1.5, 1, "1.5"):
            with self.subTest(value=str(value)):
                self.assertDecimalRejected(value, profiles.UNSUPPORTED_BOUND_VALUE, profile)

    def test_null_and_non_numeric_casts_are_not_bounded(self):
        profile = profiles.resolve_type_profile("bigquery")
        for cast in ("integer", "numeric", "string", "date", "timestamp", "boolean"):
            profiles.check_mapped_value(None, cast, profile)
        profiles.check_mapped_value("9" * 100, "string", profile)


class ReasonCodePrivacyTests(unittest.TestCase):
    def test_errors_carry_only_fixed_codes(self):
        profile = profiles.resolve_type_profile("bigquery")
        secret_integer = 98765432109876543210123
        secret_decimal = Decimal("4242424242424242424242424242424.42")
        secret_scale = Decimal("0.31415926535")
        for value, cast in ((secret_integer, "integer"), (secret_decimal, "numeric"), (secret_scale, "numeric")):
            with self.subTest(cast=cast):
                with self.assertRaises(profiles.DestinationTypeBoundError) as caught:
                    profiles.check_mapped_value(value, cast, profile)
                error = caught.exception
                self.assertIn(error.reason_code, profiles.REASON_CODES)
                self.assertEqual(error.args, (error.reason_code,))
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)
                rendered = f"{error!s} {error!r}"
                for fragment in (str(value), "42424242", "31415926", "987654321"):
                    self.assertNotIn(fragment, rendered)

    def test_reason_codes_are_fixed_snake_case_identifiers(self):
        for code in profiles.REASON_CODES:
            self.assertRegex(code, r"^[a-z][a-z_]+[a-z]$")

    def test_acceptance_report_allowlists_every_profile_reason_code(self):
        acceptance_report = importlib.import_module("acceptance_report")
        self.assertTrue(profiles.REASON_CODES <= acceptance_report._REJECT_REASONS)


class _RecordingBudget:
    def __init__(self):
        self.tables: dict[str, dict[str, int]] = {}
        self.rejections: list[tuple[str, int | None, str]] = []

    def reject(self, table, ordinal, reason):
        self.rejections.append((table, ordinal, reason))


@unittest.skipIf(migration is None, "migration runtime dependencies are not installed")
class LoaderIntegrationTests(unittest.TestCase):
    RULES = [
        {"source": "Qty", "target": "quantity", "cast": "integer"},
        {"source": "Price", "target": "unit_price", "cast": "numeric"},
    ]

    def setUp(self):
        self.profile = profiles.resolve_type_profile("bigquery")

    def test_boundary_values_pass_through_unchanged(self):
        mapped = migration.apply_mappings(
            {"Qty": str(INT64_MAX), "Price": str(MAX_DECIMAL)}, self.RULES, self.profile
        )
        self.assertEqual(mapped["quantity"], INT64_MAX)
        self.assertEqual(mapped["unit_price"], MAX_DECIMAL)

    def test_apply_mappings_without_profile_keeps_cast_only_semantics(self):
        mapped = migration.apply_mappings({"Qty": str(INT64_MAX + 1), "Price": "1"}, self.RULES)
        self.assertEqual(mapped["quantity"], INT64_MAX + 1)

    def test_rejects_record_profile_reason_codes_without_values(self):
        cases = (
            ({"Qty": str(INT64_MAX + 1), "Price": "1"}, profiles.INTEGER_OUT_OF_RANGE),
            ({"Qty": str(INT64_MIN - 1), "Price": "1"}, profiles.INTEGER_OUT_OF_RANGE),
            ({"Qty": "1", "Price": "1" + "0" * 29}, profiles.DECIMAL_INTEGER_DIGITS_EXCEEDED),
            ({"Qty": "1", "Price": "0.0000000001"}, profiles.DECIMAL_SCALE_EXCEEDED),
            ({"Qty": "1.5", "Price": "1"}, "mapping_cast_failed"),
            ({"Qty": "1", "Price": "NaN"}, "mapping_cast_failed"),
        )
        for row, reason in cases:
            with self.subTest(reason=reason, row=row):
                budget = _RecordingBudget()
                with self.assertRaises(migration.ExtractionRejected) as caught:
                    migration.apply_mappings_with_rejects(row, "lines", self.RULES, budget, 3, type_profile=self.profile)
                self.assertEqual(budget.rejections, [("lines", 3, reason)])
                error = caught.exception
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)
                rendered = f"{error!s} {error!r}"
                for value in row.values():
                    self.assertNotIn(value, rendered)

    def test_load_path_requires_a_profile(self):
        budget = _RecordingBudget()
        with self.assertRaises(TypeError):
            migration.apply_mappings_with_rejects({"Qty": "1"}, "lines", self.RULES[:1], budget)  # type: ignore[call-arg]
        for missing in (None, "bigquery", SimpleNamespace(integer=None)):
            with self.subTest(profile=missing):
                with self.assertRaisesRegex(ValueError, "type profile is required"):
                    migration.apply_mappings_with_rejects(
                        {"Qty": "1"}, "lines", self.RULES[:1], budget, type_profile=missing
                    )
                with self.assertRaisesRegex(ValueError, "type profile is required"):
                    migration.make_csv_resources(
                        Path("."), "*.csv", None, "run", "now", set(), budget, None, {}, {}, {},
                        type_profile=missing,
                    )
                with self.assertRaisesRegex(ValueError, "type profile is required"):
                    migration.make_sql_source(
                        None, "main", [], "run", "now", budget, None, "", "", {}, type_profile=missing
                    )
        self.assertEqual(budget.rejections, [])

    def test_every_cli_destination_has_a_profile(self):
        destination_action = next(
            action for action in migration.build_parser()._actions if action.dest == "destination"
        )
        for destination in destination_action.choices:
            with self.subTest(destination=destination):
                self.assertEqual(profiles.resolve_type_profile(destination).destination, destination)


@unittest.skipIf(dlt is None, "migration runtime dependencies are not installed")
class DltTypeMapperDriftTests(unittest.TestCase):
    """Fail if dlt stops emitting the column types each profile documents."""

    def test_profile_types_match_dlt_hint_mapping(self):
        table = {"name": "lines", "columns": {}}
        for destination in ALL_DESTINATIONS:
            with self.subTest(destination=destination):
                profile = profiles.resolve_type_profile(destination)
                capabilities = getattr(dlt.destinations, destination)().capabilities()
                self.assertEqual(
                    tuple(capabilities.decimal_precision), (profile.decimal.precision, profile.decimal.scale)
                )
                mapper = capabilities.get_type_mapper()
                for hint, expected in (
                    (migration._MAPPING_DLT_DATA_TYPES["integer"], profile.integer.destination_type),
                    (migration._MAPPING_DLT_DATA_TYPES["numeric"], profile.decimal.destination_type),
                ):
                    column = {"name": "value", "data_type": hint}
                    self.assertEqual(
                        mapper.to_destination_type(column, table).replace(" ", "").upper(),
                        expected.replace(" ", "").upper(),
                    )


@unittest.skipIf(migration is None or duckdb is None, "migration runtime dependencies are not installed")
class DuckDBProfileEndToEndTests(unittest.TestCase):
    """Boundary values load exactly; any out-of-profile row stops before load and leaves the target unchanged."""

    HEADER = "line_id,Qty,Price\n"

    def test_boundaries_load_and_overflows_fail_before_load(self) -> None:
        from scripts.tests._dlt_isolated_runtime import isolated_environment, prepare_runtime, run_script

        previous_umask = os.umask(0o077)
        try:
            with tempfile.TemporaryDirectory(prefix="migration-type-profile-") as temporary:
                root = Path(temporary)
                runtime = prepare_runtime(root)
                env = isolated_environment(runtime, root)
                csv_dir = root / "csv"
                csv_dir.mkdir()
                source = csv_dir / "lines.csv"
                mapping_path = root / "mapping.json"
                mapping_path.write_text(json.dumps({
                    "version": 1,
                    "tables": {"lines": LoaderIntegrationTests.RULES},
                }), encoding="utf-8")
                source.write_text(
                    self.HEADER
                    + f"l1,{INT64_MAX},{MAX_DECIMAL}\n"
                    + f"l2,{INT64_MIN},{MIN_DECIMAL}\n"
                    + "l3,0,12.5000000000000\n",
                    encoding="utf-8",
                )
                base_args = [
                    "--source", "csv", "--destination", "duckdb",
                    "--dataset", "starterkit_migration_type_profile",
                    "--pipeline-name", "type_profile",
                    "--csv-dir", str(csv_dir), "--tables", "lines",
                    "--mapping-file", str(mapping_path),
                ]

                def invoke(*args: str, expected: int = 0) -> str:
                    result = run_script(runtime, "run_migration.py", list(args), env)
                    output = result.stdout + result.stderr
                    self.assertEqual(result.returncode, expected, output)
                    return output

                dry = json.loads(invoke(*base_args, "--dry-run"))
                self.assertEqual(dry["destination_type_profile"]["name"], "duckdb_dlt_default_v1")
                confirm = [
                    "--confirm-target", dry["target_identity"],
                    "--confirm-tables", dry["table_confirmation"],
                    "--confirm-schema-fingerprint", dry["schema_confirmation_fingerprint"],
                ]
                invoke(*base_args, *confirm)
                manifest_dir = runtime / "run-manifests"
                manifests = [p for p in manifest_dir.glob("*.json") if not p.name.startswith("evidence-index-")]
                self.assertEqual(len(manifests), 1)
                baseline = json.loads(manifests[0].read_text(encoding="utf-8"))
                self.assertEqual(baseline["status"], "completed")
                self.assertEqual(baseline["destination_type_profile"]["name"], "duckdb_dlt_default_v1")
                database, _, namespace = baseline["target_identity"].removeprefix("duckdb/").rpartition("/")

                def rows():
                    with duckdb.connect(database, read_only=True) as destination:
                        return destination.execute(
                            f'SELECT line_id, quantity, typeof(quantity), unit_price, typeof(unit_price) '
                            f'FROM "{namespace}"."lines" ORDER BY line_id'
                        ).fetchall()

                loaded = rows()
                self.assertEqual(loaded, [
                    ("l1", INT64_MAX, "BIGINT", MAX_DECIMAL, "DECIMAL(38,9)"),
                    ("l2", INT64_MIN, "BIGINT", MIN_DECIMAL, "DECIMAL(38,9)"),
                    ("l3", 0, "BIGINT", Decimal("12.5"), "DECIMAL(38,9)"),
                ])

                overflow_cases = (
                    ("integer_above_max", f"{INT64_MAX + 1},1", profiles.INTEGER_OUT_OF_RANGE),
                    ("integer_below_min", f"{INT64_MIN - 1},1", profiles.INTEGER_OUT_OF_RANGE),
                    ("decimal_integer_digits", "1,1" + "0" * 29, profiles.DECIMAL_INTEGER_DIGITS_EXCEEDED),
                    ("decimal_scale", "1,3.1415926535", profiles.DECIMAL_SCALE_EXCEEDED),
                )
                for label, bad_fields, reason in overflow_cases:
                    with self.subTest(case=label):
                        source.write_text(self.HEADER + "ok,1,1\n" + f"bad,{bad_fields}\n", encoding="utf-8")
                        before = {p.name for p in manifest_dir.glob("*.json")}
                        output = invoke(*base_args, *confirm, expected=1)
                        new = [
                            p for p in manifest_dir.glob("*.json")
                            if p.name not in before and not p.name.startswith("evidence-index-")
                        ]
                        self.assertEqual(len(new), 1)
                        failed = json.loads(new[0].read_text(encoding="utf-8"))
                        self.assertEqual(failed["status"], "failed")
                        self.assertFalse(failed["destination_write_may_be_partial"])
                        self.assertEqual(failed["extraction_reject_summary"]["reason_code"], reason)
                        self.assertEqual(failed["extraction_reject_summary"]["row_ordinal"], 2)
                        self.assertEqual(failed["extraction_reject_summary"]["accepted_rows_total"], 1)
                        rejected_value = bad_fields.split(",")[0 if label.startswith("integer_") else 1]
                        # The profile description legitimately prints the int64 bounds; check everything else.
                        self.assertEqual(failed.pop("destination_type_profile"), baseline["destination_type_profile"])
                        self.assertNotIn(rejected_value, json.dumps(failed))
                        self.assertNotIn("Price", json.dumps(failed.get("extraction_reject_summary")))
                        self.assertNotIn(rejected_value, output)
                        self.assertEqual(rows(), loaded)
        finally:
            os.umask(previous_umask)


if __name__ == "__main__":
    unittest.main()
