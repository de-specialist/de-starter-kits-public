"""Offline golden tests for the BigQuery Silver scaffold renderer (item 145).

The renderer is pure: these tests only call it on synthetic plans and compare text. No
dbt, warehouse, credentials, or network is used. The dbt parse check of the golden
output is a separate manual validation recorded in the task handoff.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "migration"))

from scaffold.templates.bigquery import (  # noqa: E402
    TemplatePlanError,
    output_dir,
    render_bigquery_silver,
)

try:
    import yaml
except ModuleNotFoundError:
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"}:
        raise
    yaml = None

FIXTURES = ROOT / "migration" / "tests" / "fixtures" / "scaffold" / "bigquery"
GATE = "target.name == 'migration' and (var('enable_migration_models', false) | as_bool)"


def load_plan() -> dict:
    return json.loads((FIXTURES / "plan.json").read_text(encoding="utf-8"))


def table(plan: dict, entity: str) -> dict:
    return next(item for item in plan["tables"] if item["entity"] == entity)


def column(plan: dict, entity: str, name: str) -> dict:
    return next(item for item in table(plan, entity)["columns"] if item["canonical_name"] == name)


class GoldenOutputTests(unittest.TestCase):
    def test_matches_golden_bytes(self) -> None:
        expected_dir = FIXTURES / "expected"
        expected = {path.name: path.read_bytes() for path in sorted(expected_dir.iterdir())}
        rendered = render_bigquery_silver(load_plan())
        self.assertEqual(sorted(rendered), sorted(expected))
        for name, text in rendered.items():
            self.assertEqual(text.encode("utf-8"), expected[name], name)

    def test_deterministic_and_order_independent(self) -> None:
        plan = load_plan()
        first = render_bigquery_silver(plan)
        self.assertEqual(first, render_bigquery_silver(load_plan()))
        shuffled = copy.deepcopy(plan)
        shuffled["tables"].reverse()
        for item in shuffled["tables"]:
            item["columns"].reverse()
        self.assertEqual(first, render_bigquery_silver(shuffled))
        self.assertEqual(list(first), sorted(first))

    def test_text_hygiene(self) -> None:
        for name, text in render_bigquery_silver(load_plan()).items():
            self.assertNotIn("\r", text, name)
            self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"), name)
            self.assertNotRegex(text, r"[ \t]\n", name)
            self.assertNotIn("/home", text, name)
            self.assertNotRegex(text, r"\d{4}-\d{2}-\d{2}", name)
            self.assertTrue(text.isascii(), name)

    def test_accepts_attribute_objects(self) -> None:
        @dataclass(frozen=True)
        class Obj:
            data: dict

            def __getattr__(self, key: str):
                value = self.__dict__["data"][key]
                if isinstance(value, dict):
                    return Obj(value)
                if isinstance(value, list):
                    return [Obj(v) if isinstance(v, dict) else v for v in value]
                return value

        plan = load_plan()
        self.assertEqual(render_bigquery_silver(plan), render_bigquery_silver(Obj(plan)))

    def test_output_dir(self) -> None:
        self.assertEqual(output_dir("synthetic_retail"), "models/migration/generated/synthetic_retail")
        with self.assertRaises(TemplatePlanError):
            output_dir("../x")


class SqlContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.files = render_bigquery_silver(load_plan())
        self.sql = {name: text for name, text in self.files.items() if name.endswith(".sql")}

    def test_one_model_per_canonical_table(self) -> None:
        self.assertEqual(
            sorted(self.sql),
            [f"silver_synthetic_retail__{t}.sql" for t in ("customers", "order_items", "orders", "products")],
        )

    def test_explicit_config_and_gate(self) -> None:
        for name, text in self.sql.items():
            self.assertIn("materialized='view'", text, name)
            self.assertIn("schema='silver'", text, name)
            self.assertIn("tags=['scaffolded']", text, name)
            self.assertIn(f"enabled=({GATE})", text, name)
            self.assertNotIn("medallion", text, name)
            self.assertTrue(text.startswith("-- scaffolded: editable Silver starter model"), name)
            self.assertIn("Ownership: operator-owned after publication", text, name)
            self.assertIn("No deduplication", text, name)
            self.assertNotIn("row_number", text, name)
            self.assertNotIn("safe_cast", text, name)

    def test_selects_only_canonical_and_provenance_columns(self) -> None:
        expected = {
            "customers": ["customer_id", "created_at", "country"],
            "order_items": ["order_item_id", "order_id", "product_id", "quantity", "unit_price"],
            "orders": ["order_id", "customer_id", "order_status", "order_date", "updated_at"],
            "products": ["product_id", "product_name", "category"],
        }
        for entity, columns in expected.items():
            text = self.sql[f"silver_synthetic_retail__{entity}.sql"]
            body = text.split("select\n", 1)[1].split("\nfrom ", 1)[0]
            aliases = re.findall(r"(?:\bas (\w+),?|^    (_\w+),?)$", body, flags=re.MULTILINE)
            names = [a or b for a, b in aliases]
            self.assertEqual(names, columns + ["_migration_run_id", "_ingested_at_utc"], entity)
            self.assertIn(f"{{{{ source('migration_bronze_synthetic_retail', '{entity}') }}}}", text)

    def test_bigquery_casts_and_key_policy(self) -> None:
        items = self.sql["silver_synthetic_retail__order_items.sql"]
        self.assertIn("cast(`units` as int64) as quantity", items)
        self.assertIn("cast(`item_price` as numeric) as unit_price", items)
        self.assertIn("when replace(cast(`sku` as string), ' ', '') = '' then cast(null as string)", items)
        self.assertIn("where cast(`line_id` as string) is not null\n  and replace(cast(`line_id` as string), ' ', '') <> ''", items)
        orders = self.sql["silver_synthetic_retail__orders.sql"]
        self.assertIn("cast(`purchase_date` as date) as order_date", orders)
        self.assertIn("REVIEW: cast placeholders", orders)
        self.assertIn("order_date (date), updated_at (timestamp)", orders)
        self.assertNotIn("REVIEW", self.sql["silver_synthetic_retail__products.sql"])

    def test_integer_and_boolean_keys(self) -> None:
        plan = load_plan()
        column(plan, "order_items", "order_item_id")["cast"] = "integer"
        column(plan, "order_items", "product_id")["cast"] = "integer"
        column(plan, "products", "category")["cast"] = "boolean"
        files = render_bigquery_silver(plan)
        items = files["silver_synthetic_retail__order_items.sql"]
        self.assertTrue(items.endswith("where cast(`line_id` as int64) is not null\n"))
        self.assertIn("    cast(`sku` as int64) as product_id,\n", items)
        self.assertIn("cast(`product_group` as bool) as category", files["silver_synthetic_retail__products.sql"])

    def test_mapped_in_place_bronze_names(self) -> None:
        plan = load_plan()
        for item in plan["tables"]:
            for col in item["columns"]:
                col["bronze_column"] = col["canonical_name"]
        text = render_bigquery_silver(plan)["silver_synthetic_retail__customers.sql"]
        self.assertIn("cast(`customer_id` as string) as customer_id", text)


@unittest.skipIf(yaml is None, "PyYAML is unavailable")
class YamlContractTests(unittest.TestCase):
    def parsed(self, plan: dict) -> dict:
        return yaml.safe_load(render_bigquery_silver(plan)["_silver_synthetic_retail__models.yml"])

    def test_source_block(self) -> None:
        doc = self.parsed(load_plan())
        self.assertEqual(doc["version"], 2)
        (source,) = doc["sources"]
        self.assertEqual(source["name"], "migration_bronze_synthetic_retail")
        self.assertEqual(
            source["schema"], "{{ env_var('MIGRATION_BRONZE_NAMESPACE', 'starterkit_migration_bronze') }}"
        )
        self.assertNotIn("database", source)
        self.assertEqual(source["config"]["enabled"], f"{{{{ {GATE} }}}}")
        self.assertEqual(
            [(t["name"], t["identifier"]) for t in source["tables"]],
            [(t, t) for t in ("customers", "order_items", "orders", "products")],
        )

    def test_namespace_uses_edition_mechanism_not_literal(self) -> None:
        # docs/scaffolder-input-contract.md: the Bronze namespace is referenced through the
        # edition's MIGRATION_BRONZE_NAMESPACE mechanism, never written as a bare literal.
        plan = load_plan()
        plan["bronze_namespace"] = "starterkit_migration_other"
        text = render_bigquery_silver(plan)["_silver_synthetic_retail__models.yml"]
        (source,) = yaml.safe_load(text)["sources"]
        self.assertEqual(
            source["schema"], "{{ env_var('MIGRATION_BRONZE_NAMESPACE', 'starterkit_migration_other') }}"
        )
        self.assertNotRegex(text, r"(?m)^\s*schema:\s*\"?starterkit_migration_")
        self.assertEqual(text.count("starterkit_migration_other"), 1)

    def test_yaml_keyword_bronze_names_stay_strings(self) -> None:
        # Legal identifiers that plain YAML would read as null or booleans.
        for name in ("null", "on", "no", "true", "off", "yes"):
            with self.subTest(name=name):
                plan = load_plan()
                table(plan, "customers")["bronze_table"] = name
                doc = self.parsed(plan)
                identifiers = {t["name"]: t["identifier"] for t in doc["sources"][0]["tables"]}
                self.assertEqual(identifiers["customers"], name)

    def test_data_tests_use_dbt_110_syntax(self) -> None:
        doc = self.parsed(load_plan())
        models = {m["name"]: {c["name"]: c.get("data_tests", []) for c in m["columns"]} for m in doc["models"]}
        self.assertEqual(models["silver_synthetic_retail__customers"]["customer_id"], ["not_null", "unique"])
        self.assertEqual(
            models["silver_synthetic_retail__order_items"]["product_id"],
            [{"relationships": {"arguments": {"to": "ref('silver_synthetic_retail__products')", "field": "product_id"}}}],
        )
        self.assertEqual(
            models["silver_synthetic_retail__orders"]["customer_id"],
            [{"relationships": {"arguments": {"to": "ref('silver_synthetic_retail__customers')", "field": "customer_id"}}}],
        )
        rendered = render_bigquery_silver(load_plan())["_silver_synthetic_retail__models.yml"]
        self.assertNotIn("tests:\n", rendered.replace("data_tests:\n", ""))

    def test_relationships_only_when_parent_planned(self) -> None:
        plan = load_plan()
        plan["tables"] = [t for t in plan["tables"] if t["entity"] in {"order_items", "orders"}]
        doc = self.parsed(plan)
        models = {m["name"]: {c["name"]: c.get("data_tests", []) for c in m["columns"]} for m in doc["models"]}
        self.assertEqual(sorted(models), ["silver_synthetic_retail__order_items", "silver_synthetic_retail__orders"])
        self.assertEqual(models["silver_synthetic_retail__order_items"]["product_id"], [])
        self.assertEqual(len(models["silver_synthetic_retail__order_items"]["order_id"]), 1)
        self.assertEqual(models["silver_synthetic_retail__orders"]["customer_id"], [])

    def test_non_nullable_adds_not_null(self) -> None:
        plan = load_plan()
        column(plan, "orders", "customer_id")["nullable"] = False
        column(plan, "orders", "order_status")["nullable"] = False
        doc = self.parsed(plan)
        orders = {c["name"]: c.get("data_tests", []) for c in doc["models"][2]["columns"]}
        self.assertEqual(orders["order_status"], ["not_null"])
        self.assertEqual(orders["customer_id"][0], "not_null")


class RefusalTests(unittest.TestCase):
    def assertRefused(self, plan, reason: str) -> None:
        with self.assertRaises(TemplatePlanError) as caught:
            render_bigquery_silver(plan)
        self.assertEqual(caught.exception.reason, reason)
        self.assertNotIn("canary", str(caught.exception))

    def test_refusals(self) -> None:
        cases = []

        def case(reason, mutate):
            plan = load_plan()
            mutate(plan)
            cases.append((reason, plan))

        case("edition_mismatch", lambda p: p.update(edition="snowflake"))
        case("invalid_source_id", lambda p: p.update(source_id="Canary"))
        case("invalid_source_id", lambda p: p.update(source_id="canary/../x"))
        case("invalid_source_id", lambda p: p.update(source_id="a" * 41))
        case("invalid_bronze_namespace", lambda p: p.update(bronze_namespace="canary_dataset"))
        case("invalid_bronze_namespace", lambda p: p.update(bronze_namespace="starterkit_migration_canary`; drop"))
        case("plan_invalid", lambda p: p.update(tables=[]))
        case("plan_invalid", lambda p: p.pop("bronze_namespace"))
        case("non_canonical_table", lambda p: table(p, "orders").update(entity="canary_users"))
        case("duplicate_table", lambda p: p["tables"].append(copy.deepcopy(table(p, "orders"))))
        case("unsafe_identifier", lambda p: table(p, "orders").update(bronze_table="canary-orders"))
        case("unsafe_identifier", lambda p: column(p, "orders", "order_status").update(bronze_column="canary` as x"))
        case("unsafe_identifier", lambda p: column(p, "orders", "order_status").update(bronze_column=""))
        case("non_canonical_target", lambda p: column(p, "customers", "country").update(canonical_name="canary_email"))
        case("duplicate_column", lambda p: table(p, "products")["columns"].append(copy.deepcopy(column(p, "products", "category"))))
        case("missing_contract_column", lambda p: table(p, "products")["columns"].pop())
        case("missing_primary_key", lambda p: table(p, "products")["columns"].pop(0))
        case("unsupported_cast", lambda p: column(p, "orders", "order_date").update(cast="canary_float"))
        case("plan_invalid", lambda p: column(p, "orders", "order_date").update(nullable="canary"))
        case("nullable_primary_key", lambda p: column(p, "orders", "order_id").update(nullable=True))

        for reason, plan in cases:
            with self.subTest(reason=reason):
                self.assertRefused(plan, reason)


if __name__ == "__main__":
    unittest.main()
