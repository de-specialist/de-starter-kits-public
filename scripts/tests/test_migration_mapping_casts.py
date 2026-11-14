"""Offline contract tests for explicit migration mapping casts."""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "migration"))
try:
    migration = importlib.import_module("migration.run_migration")
except ModuleNotFoundError as error:
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name not in {"dlt", "sqlalchemy"}:
        raise
    migration = None


@unittest.skipIf(migration is None, "migration runtime dependencies are not installed")
class MappingCastContractTests(unittest.TestCase):
    def assertCast(self, value, cast, expected):
        self.assertEqual(migration._cast_mapped_value(value, cast), expected)

    def assertRejected(self, value, cast):
        with self.assertRaisesRegex(ValueError, "mapped_value_cast_failed"):
            migration._cast_mapped_value(value, cast)

    def test_string_cast_accepts_general_values_and_preserves_blanks(self):
        self.assertCast(42, "string", "42")
        self.assertCast("", "string", "")
        self.assertCast("   ", "string", "   ")
        self.assertCast(None, "string", None)
        self.assertRejected(b"hello", "string")
        self.assertRejected(bytearray(b"hello"), "string")
        self.assertRejected(memoryview(b"hello"), "string")

    def test_integer_cast_rejects_booleans_fractional_values_and_blanks(self):
        self.assertCast("+12", "integer", 12)
        self.assertCast(" 12 ", "integer", 12)
        self.assertCast(12.0, "integer", 12)
        self.assertCast(Decimal("12.00"), "integer", 12)
        self.assertCast(Fraction(12, 1), "integer", 12)
        self.assertCast(None, "integer", None)
        for rejected in (True, False, 12.5, Decimal("12.1"), Fraction(5, 2), "12.5", ""):
            with self.subTest(value=rejected):
                self.assertRejected(rejected, "integer")

    def test_numeric_cast_returns_decimal_and_rejects_non_finite_and_blanks(self):
        self.assertCast("-12.50", "numeric", Decimal("-12.50"))
        self.assertCast(10, "numeric", Decimal("10"))
        self.assertCast(None, "numeric", None)
        for rejected in ("", "   ", "NaN", "Infinity", float("nan"), float("inf")):
            with self.subTest(value=rejected):
                self.assertRejected(rejected, "numeric")

    def test_date_cast_accepts_iso_date_only(self):
        self.assertCast("2025-03-04", "date", date(2025, 3, 4))
        self.assertCast(date(2025, 3, 4), "date", date(2025, 3, 4))
        self.assertCast(None, "date", None)
        for rejected in ("", "2025-02-30", "2025-03-04T00:00:00", datetime(2025, 3, 4)):
            with self.subTest(value=rejected):
                self.assertRejected(rejected, "date")

    def test_timestamp_accepts_offset_and_naive_iso_values_without_normalizing(self):
        offset = migration._cast_mapped_value("2025-03-04T10:30:00+10:00", "timestamp")
        self.assertEqual(offset, datetime(2025, 3, 4, 10, 30, tzinfo=timezone(timedelta(hours=10))))
        self.assertEqual(offset.utcoffset().total_seconds(), 10 * 60 * 60)
        naive = migration._cast_mapped_value("2025-03-04T10:30:00", "timestamp")
        self.assertEqual(naive, datetime(2025, 3, 4, 10, 30))
        self.assertIsNone(naive.tzinfo)
        self.assertEqual(
            migration._cast_mapped_value("2025-03-04T10:30:00.123456Z", "timestamp").microsecond,
            123456,
        )
        self.assertCast(None, "timestamp", None)
        for rejected in ("", "not-a-timestamp", "2025-03-04T10:30:00.1234567Z"):
            with self.subTest(value=rejected):
                self.assertRejected(rejected, "timestamp")

    def test_boolean_cast_accepts_only_documented_true_false_aliases(self):
        for value in (True, "true", "TRUE", "1", " yes ", "Y"):
            with self.subTest(value=value):
                self.assertCast(value, "boolean", True)
        for value in (False, "false", "FALSE", "0", " no ", "N"):
            with self.subTest(value=value):
                self.assertCast(value, "boolean", False)
        self.assertCast(None, "boolean", None)
        for rejected in ("", " ", "t", "f", "on", "off", 2):
            with self.subTest(value=rejected):
                self.assertRejected(rejected, "boolean")


if __name__ == "__main__":
    unittest.main()
