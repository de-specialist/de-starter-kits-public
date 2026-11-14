"""Run both editions' rendered Silver SQL against synthetic DuckDB Bronze tables."""

from __future__ import annotations

import re
import importlib
import os
import unittest
from pathlib import Path

def _load_duckdb(importer=importlib.import_module, *, ci: bool | None = None):
    try:
        return importer("duckdb")
    except ModuleNotFoundError as error:
        running_in_ci = (
            os.environ.get("CI", "").casefold() in {"1", "true", "yes"}
            if ci is None else ci
        )
        if running_in_ci or error.name != "duckdb":
            raise
        return None


duckdb = _load_duckdb()

ROOT = Path(__file__).resolve().parents[2]
EDITION_ROOTS = (ROOT / "editions" / "Bigquery", ROOT / "editions" / "Snowflake" / "kit")


class DuckDBImportPolicyTests(unittest.TestCase):
    def test_only_missing_duckdb_can_skip_outside_ci(self):
        def missing_duckdb(_name):
            raise ModuleNotFoundError("missing duckdb", name="duckdb")

        self.assertIsNone(_load_duckdb(missing_duckdb, ci=False))
        with self.assertRaisesRegex(ModuleNotFoundError, "missing duckdb"):
            _load_duckdb(missing_duckdb, ci=True)

    def test_missing_transitive_dependency_is_never_skipped(self):
        def missing_internal(_name):
            raise ModuleNotFoundError("missing duckdb dependency", name="duckdb_engine")

        with self.assertRaisesRegex(ModuleNotFoundError, "missing duckdb dependency"):
            _load_duckdb(missing_internal, ci=False)


def render_model(sql: str, source_table: str) -> str:
    sql = re.sub(r"\{\{\s*dbt\.type_string\(\)\s*\}\}", "varchar", sql)
    sql = re.sub(r"\{\{\s*dbt\.type_timestamp\(\)\s*\}\}", "timestamp", sql)
    sql = re.sub(r"\{\{\s*dbt\.type_numeric\(\)\s*\}\}", "decimal(38, 9)", sql)
    sql = re.sub(
        r"\{\{\s*source\('migration_bronze',\s*'[^']+'\)\s*\}\}",
        source_table,
        sql,
    )
    if "{{" in sql or "}}" in sql:
        raise AssertionError("Unexpected unrendered dbt expression in Silver SQL")
    return sql


@unittest.skipIf(duckdb is None, "duckdb is required for the offline Silver SQL regression")
class SilverBlankKeyTests(unittest.TestCase):
    def _execute(self, edition_root: Path, model: str, bronze_table: str, setup_sql: str):
        connection = duckdb.connect(database=":memory:")
        self.addCleanup(connection.close)
        connection.execute(setup_sql)
        model_sql = (edition_root / "models" / "migration" / "silver" / f"silver_{model}.sql").read_text(encoding="utf-8")
        return connection.execute(render_model(model_sql, bronze_table)).fetchall(), connection

    def test_primary_keys_exclude_only_null_empty_or_ascii_space_values(self):
        for edition in EDITION_ROOTS:
            for model, table, setup in (
                ("customers", "customers", """
                    create table customers(customer_id varchar, created_at timestamp, country varchar, _ingested_at_utc timestamp);
                    insert into customers values
                      (null, null, 'AU', current_timestamp), ('', null, 'AU', current_timestamp),
                      ('   ', null, 'AU', current_timestamp), ('id', null, 'AU', current_timestamp),
                      (' id', null, 'AU', current_timestamp), (chr(9), null, 'AU', current_timestamp);
                """),
                ("products", "products", """
                    create table products(product_id varchar, product_name varchar, category varchar, _ingested_at_utc timestamp);
                    insert into products values
                      (null, 'Null', 'x', current_timestamp), ('', 'Empty', 'x', current_timestamp),
                      ('  ', 'Spaces', 'x', current_timestamp), ('p', 'Plain', 'x', current_timestamp),
                      (' p', 'Padded', 'x', current_timestamp), (chr(9), 'Tab', 'x', current_timestamp);
                """),
                ("orders", "orders", """
                    create table orders(order_id varchar, customer_id varchar, order_status varchar, order_date date,
                                        updated_at timestamp, _ingested_at_utc timestamp);
                    insert into orders values
                      (null, 'c', 'complete', date '2025-01-01', current_timestamp, current_timestamp),
                      ('', 'c', 'complete', date '2025-01-01', current_timestamp, current_timestamp),
                      ('   ', 'c', 'complete', date '2025-01-01', current_timestamp, current_timestamp),
                      ('o', null, 'complete', date '2025-01-01', current_timestamp, current_timestamp),
                      (' o', ' c ', 'complete', date '2025-01-01', current_timestamp, current_timestamp),
                      ('o2', '', 'complete', date '2025-01-01', current_timestamp, current_timestamp),
                      ('o3', '   ', 'complete', date '2025-01-01', current_timestamp, current_timestamp),
                      (chr(9), 'c', 'complete', date '2025-01-01', current_timestamp, current_timestamp);
                """),
                ("order_items", "order_items", """
                    create table order_items(order_item_id varchar, order_id varchar, product_id varchar,
                                             quantity integer, unit_price decimal(10, 2), _ingested_at_utc timestamp);
                    insert into order_items values
                      (null, 'o', 'p', 1, 2, current_timestamp), ('', 'o', 'p', 1, 2, current_timestamp),
                      ('   ', 'o', 'p', 1, 2, current_timestamp), ('i', null, '', 1, 2, current_timestamp),
                      (' i', ' o ', '  ', 1, 2, current_timestamp),
                      (chr(9), 'o', 'p', 1, 2, current_timestamp);
                """),
            ):
                with self.subTest(edition=str(edition), model=model):
                    result, connection = self._execute(edition, model, table, setup)
                    primary_key = {"customers": "customer_id", "products": "product_id",
                                   "orders": "order_id", "order_items": "order_item_id"}[model]
                    position = {"customers": 0, "products": 0, "orders": 0, "order_items": 0}[model]
                    keys = {row[position] for row in result}
                    expected = {"id", " id"} if model == "customers" else (
                        {"p", " p", "\t"} if model == "products" else (
                            {"o", " o", "o2", "o3", "\t"} if model == "orders" else {"i", " i", "\t"}
                        )
                    )
                    if model == "customers":
                        expected.add("\t")
                    self.assertEqual(keys, expected, f"{primary_key} key filtering changed")
                    # Silver is a read-only projection; original Bronze blanks remain present.
                    self.assertGreater(connection.execute(f"select count(*) from {table}").fetchone()[0], len(result))

    def test_foreign_keys_become_null_without_trimming_padded_nonblank_values(self):
        for edition in EDITION_ROOTS:
            with self.subTest(edition=str(edition)):
                orders, _ = self._execute(edition, "orders", "orders", """
                    create table orders(order_id varchar, customer_id varchar, order_status varchar, order_date date,
                                        updated_at timestamp, _ingested_at_utc timestamp);
                    insert into orders values
                      ('o1', null, 'complete', date '2025-01-01', current_timestamp, current_timestamp),
                      ('o2', '', 'complete', date '2025-01-01', current_timestamp, current_timestamp),
                      ('o3', '   ', 'complete', date '2025-01-01', current_timestamp, current_timestamp),
                      ('o4', ' c ', 'complete', date '2025-01-01', current_timestamp, current_timestamp);
                """)
                orders_by_id = {row[0]: row[1] for row in orders}
                self.assertIsNone(orders_by_id["o1"])
                self.assertIsNone(orders_by_id["o2"])
                self.assertIsNone(orders_by_id["o3"])
                self.assertEqual(orders_by_id["o4"], " c ")

                items, _ = self._execute(edition, "order_items", "order_items", """
                    create table order_items(order_item_id varchar, order_id varchar, product_id varchar,
                                             quantity integer, unit_price decimal(10, 2), _ingested_at_utc timestamp);
                    insert into order_items values
                      ('i1', null, '', 1, 2, current_timestamp),
                      ('i2', '', '   ', 1, 2, current_timestamp),
                      ('i3', ' o ', ' p ', 1, 2, current_timestamp);
                """)
                items_by_id = {row[0]: (row[1], row[2]) for row in items}
                self.assertEqual(items_by_id["i1"], (None, None))
                self.assertEqual(items_by_id["i2"], (None, None))
                self.assertEqual(items_by_id["i3"], (" o ", " p "))

    def test_descriptive_fields_trim_only_ascii_spaces_identically(self):
        for edition in EDITION_ROOTS:
            with self.subTest(edition=str(edition)):
                customers, _ = self._execute(edition, "customers", "customers", """
                    create table customers(customer_id varchar, created_at timestamp, country varchar, _ingested_at_utc timestamp);
                    insert into customers values
                      ('c-space', null, '  AU  ', current_timestamp),
                      ('c-empty', null, '   ', current_timestamp),
                      ('c-tab', null, chr(9) || 'AU' || chr(9), current_timestamp),
                      ('c-unicode', null, chr(160) || 'AU' || chr(160), current_timestamp);
                """)
                country_by_id = {row[0]: row[2] for row in customers}
                self.assertEqual(country_by_id["c-space"], "AU")
                self.assertIsNone(country_by_id["c-empty"])
                self.assertEqual(country_by_id["c-tab"], "\tAU\t")
                self.assertEqual(country_by_id["c-unicode"], "\u00a0AU\u00a0")

                products, _ = self._execute(edition, "products", "products", """
                    create table products(product_id varchar, product_name varchar, category varchar, _ingested_at_utc timestamp);
                    insert into products values
                      ('p-space', '  Tote  ', ' Bags ', current_timestamp),
                      ('p-empty', '   ', '   ', current_timestamp),
                      ('p-tab', chr(9) || 'Mug' || chr(9), chr(9), current_timestamp),
                      ('p-unicode', chr(160) || 'Book' || chr(160), chr(160) || 'Paper' || chr(160), current_timestamp);
                """)
                products_by_id = {row[0]: (row[1], row[2]) for row in products}
                self.assertEqual(products_by_id["p-space"], ("Tote", "Bags"))
                self.assertEqual(products_by_id["p-empty"], (None, None))
                self.assertEqual(products_by_id["p-tab"], ("\tMug\t", "\t"))
                self.assertEqual(products_by_id["p-unicode"], ("\u00a0Book\u00a0", "\u00a0Paper\u00a0"))

                orders, _ = self._execute(edition, "orders", "orders", """
                    create table orders(order_id varchar, customer_id varchar, order_status varchar, order_date date,
                                        updated_at timestamp, _ingested_at_utc timestamp);
                    insert into orders values
                      ('o-space', 'c', '  COMPLETE  ', date '2025-01-01', current_timestamp, current_timestamp),
                      ('o-tab', 'c', chr(9) || 'RETURNED' || chr(9), date '2025-01-01', current_timestamp, current_timestamp),
                      ('o-unicode', 'c', chr(160) || 'PENDING' || chr(160), date '2025-01-01', current_timestamp, current_timestamp);
                """)
                status_by_id = {row[0]: row[2] for row in orders}
                self.assertEqual(status_by_id["o-space"], "complete")
                self.assertEqual(status_by_id["o-tab"], "\treturned\t")
                self.assertEqual(status_by_id["o-unicode"], "\u00a0pending\u00a0")


if __name__ == "__main__":
    unittest.main()
