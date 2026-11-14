"""Offline tests for dlt-normalized retail contract and value diagnostics."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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
class RetailDiagnosticNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.client = SimpleNamespace(
            schema=SimpleNamespace(naming=migration.SnakeCaseNamingConvention()),
            capabilities=SimpleNamespace(casefold_identifier=str.casefold),
        )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.csv_dir = Path(self.temp.name)

    def _inventory(self, columns):
        return [{"table": "customers", "status": "ready", "columns": columns}]

    def _write_customers(self, header, row):
        (self.csv_dir / "customers.csv").write_text(f"{header}\n{row}\n", encoding="utf-8")

    def test_contract_recognizes_normalized_alias_and_preserves_raw_metadata(self):
        inventory = self._inventory([
            {"name": "CustomerID", "type": "TEXT"},
            {"name": "CreatedAt", "type": "TIMESTAMP"},
            {"name": "Country", "type": "TEXT"},
        ])
        report = migration.retail_contract_report(inventory, "sql", self.client)
        customers = next(item for item in report["tables"] if item["table"] == "customers")
        customer_id = next(field for field in customers["field_types"] if field["column"] == "customer_id")
        self.assertEqual(customer_id["source_column"], "CustomerID")
        self.assertEqual(customer_id["normalized_source_column"], "customer_id")
        self.assertNotIn("customer_id", customers["missing_columns"])

    def test_numeric_sql_key_stringification_is_review_required(self):
        inventory = self._inventory([
            {"name": "customer_id", "type": "DECIMAL(12, 2)"},
            {"name": "created_at", "type": "TIMESTAMP"},
            {"name": "country", "type": "TEXT"},
        ])
        report = migration.retail_contract_report(inventory, "sql", self.client)
        customers = next(item for item in report["tables"] if item["table"] == "customers")
        key = next(field for field in customers["field_types"] if field["column"] == "customer_id")
        descriptive = next(field for field in customers["field_types"] if field["column"] == "country")
        self.assertEqual(key["fit"], "numeric_key_stringification_unverified")
        self.assertEqual(descriptive["fit"], "cast_by_model")
        self.assertIn("customers.customer_id", report["review_required_columns"])
        self.assertEqual(migration._sql_type_fit("BIGINT", "string", is_key=True), "cast_by_model")
        self.assertEqual(migration._sql_type_fit("FLOAT", "string", is_key=True), "numeric_key_stringification_unverified")

    def test_profile_counts_values_through_normalized_alias(self):
        self._write_customers("CustomerID,CreatedAt,Country", "c1,2025-01-01T00:00:00Z,AU")
        inventory = self._inventory([
            {"name": "CustomerID", "type": "CSV text"},
            {"name": "CreatedAt", "type": "CSV text"},
            {"name": "Country", "type": "CSV text"},
        ])
        result = migration.profile_retail_values(
            "csv", inventory, self.csv_dir, "*.csv", None, None, {}, 10, 100_000, self.client,
        )
        customer = result["tables"][0]["fields"]["customer_id"]
        self.assertEqual(customer["status"], "available")
        self.assertEqual(customer["source_column"], "CustomerID")
        self.assertEqual(customer["null"], 0)
        self.assertEqual(customer["blank"], 0)
        self.assertEqual(customer["cast_failure"], 0)

    def test_profile_blank_key_semantics_match_silver_ascii_space_rule(self):
        self._write_customers(
            "CustomerID,CreatedAt,Country",
            "  c1  ,2025-01-01T00:00:00Z,AU\n"
            "c1,2025-01-01T00:00:00Z,AU\n"
            "\t,2025-01-01T00:00:00Z,AU\n"
            "\t,2025-01-01T00:00:00Z,AU\n"
            ",2025-01-01T00:00:00Z,AU\n"
            "   ,2025-01-01T00:00:00Z,AU",
        )
        inventory = self._inventory([
            {"name": "CustomerID", "type": "CSV text"},
            {"name": "CreatedAt", "type": "CSV text"},
            {"name": "Country", "type": "CSV text"},
        ])

        result = migration.profile_retail_values(
            "csv", inventory, self.csv_dir, "*.csv", None, None, {}, 10, 100_000, self.client,
        )
        customer = result["tables"][0]

        self.assertFalse(migration._is_ascii_space_blank(None))
        self.assertTrue(migration._is_ascii_space_blank(""))
        self.assertTrue(migration._is_ascii_space_blank("   "))
        self.assertFalse(migration._is_ascii_space_blank("\t"))
        self.assertEqual(customer["fields"]["customer_id"]["blank"], 2)
        self.assertEqual(customer["duplicate_key_counts"]["customer_id"], 1)

    def test_profile_counts_only_ascii_space_only_values_as_blank_keys(self):
        (self.csv_dir / "customers.csv").write_text(
            'customer_id,created_at,country\n"",,AU\n"   ",,AU\n"\t",,AU\n"\t",,AU\n" padded ",,AU\n" padded ",,AU\n',
            encoding="utf-8",
        )
        inventory = self._inventory([
            {"name": "customer_id", "type": "CSV text"},
            {"name": "created_at", "type": "CSV text"},
            {"name": "country", "type": "CSV text"},
        ])
        result = migration.profile_retail_values(
            "csv", inventory, self.csv_dir, "*.csv", None, None, {}, 10, 100_000, self.client,
        )
        customer = result["tables"][0]
        self.assertEqual(customer["fields"]["customer_id"]["blank"], 2)
        self.assertEqual(customer["fields"]["customer_id"]["cast_failure"], 0)
        self.assertEqual(customer["duplicate_key_counts"]["customer_id"], 2)

    def test_profile_uses_raw_csv_table_alias_and_applies_its_exact_mapping(self):
        (self.csv_dir / "OrderItems.csv").write_text(
            "LegacyItemId,order_id,product_id,LegacyQuantity,unit_price\n"
            "i-1,o-1,p-1,2.5,3.25\n",
            encoding="utf-8",
        )
        inventory = migration.inventory_csv_inputs(self.csv_dir, "*.csv", None, count_rows=True)
        self.assertEqual(inventory[0]["table"], "OrderItems")
        mappings = {"orderitems": [
            {"source": "LegacyItemId", "target": "order_item_id", "cast": "string"},
            {"source": "LegacyQuantity", "target": "quantity", "cast": "integer"},
        ]}
        result = migration.profile_retail_values(
            "csv", inventory, self.csv_dir, "*.csv", None, None, mappings, 10, 100_000, self.client,
        )
        item = result["tables"][0]
        self.assertEqual(item["table"], "order_items")
        self.assertEqual(item["fields"]["order_item_id"]["source_column"], "LegacyItemId")
        self.assertEqual(item["fields"]["quantity"]["source_column"], "LegacyQuantity")
        self.assertEqual(item["fields"]["quantity"]["cast_failure"], 1)

    def test_normalized_collision_is_ambiguous_and_blocks_contract_readiness(self):
        inventory = self._inventory([
            {"name": "CustomerID", "type": "TEXT"},
            {"name": "customer_id", "type": "TEXT"},
            {"name": "CreatedAt", "type": "TIMESTAMP"},
            {"name": "Country", "type": "TEXT"},
        ])
        report = migration.retail_contract_report(inventory, "sql", self.client)
        customers = next(item for item in report["tables"] if item["table"] == "customers")
        self.assertEqual(customers["status"], "ambiguous_normalized_columns")
        self.assertEqual(customers["ambiguous_columns"][0]["source_columns"], ["CustomerID", "customer_id"])
        self.assertFalse(report["can_feed_retail_models"])
        self.assertIn("customers.customer_id", report["ambiguous_normalized_columns"])

    def test_normalized_collision_does_not_choose_profile_values(self):
        self._write_customers("CustomerID,customer_id,CreatedAt,Country", "c1,c2,2025-01-01T00:00:00Z,AU")
        inventory = self._inventory([
            {"name": "CustomerID", "type": "CSV text"},
            {"name": "customer_id", "type": "CSV text"},
            {"name": "CreatedAt", "type": "CSV text"},
            {"name": "Country", "type": "CSV text"},
        ])
        result = migration.profile_retail_values(
            "csv", inventory, self.csv_dir, "*.csv", None, None, {}, 10, 100_000, self.client,
        )
        customer = result["tables"][0]["fields"]["customer_id"]
        self.assertEqual(customer["status"], "ambiguous")
        self.assertIsNone(customer["null"])
        self.assertEqual(customer["ambiguous_source_columns"], ["CustomerID", "customer_id"])


if __name__ == "__main__":
    unittest.main()
