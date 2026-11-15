"""Regression checks for the synthetic source-adaptation walkthrough."""

from __future__ import annotations

import csv
import json
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "migration" / "examples" / "source-adaptation"
MAPPING_PATH = ROOT / "migration" / "mappings" / "retail-source.example.json"
TABLES = ("customers", "products", "orders", "order_items")
PRIMARY_KEYS = {
    "customers": "customer_number",
    "products": "sku",
    "orders": "order_number",
    "order_items": "line_id",
}


def read_rows(table: str) -> tuple[list[str], list[dict[str, str]]]:
    with (FIXTURE_DIR / f"{table}.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


class SourceAdaptationFixtureTests(unittest.TestCase):
    def test_mapping_sources_exist_and_targets_cover_canonical_columns(self) -> None:
        mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
        self.assertEqual(mapping["version"], 1)
        self.assertEqual(set(mapping["tables"]), set(TABLES))

        for table in TABLES:
            with self.subTest(table=table):
                headers, _ = read_rows(table)
                rules = mapping["tables"][table]
                self.assertTrue(rules)
                self.assertTrue({rule["source"] for rule in rules} <= set(headers))
                self.assertEqual(len({rule["source"] for rule in rules}), len(rules))
                self.assertEqual(len({rule["target"] for rule in rules}), len(rules))

    def test_synthetic_keys_and_relationships_match_table_grains(self) -> None:
        records = {table: read_rows(table)[1] for table in TABLES}
        all_ids = []
        for table, key in PRIMARY_KEYS.items():
            values = [row[key] for row in records[table]]
            self.assertEqual(len(values), len(set(values)), table)
            self.assertTrue(all(value.startswith("MOCK-") for value in values), table)
            all_ids.extend(values)

        customer_ids = {row["customer_number"] for row in records["customers"]}
        product_ids = {row["sku"] for row in records["products"]}
        order_ids = {row["order_number"] for row in records["orders"]}
        self.assertTrue(all(row["buyer_id"] in customer_ids for row in records["orders"]))
        self.assertTrue(all(row["order_number"] in order_ids for row in records["order_items"]))
        self.assertTrue(all(row["sku"] in product_ids for row in records["order_items"]))
        self.assertTrue(all(identifier.startswith("MOCK-") for identifier in all_ids))

    def test_order_change_time_is_distinct_from_business_date_and_delete(self) -> None:
        _, orders = read_rows("orders")
        cancelled = next(row for row in orders if row["status"] == "cancelled")
        changed_at = datetime.fromisoformat(cancelled["modified_at"].replace("Z", "+00:00"))
        event_date = date.fromisoformat(cancelled["purchase_date"])
        self.assertGreater(changed_at.date(), event_date)
        self.assertNotIn("deleted_at", cancelled)
        _, order_items = read_rows("order_items")
        self.assertTrue(all(int(row["units"]) > 0 for row in order_items))
        self.assertTrue(all(Decimal(row["item_price"]) > 0 for row in order_items))


if __name__ == "__main__":
    unittest.main()
