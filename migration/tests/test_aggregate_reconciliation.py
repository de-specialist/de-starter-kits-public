"""Offline tests for allowlisted aggregate reconciliation across Source/Bronze/Silver/Gold.

All fixtures are synthetic: a SQLite source and DuckDB Bronze/Silver/Gold files in a
temporary directory. Table names, column names, keys, dates, amounts, raw statuses, and
paths are unique sentinels so the tests can prove none of them reach any output.
"""

from __future__ import annotations

import contextlib
import copy
import importlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from collections import defaultdict
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "migration"))
agg = importlib.import_module("aggregate_reconciliation")


def _load_duckdb():
    try:
        return importlib.import_module("duckdb")
    except ModuleNotFoundError as error:
        if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name != "duckdb":
            raise
        return None


duckdb = _load_duckdb()

RUN_ID = "123e4567-e89b-42d3-a456-426614174000"

SRC_T, BRZ_T, SLV_T = "zqsrc_orders_t81", "zqbrz_orders_t82", "zqslv_orders_t83"
GLD_CUST_T, GLD_DAY_T = "zqgld_custval_t84", "zqgld_daily_t85"
C_ORD, C_CUST, C_STAT, C_AMT, C_DAY = "zqc_ord_k1", "zqc_cust_k2", "zqc_stat_k3", "zqc_amt_k4", "zqc_day_k5"
C_CNT, C_REV = "zqc_ordcnt_k6", "zqc_rev_k7"
PATH_SENTINEL = "zqpath_9931"
ZERO_CUSTOMER = "ZQCUS-5505"

ROWS = [
    ("ZQORD-7101", "ZQCUS-5501", "ZqComplete", "481.37", "2031-07-19"),
    ("ZQORD-7102", "ZQCUS-5501", "ZqShipped", "263.19", "2031-07-19"),
    ("ZQORD-7103", "ZQCUS-5502", "ZqComplete", "157.83", "2031-07-19"),
    ("ZQORD-7104", "ZQCUS-5502", "ZqReturned", "392.61", "2031-07-20"),
    ("ZQORD-7105", "ZQCUS-5502", "ZqComplete", "74.29", "2031-07-20"),
    ("ZQORD-7106", "ZQCUS-5503", "ZqShipped", "918.53", "2031-07-20"),
    ("ZQORD-7107", "ZQCUS-5503", "ZqComplete", "205.47", "2031-07-20"),
    ("ZQORD-7108", "ZQCUS-5503", "ZqComplete", "336.71", "2031-07-21"),
    ("ZQORD-7109", "ZQCUS-5504", "ZqComplete", "129.97", "2031-07-21"),
    ("ZQORD-7110", "ZQCUS-5504", "ZqShipped", "647.13", "2031-07-21"),
    ("ZQORD-7111", "ZQCUS-5504", "ZqComplete", "58.39", "2031-07-21"),
    ("ZQORD-7112", "ZQCUS-5501", "ZqComplete", "710.83", "2031-07-21"),
]
RAW_STATUS_MAP = {"complete": ["ZqComplete"], "shipped": ["ZqShipped"], "returned": ["ZqReturned"]}
INCLUDED_RAW = {"ZqComplete", "ZqShipped"}
SENTINELS = (
    [SRC_T, BRZ_T, SLV_T, GLD_CUST_T, GLD_DAY_T, C_ORD, C_CUST, C_STAT, C_AMT, C_DAY, C_CNT, C_REV,
     PATH_SENTINEL, ZERO_CUSTOMER, "ZQORD", "ZQCUS", "ZqComplete", "ZqShipped", "ZqReturned", "2031-07"]
    + [row[3] for row in ROWS]
)


def _insert_many(connection, table: str, row_sql: str, rows: list) -> None:
    """One multi-row parameterized INSERT; DuckDB executemany is slow for test setup."""
    connection.execute(f"insert into {table} values " + ", ".join([row_sql] * len(rows)),
                       [value for row in rows for value in row])


def _status(raw_layer: bool, column: str = C_STAT) -> dict:
    return {"column": column, "values": copy.deepcopy(RAW_STATUS_MAP)} if raw_layer else {"column": column}


def make_contract(**overrides) -> dict:
    status_rule = {"name": "exclude_returned", "include": ["complete", "shipped"]}
    order_count = {
        "name": "order_count", "aggregation": "count_rows", "unit": "count", "scale": 0,
        "grain": ["customer"], "null_measure": "ignore", "null_grain": "exclude",
        "zero_value_groups": overrides.get("zero_value_groups", "equivalent_to_absent"),
        "status_rule": status_rule,
        "layers": {
            "source": {"table": SRC_T, "grain_columns": [C_CUST], "status": _status(True)},
            "bronze": {"table": BRZ_T, "grain_columns": [C_CUST], "status": _status(True)},
            "silver": {"table": SLV_T, "grain_columns": [C_CUST], "status": _status(False)},
            "gold": {"table": GLD_CUST_T, "grain_columns": [C_CUST], "measure": C_CNT,
                     "aggregation": "precounted", "status": {"prefiltered": True}},
        },
    }
    revenue = {
        "name": "daily_revenue", "aggregation": "sum", "unit": "currency_major", "currency": "AUD",
        "scale": 2, "grain": ["order_date"],
        "null_measure": overrides.get("null_measure", "ignore"),
        "null_grain": overrides.get("null_grain", "exclude"),
        "zero_value_groups": "significant", "status_rule": copy.deepcopy(status_rule),
        "layers": {
            "source": {"table": SRC_T, "grain_columns": [C_DAY], "measure": C_AMT, "status": _status(True)},
            "bronze": {"table": BRZ_T, "grain_columns": [C_DAY], "measure": C_AMT, "status": _status(True)},
            "silver": {"table": SLV_T, "grain_columns": [C_DAY], "measure": C_AMT, "status": _status(False)},
            "gold": {"table": GLD_DAY_T, "grain_columns": [C_DAY], "measure": C_REV, "status": {"prefiltered": True}},
        },
    }
    distinct = {
        "name": "distinct_customers", "aggregation": "count_distinct", "unit": "count", "scale": 0,
        "grain": [], "null_measure": "ignore", "null_grain": "exclude",
        "zero_value_groups": "significant", "status_rule": None,
        "layers": {
            "source": {"table": SRC_T, "grain_columns": [], "measure": C_CUST},
            "silver": {"table": SLV_T, "grain_columns": [], "measure": C_CUST},
        },
    }
    contract = {"contract_version": 1, "metrics": [order_count, revenue, distinct]}
    if "min_rows_for_total" in overrides:
        contract["min_rows_for_total"] = overrides["min_rows_for_total"]
    return contract


def metric(report: dict, name: str) -> dict:
    return next(item for item in report["metrics"] if item["name"] == name)


def layer(report: dict, name: str, layer_name: str) -> dict:
    return next(item for item in metric(report, name)["layers"] if item["layer"] == layer_name)


def comparison(report: dict, name: str, left: str, right: str) -> dict:
    return next(item for item in metric(report, name)["comparisons"]
                if (item["left"], item["right"]) == (left, right))


@unittest.skipIf(duckdb is None, "duckdb is required for offline aggregate reconciliation tests")
class AggregateReconciliationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / PATH_SENTINEL
        self.root.mkdir()
        self.paths = {
            "source": self.root / "zqsource_db_x1.sqlite",
            "bronze": self.root / "zqbronze_db_x2.duckdb",
            "silver": self.root / "zqsilver_db_x3.duckdb",
            "gold": self.root / "zqgold_db_x4.duckdb",
        }
        self._build()
        self.connections = {
            "source": sqlite3.connect(self.paths["source"]),
            "bronze": duckdb.connect(str(self.paths["bronze"])),
            "silver": duckdb.connect(str(self.paths["silver"])),
            "gold": duckdb.connect(str(self.paths["gold"])),
        }
        for connection in self.connections.values():
            self.addCleanup(connection.close)

    def _build(self):
        source = sqlite3.connect(self.paths["source"])
        source.execute(f"create table {SRC_T}({C_ORD} text, {C_CUST} text, {C_STAT} text, {C_AMT} text, {C_DAY} text)")
        source.executemany(f"insert into {SRC_T} values (?, ?, ?, ?, ?)", ROWS)
        source.commit()
        source.close()
        bronze = duckdb.connect(str(self.paths["bronze"]))
        bronze.execute(f"create table {BRZ_T}({C_ORD} varchar, {C_CUST} varchar, {C_STAT} varchar, "
                       f"{C_AMT} varchar, {C_DAY} varchar)")
        _insert_many(bronze, BRZ_T, "(?, ?, ?, ?, ?)", ROWS)
        bronze.close()
        silver = duckdb.connect(str(self.paths["silver"]))
        silver.execute(f"create table {SLV_T}({C_ORD} varchar, {C_CUST} varchar, {C_STAT} varchar, "
                       f"{C_AMT} decimal(18, 2), {C_DAY} date)")
        _insert_many(
            silver, SLV_T, "(?, ?, ?, cast(? as decimal(18, 2)), cast(? as date))",
            [[order, customer, status[2:].lower(), amount, day] for order, customer, status, amount, day in ROWS],
        )
        silver.close()
        counts: dict[str, int] = defaultdict(int)
        revenue: dict[str, int] = defaultdict(int)
        daily_count: dict[str, int] = defaultdict(int)
        daily_revenue: dict[str, Decimal] = defaultdict(Decimal)
        for _order, customer, status, amount, day in ROWS:
            if status in INCLUDED_RAW:
                counts[customer] += 1
                revenue[customer] += Decimal(amount)
                daily_count[day] += 1
                daily_revenue[day] += Decimal(amount)
        gold = duckdb.connect(str(self.paths["gold"]))
        gold.execute(f"create table {GLD_CUST_T}({C_CUST} varchar, {C_CNT} bigint, {C_REV} decimal(18, 2))")
        _insert_many(gold, GLD_CUST_T, "(?, ?, cast(? as decimal(18, 2)))",
                     [[customer, counts[customer], str(revenue[customer])] for customer in sorted(counts)]
                     + [[ZERO_CUSTOMER, 0, "0.00"]])
        gold.execute(f"create table {GLD_DAY_T}({C_DAY} date, {C_CNT} bigint, {C_REV} decimal(18, 2))")
        _insert_many(gold, GLD_DAY_T, "(cast(? as date), ?, cast(? as decimal(18, 2)))",
                     [[day, daily_count[day], str(daily_revenue[day])] for day in sorted(daily_count)])
        gold.close()

    def run_report(self, contract=None, connections=None, **kwargs) -> dict:
        report = agg.reconcile(contract or make_contract(), self.connections if connections is None else connections,
                               run_id=RUN_ID, **kwargs)
        agg.validate_report(report, expected_run_id=RUN_ID)
        self.assert_private(json.dumps(report))
        return report

    def assert_private(self, text: str):
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)

    # ------------------------------------------------------------------ outcomes

    def test_all_layers_match_and_report_is_deterministic(self):
        first = self.run_report()
        second = self.run_report()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(first["status"], "matched")
        self.assertIs(first["cutover_ready"], False)
        self.assertEqual(first["evidence_scope"], "local_only")
        self.assertRegex(first["contract_fingerprint"], r"^[0-9a-f]{64}$")
        for item in first["metrics"]:
            self.assertEqual(item["status"], "matched", item["name"])
        self.assertEqual(
            [(c["left"], c["right"]) for c in metric(first, "order_count")["comparisons"]],
            [("source", "bronze"), ("bronze", "silver"), ("silver", "gold"), ("source", "gold")],
        )
        source = layer(first, "order_count", "source")
        self.assertEqual(source["rows"], {"scanned": 12, "kept": 11, "excluded_status": 1,
                                          "null_measure": 0, "null_grain": 0})
        self.assertEqual(source["filter_mode"], "column")
        self.assertEqual(layer(first, "order_count", "gold")["filter_mode"], "prefiltered")
        self.assertEqual(layer(first, "order_count", "gold")["aggregation_mode"], "precounted")
        self.assertEqual(source["total"], "11")
        expected_revenue = sum(Decimal(row[3]) for row in ROWS if row[2] in INCLUDED_RAW)
        for name in ("source", "bronze", "silver"):
            self.assertEqual(layer(first, "daily_revenue", name)["total"], f"{expected_revenue:.2f}")
        self.assertEqual(layer(first, "daily_revenue", "gold")["total_status"], "suppressed_min_rows")
        self.assertEqual(comparison(first, "daily_revenue", "source", "gold")["groups_matched"], 3)
        self.assertEqual(layer(first, "distinct_customers", "source")["total"], "4")
        definition = metric(first, "daily_revenue")["definition"]
        self.assertEqual(definition["currency"], "AUD")
        self.assertEqual(definition["status_rule"], {"name": "exclude_returned", "include": ["complete", "shipped"]})

    def test_fingerprint_changes_with_physical_mapping(self):
        changed = make_contract()
        changed["metrics"][0]["layers"]["bronze"]["table"] = "zqother_table"
        connections = dict(self.connections)
        self.assertNotEqual(agg.reconcile(make_contract(), connections)["contract_fingerprint"],
                            agg.reconcile(changed, connections)["contract_fingerprint"])

    def test_bronze_duplicates_are_value_mismatches(self):
        self.connections["bronze"].execute(f"insert into {BRZ_T} select * from {BRZ_T} where {C_ORD} = 'ZQORD-7101'")
        report = self.run_report()
        self.assertEqual(report["status"], "mismatched")
        source_bronze = comparison(report, "order_count", "source", "bronze")
        self.assertEqual((source_bronze["status"], source_bronze["groups_value_mismatch"],
                          source_bronze["groups_matched"]), ("mismatched", 1, 3))
        self.assertEqual(comparison(report, "order_count", "silver", "gold")["status"], "matched")
        self.assertEqual(comparison(report, "daily_revenue", "bronze", "silver")["groups_value_mismatch"], 1)

    def test_missing_and_extra_groups(self):
        silver = self.connections["silver"]
        silver.execute(f"delete from {SLV_T} where {C_CUST} = 'ZQCUS-5504'")
        silver.execute(f"insert into {SLV_T} values ('ZQORD-7199', 'ZQCUS-5599', 'complete', 12.34, date '2031-07-22')")
        report = self.run_report()
        bronze_silver = comparison(report, "order_count", "bronze", "silver")
        self.assertEqual((bronze_silver["groups_missing_right"], bronze_silver["groups_extra_right"],
                          bronze_silver["groups_matched"]), (1, 1, 3))
        silver_gold = comparison(report, "order_count", "silver", "gold")
        self.assertEqual((silver_gold["groups_missing_right"], silver_gold["groups_extra_right"]), (1, 1))
        daily = comparison(report, "daily_revenue", "bronze", "silver")
        self.assertEqual((daily["groups_extra_right"], daily["groups_value_mismatch"]), (1, 1))
        self.assertEqual(metric(report, "order_count")["status"], "mismatched")

    def test_returned_status_drift(self):
        # Raw casing drift in Bronze is handled by the per-layer status mapping.
        bronze = self.connections["bronze"]
        bronze.execute(f"update {BRZ_T} set {C_STAT} = 'ZQCOMPLETE_V2' where {C_STAT} = 'ZqComplete'")
        contract = make_contract()
        for item in contract["metrics"][:2]:
            item["layers"]["bronze"]["status"]["values"]["complete"] = ["ZQCOMPLETE_V2"]
        self.assertEqual(self.run_report(contract)["status"], "matched")
        # A returned order normalized to complete in Silver is real drift.
        self.connections["silver"].execute(f"update {SLV_T} set {C_STAT} = 'complete' where {C_STAT} = 'returned'")
        report = self.run_report(contract)
        drift = comparison(report, "order_count", "bronze", "silver")
        self.assertEqual((drift["status"], drift["groups_value_mismatch"]), ("mismatched", 1))
        self.assertEqual(layer(report, "order_count", "silver")["rows"]["excluded_status"], 0)
        # An unmapped raw status is excluded, never silently included.
        bronze.execute(f"update {BRZ_T} set {C_STAT} = 'ZqUnknownState' where {C_ORD} = 'ZQORD-7110'")
        report = self.run_report(contract)
        self.assertEqual(layer(report, "order_count", "bronze")["rows"]["excluded_status"], 2)
        self.assertNotIn("ZqUnknownState", json.dumps(report))

    def test_zero_order_customer_rules(self):
        equivalent = self.run_report(make_contract(zero_value_groups="equivalent_to_absent"))
        silver_gold = comparison(equivalent, "order_count", "silver", "gold")
        self.assertEqual((silver_gold["status"], silver_gold["groups_extra_right"]), ("matched", 0))
        self.assertEqual(layer(equivalent, "order_count", "gold")["groups"], 5)
        significant = self.run_report(make_contract(zero_value_groups="significant"))
        silver_gold = comparison(significant, "order_count", "silver", "gold")
        self.assertEqual((silver_gold["status"], silver_gold["groups_extra_right"]), ("mismatched", 1))
        self.assertEqual(metric(significant, "order_count")["status"], "mismatched")

    def test_null_measure_policies(self):
        self.connections["silver"].execute(f"update {SLV_T} set {C_AMT} = null where {C_ORD} = 'ZQORD-7103'")
        ignored = self.run_report(make_contract(null_measure="ignore"))
        silver = layer(ignored, "daily_revenue", "silver")
        self.assertEqual((silver["status"], silver["rows"]["null_measure"], silver["rows"]["kept"]), ("available", 1, 10))
        self.assertEqual(comparison(ignored, "daily_revenue", "bronze", "silver")["groups_value_mismatch"], 1)
        failed = self.run_report(make_contract(null_measure="fail"))
        silver = layer(failed, "daily_revenue", "silver")
        self.assertEqual((silver["status"], silver["reason"], silver["groups"], silver["total"]),
                         ("null_policy_violation", "null_measure_present", None, None))
        self.assertEqual(comparison(failed, "daily_revenue", "bronze", "silver")["status"], "null_policy_violation")
        self.assertEqual(comparison(failed, "daily_revenue", "source", "bronze")["status"], "matched")
        self.assertEqual(metric(failed, "daily_revenue")["status"], "null_policy_violation")

    def test_null_grain_policies(self):
        self.connections["bronze"].execute(f"update {BRZ_T} set {C_DAY} = null where {C_ORD} = 'ZQORD-7101'")
        excluded = self.run_report(make_contract(null_grain="exclude"))
        bronze = layer(excluded, "daily_revenue", "bronze")
        self.assertEqual((bronze["rows"]["null_grain"], bronze["groups"]), (1, 3))
        self.assertEqual(comparison(excluded, "daily_revenue", "source", "bronze")["groups_value_mismatch"], 1)
        grouped = self.run_report(make_contract(null_grain="group"))
        bronze = layer(grouped, "daily_revenue", "bronze")
        self.assertEqual((bronze["rows"]["null_grain"], bronze["groups"]), (0, 4))
        self.assertEqual(comparison(grouped, "daily_revenue", "source", "bronze")["groups_extra_right"], 1)
        failed = self.run_report(make_contract(null_grain="fail"))
        bronze = layer(failed, "daily_revenue", "bronze")
        self.assertEqual((bronze["status"], bronze["reason"]), ("null_policy_violation", "null_grain_present"))
        self.assertEqual(metric(failed, "daily_revenue")["status"], "null_policy_violation")

    def test_status_precedence(self):
        self.connections["bronze"].execute(f"update {BRZ_T} set {C_DAY} = null where {C_ORD} = 'ZQORD-7101'")
        self.connections["silver"].execute(f"delete from {SLV_T} where {C_ORD} = 'ZQORD-7112'")
        report = self.run_report(make_contract(null_grain="fail"))
        statuses = {c["status"] for c in metric(report, "daily_revenue")["comparisons"]}
        self.assertIn("null_policy_violation", statuses)
        self.assertIn("mismatched", statuses)
        self.assertEqual(metric(report, "daily_revenue")["status"], "mismatched")
        connections = dict(self.connections)
        del connections["gold"]
        report = self.run_report(make_contract(null_grain="fail"), connections)
        self.assertEqual(comparison(report, "daily_revenue", "silver", "gold")["status"], "unavailable")
        self.assertEqual(comparison(report, "daily_revenue", "bronze", "silver")["status"], "null_policy_violation")
        self.assertEqual(agg._worst(["unavailable", "null_policy_violation", "matched"]), "null_policy_violation")
        self.assertEqual(agg._worst(["unavailable", "matched"]), "unavailable")

    def test_missing_connections_tables_and_columns(self):
        connections = dict(self.connections)
        del connections["gold"]
        self.connections["bronze"].execute(f"drop table {BRZ_T}")
        self.connections["source"].execute(f"alter table {SRC_T} rename column {C_DAY} to zqc_renamed_k9")
        report = self.run_report(connections=connections)
        self.assertEqual(layer(report, "order_count", "gold")["reason"], "connection_unavailable")
        self.assertEqual(layer(report, "order_count", "bronze")["reason"], "table_unavailable")
        self.assertEqual(layer(report, "daily_revenue", "source")["reason"], "column_unavailable")
        self.assertEqual(layer(report, "order_count", "source")["status"], "available")
        for name in ("order_count", "daily_revenue"):
            entry = layer(report, name, "gold")
            self.assertEqual((entry["status"], entry["rows"], entry["groups"], entry["total"]),
                             ("unavailable", None, None, None))
        self.assertEqual(comparison(report, "order_count", "source", "bronze")["groups_matched"], None)
        self.assertEqual(metric(report, "order_count")["status"], "unavailable")
        self.assertNotIn("zqc_renamed_k9", json.dumps(report))

    def test_query_errors_are_reason_codes_only(self):
        class Broken:
            def execute(self, _sql):
                raise RuntimeError(f"secret failure in {SRC_T} at {PATH_SENTINEL}")

        connections = dict(self.connections, source=Broken())
        report = self.run_report(connections=connections)
        self.assertEqual(layer(report, "order_count", "source")["reason"], "table_unavailable")

    def test_empty_layers(self):
        self.connections["bronze"].execute(f"delete from {BRZ_T}")
        report = self.run_report()
        bronze = layer(report, "order_count", "bronze")
        self.assertEqual((bronze["status"], bronze["groups"], bronze["rows"]["scanned"], bronze["total_status"]),
                         ("available", 0, 0, "suppressed_min_rows"))
        self.assertEqual(comparison(report, "order_count", "source", "bronze")["groups_missing_right"], 4)
        self.assertEqual(comparison(report, "order_count", "bronze", "silver")["groups_extra_right"], 4)
        self.connections["silver"].execute(f"delete from {SLV_T}")
        self.connections["source"].execute(f"delete from {SRC_T}")
        report = self.run_report()
        distinct = comparison(report, "distinct_customers", "source", "silver")
        self.assertEqual((distinct["status"], distinct["groups_matched"]), ("matched", 1))

    def test_sqlite_text_amounts(self):
        source = self.connections["source"]
        source.execute(f"update {SRC_T} set {C_AMT} = 157.83 where {C_ORD} = 'ZQORD-7103'")  # stored as REAL
        source.execute(f"update {SRC_T} set {C_AMT} = '74.290' where {C_ORD} = 'ZQORD-7105'")
        self.assertEqual(self.run_report()["status"], "matched")
        source.execute(f"update {SRC_T} set {C_AMT} = 'zq-not-a-number' where {C_ORD} = 'ZQORD-7101'")
        report = self.run_report()
        self.assertEqual(layer(report, "daily_revenue", "source")["reason"], "measure_not_numeric")
        self.assertNotIn("zq-not-a-number", json.dumps(report))
        source.execute(f"update {SRC_T} set {C_AMT} = '481.375' where {C_ORD} = 'ZQORD-7101'")
        self.assertEqual(layer(self.run_report(), "daily_revenue", "source")["reason"], "measure_scale_exceeded")
        source.execute(f"update {SRC_T} set {C_AMT} = ' 481.37' where {C_ORD} = 'ZQORD-7101'")
        self.assertEqual(layer(self.run_report(), "daily_revenue", "source")["reason"], "measure_not_numeric")

    def test_decimal_values_never_use_binary_floats(self):
        self.assertEqual(agg.format_decimal(Decimal("0.1") + Decimal("0.2"), 2), "0.30")
        self.assertEqual(agg.format_decimal(Decimal("-0.00"), 2), "0.00")
        self.assertEqual(agg.format_decimal(Decimal("-12.5"), 2), "-12.50")
        self.assertEqual(agg._to_decimal(0.1), Decimal("0.1"))
        self.assertIsNone(agg._to_decimal(float("nan")))
        self.assertIsNone(agg._to_decimal(True))
        self.assertIsNone(agg._to_decimal("1e3"))

    def test_distinct_counts_are_not_summable(self):
        gold = self.connections["gold"]
        gold.execute("create table zqgld_distinct_t87(zqc_one_k8 integer, zqc_dc_k9 bigint)")
        gold.execute("insert into zqgld_distinct_t87 values (1, 4)")
        contract = make_contract()
        contract["metrics"][2]["layers"]["gold"] = {
            "table": "zqgld_distinct_t87", "grain_columns": [], "measure": "zqc_dc_k9", "aggregation": "precounted",
        }
        self.assertEqual(metric(self.run_report(contract), "distinct_customers")["status"], "matched")
        gold.execute("insert into zqgld_distinct_t87 values (2, 0)")
        report = self.run_report(contract)
        self.assertEqual(layer(report, "distinct_customers", "gold")["reason"], "distinct_count_not_summable")
        self.assertEqual(metric(report, "distinct_customers")["status"], "unavailable")
        # Distinct counts per group never produce a summed total.
        grouped = make_contract()
        for spec in grouped["metrics"][2]["layers"].values():
            spec["grain_columns"] = [C_DAY]
        grouped["metrics"][2]["grain"] = ["order_date"]
        report = self.run_report(grouped)
        source = layer(report, "distinct_customers", "source")
        self.assertEqual((source["total_status"], source["total"], source["groups"]), ("not_summable", None, 3))
        # Precounted counts must be nonnegative integers.
        gold.execute(f"update {GLD_CUST_T} set {C_CNT} = -1 where {C_CUST} = '{ZERO_CUSTOMER}'")
        self.assertEqual(layer(self.run_report(), "order_count", "gold")["reason"], "precounted_value_invalid")

    def test_totals_are_suppressed_below_minimum_rows(self):
        report = self.run_report(make_contract(min_rows_for_total=100))
        for item in report["metrics"]:
            for entry in item["layers"]:
                self.assertEqual((entry["total"], entry["total_status"]), (None, "suppressed_min_rows"))
        self.assertEqual(report["min_rows_for_total"], 100)
        default = self.run_report()
        self.assertEqual(default["min_rows_for_total"], 10)
        self.assertEqual(layer(default, "order_count", "gold")["total_status"], "suppressed_min_rows")
        self.assertIsNone(layer(default, "order_count", "gold")["total"])

    def assert_not_derivable(self, report: dict, sentinel: Decimal):
        """No emitted total, nor the difference of any two emitted totals, equals the sentinel."""
        self.assertNotIn(str(sentinel), json.dumps(report))
        for item in report["metrics"]:
            totals = [Decimal(entry["total"]) for entry in item["layers"] if entry["total"] is not None]
            for position, total in enumerate(totals):
                self.assertNotEqual(total, sentinel, item["name"])
                for other in totals[position + 1:]:
                    self.assertNotEqual(abs(total - other), sentinel, item["name"])

    def test_one_extra_silver_row_cannot_be_derived_from_totals(self):
        sentinel = Decimal("8765.43")
        for day in ("2031-07-19", "2031-07-23"):  # existing group, then a new group
            with self.subTest(day=day):
                silver = self.connections["silver"]
                silver.execute(f"delete from {SLV_T} where {C_ORD} = 'ZQORD-7198'")
                silver.execute(f"insert into {SLV_T} values ('ZQORD-7198', 'ZQCUS-5501', 'complete', "
                               f"{sentinel}, date '{day}')")
                report = self.run_report()
                self.assertEqual(report["status"], "mismatched")
                self.assertIs(report["cutover_ready"], False)
                for name in ("order_count", "daily_revenue"):
                    entry = layer(report, name, "silver")
                    self.assertEqual((entry["rows"]["kept"], entry["total_status"], entry["total"]),
                                     (12, "suppressed_small_difference", None))
                # Source and Bronze still match each other, so their equal totals are emitted.
                self.assertEqual(layer(report, "daily_revenue", "source")["total_status"], "emitted")
                self.assertEqual(layer(report, "daily_revenue", "source")["total"],
                                 layer(report, "daily_revenue", "bronze")["total"])
                self.assert_not_derivable(report, sentinel)

    def test_single_changed_group_cannot_be_derived_from_totals(self):
        sentinel = Decimal("5432.10")
        self.connections["silver"].execute(
            f"update {SLV_T} set {C_AMT} = {C_AMT} + {sentinel} where {C_ORD} = 'ZQORD-7103'")
        report = self.run_report()
        daily = comparison(report, "daily_revenue", "bronze", "silver")
        self.assertEqual((daily["status"], daily["groups_value_mismatch"]), ("mismatched", 1))
        silver = layer(report, "daily_revenue", "silver")
        self.assertEqual(silver["rows"]["kept"], layer(report, "daily_revenue", "bronze")["rows"]["kept"])
        self.assertEqual((silver["total_status"], silver["total"]), ("suppressed_small_difference", None))
        self.assert_not_derivable(report, sentinel)
        # Non-adjacent layers are checked too: with Bronze unavailable, Source and Silver are never
        # compared directly, yet their totals must not both be emitted. The downstream layer is suppressed.
        connections = dict(self.connections)
        del connections["bronze"]
        report = self.run_report(connections=connections)
        self.assertEqual([c["left"] + "-" + c["right"] for c in metric(report, "daily_revenue")["comparisons"]
                          if c["status"] != "unavailable"], ["silver-gold", "source-gold"])
        self.assertEqual(layer(report, "daily_revenue", "source")["total_status"], "emitted")
        self.assertEqual(layer(report, "daily_revenue", "silver")["total_status"], "suppressed_small_difference")
        self.assert_not_derivable(report, sentinel)
        # A threshold of one disables the small-difference rule, which is how the leak would look.
        exposed = self.run_report(make_contract(min_rows_for_total=1))
        totals = [Decimal(layer(exposed, "daily_revenue", name)["total"]) for name in ("bronze", "silver")]
        self.assertEqual(totals[1] - totals[0], sentinel)

    def test_duckdb_layer_files_cannot_read_external_files(self):
        secret = self.root / "zqexternal_secret_e1.csv"
        secret.write_text(f"{C_CUST},{C_STAT}\nZQCUS-5501,complete\n", encoding="utf-8")
        path = self.root / "zqview_db_v1.duckdb"
        writer = duckdb.connect(str(path))
        writer.execute(f"create view {SLV_T} as select * from read_csv('{secret}')")
        writer.close()
        connection = agg._open_local(path)
        self.assertIsNotNone(connection)
        self.addCleanup(connection.close)
        with self.assertRaises(Exception):
            connection.execute("set enable_external_access = true")
        with self.assertRaises(Exception):
            connection.execute("install httpfs")
        contract = make_contract()
        contract["metrics"] = contract["metrics"][:1]
        report = self.run_report(contract, connections=dict(self.connections, silver=connection))
        silver = layer(report, "order_count", "silver")
        self.assertEqual((silver["status"], silver["reason"], silver["rows"], silver["total"]),
                         ("unavailable", "table_unavailable", None, None))
        self.assertNotIn("zqexternal_secret_e1", json.dumps(report))

    def test_null_policy_violation_is_not_masked_by_later_errors(self):
        source = self.connections["source"]
        for null_order, bad_order in (("ZQORD-7101", "ZQORD-7112"), ("ZQORD-7112", "ZQORD-7101")):
            with self.subTest(null_first=null_order == "ZQORD-7101"):
                source.execute(f"update {SRC_T} set {C_AMT} = '1.00'")
                source.execute(f"update {SRC_T} set {C_AMT} = null where {C_ORD} = '{null_order}'")
                source.execute(f"update {SRC_T} set {C_AMT} = 'zq-not-a-number' where {C_ORD} = '{bad_order}'")
                report = self.run_report(make_contract(null_measure="fail"))
                entry = layer(report, "daily_revenue", "source")
                self.assertEqual((entry["status"], entry["reason"]), ("null_policy_violation", "null_measure_present"))
                ignored = layer(self.run_report(make_contract(null_measure="ignore")), "daily_revenue", "source")
                self.assertEqual((ignored["status"], ignored["reason"]), ("unavailable", "measure_not_numeric"))

    def test_blank_grain_keys_are_distinct_groups_without_export(self):
        self.connections["silver"].execute(
            f"insert into {SLV_T} values ('ZQORD-7197', '   ', 'complete', 1.00, date '2031-07-19')")
        report = self.run_report()
        drift = comparison(report, "order_count", "bronze", "silver")
        self.assertEqual((drift["status"], drift["groups_extra_right"]), ("mismatched", 1))
        self.assertEqual(layer(report, "distinct_customers", "silver")["groups"], 1)
        self.assertEqual(comparison(report, "distinct_customers", "source", "silver")["groups_value_mismatch"], 1)

    def test_gold_join_fan_out_is_detected(self):
        # A duplicated dimension row in a Gold join doubles one day's count and revenue.
        self.connections["gold"].execute(
            f"update {GLD_DAY_T} set {C_CNT} = {C_CNT} * 2, {C_REV} = {C_REV} * 2 where {C_DAY} = date '2031-07-20'")
        report = self.run_report()
        for left in ("silver", "source"):
            fan_out = comparison(report, "daily_revenue", left, "gold")
            self.assertEqual((fan_out["status"], fan_out["groups_value_mismatch"], fan_out["groups_matched"]),
                             ("mismatched", 1, 2))
        self.assertEqual(comparison(report, "daily_revenue", "bronze", "silver")["status"], "matched")
        self.assertEqual(report["status"], "mismatched")
        self.assertIs(report["cutover_ready"], False)

    def test_grain_keys_are_compared_by_text_normalisation(self):
        self.assertEqual(agg._canonical_key_part("1"), agg._canonical_key_part(1))
        self.assertEqual(agg._canonical_key_part(Decimal("1.0")), agg._canonical_key_part(1))
        self.assertEqual(agg._canonical_key_part(True), agg._canonical_key_part("true"))
        import datetime
        self.assertEqual(agg._canonical_key_part(datetime.date(2031, 7, 19)), "2031-07-19")
        self.assertNotEqual(agg._canonical_key_part(" 1"), agg._canonical_key_part(1))

    # ------------------------------------------------------------------ contract

    def test_contract_rejections(self):
        def mutate(change):
            contract = make_contract()
            change(contract)
            return contract

        def set_layer(name, key, value):
            return lambda c: c["metrics"][0]["layers"][name].__setitem__(key, value)

        cases = {
            "unknown top field": lambda c: c.__setitem__("zq_extra", 1),
            "unknown metric field": lambda c: c["metrics"][0].__setitem__("filters", []),
            "unknown layer field": set_layer("bronze", "where", "1=1"),
            "unknown layer name": lambda c: c["metrics"][0]["layers"].__setitem__("platinum", {}),
            "version": lambda c: c.__setitem__("contract_version", 2),
            "bool version": lambda c: c.__setitem__("contract_version", True),
            "min rows": lambda c: c.__setitem__("min_rows_for_total", 0),
            "single layer": lambda c: c["metrics"][2]["layers"].pop("silver"),
            "bad aggregation": lambda c: c["metrics"][0].__setitem__("aggregation", "avg"),
            "count unit": lambda c: c["metrics"][1].__setitem__("unit", "count"),
            "missing currency": lambda c: c["metrics"][1].pop("currency"),
            "bad currency": lambda c: c["metrics"][1].__setitem__("currency", "aud"),
            "currency on count": lambda c: c["metrics"][0].__setitem__("currency", "AUD"),
            "count scale": lambda c: c["metrics"][0].__setitem__("scale", 2),
            "scale range": lambda c: c["metrics"][1].__setitem__("scale", 10),
            "bool scale": lambda c: c["metrics"][1].__setitem__("scale", True),
            "missing null rule": lambda c: c["metrics"][0].pop("null_grain"),
            "missing status rule": lambda c: c["metrics"][0].pop("status_rule"),
            "duplicate grain": lambda c: c["metrics"][0].__setitem__("grain", ["customer", "customer"]),
            "grain width": set_layer("bronze", "grain_columns", [C_CUST, C_DAY]),
            "measure on row count": set_layer("bronze", "measure", C_AMT),
            "sum without measure": lambda c: c["metrics"][1]["layers"]["bronze"].pop("measure"),
            "precounted sum": lambda c: c["metrics"][1]["layers"]["gold"].__setitem__("aggregation", "precounted"),
            "precounted without measure": lambda c: c["metrics"][0]["layers"]["gold"].pop("measure"),
            "status without rule": lambda c: c["metrics"][2]["layers"]["source"].__setitem__("status", {"column": C_STAT}),
            "rule without status": lambda c: c["metrics"][0]["layers"]["bronze"].pop("status"),
            "prefiltered false": set_layer("gold", "status", {"prefiltered": False}),
            "status mixed": set_layer("gold", "status", {"prefiltered": True, "column": C_STAT}),
            "raw mapped twice": set_layer("bronze", "status", {"column": C_STAT, "values": {
                "complete": ["ZqComplete"], "shipped": ["ZqComplete"]}}),
            "empty include": lambda c: c["metrics"][0]["status_rule"].__setitem__("include", []),
            "injection table": set_layer("bronze", "table", 'x"; drop table y; --'),
            "injection column": set_layer("bronze", "grain_columns", ['a" or 1=1 --']),
            "duplicate metric": lambda c: c["metrics"].append(copy.deepcopy(c["metrics"][0])),
            "bad metric name": lambda c: c["metrics"][0].__setitem__("name", "Order Count"),
            "no metrics": lambda c: c.__setitem__("metrics", []),
        }
        for label, change in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                agg.parse_contract(mutate(change))
        agg.parse_contract(make_contract())

    def test_load_contract_rejects_duplicates_and_non_finite(self):
        path = self.root / "contract.json"
        path.write_text('{"contract_version": 1, "contract_version": 1, "metrics": []}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            agg.load_contract(path)
        text = json.dumps(make_contract()).replace('"min_rows_for_total": 10', "").rstrip("}") + ', "min_rows_for_total": NaN}'
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(ValueError):
            agg.load_contract(path)
        path.write_text(json.dumps(make_contract()), encoding="utf-8")
        self.assertEqual(agg.load_contract(path), agg.parse_contract(make_contract()))

    def test_module_does_not_import_loader_or_dlt(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import aggregate_reconciliation; "
                "bad = {'dlt', 'run_migration', 'sqlalchemy', 'duckdb'} & set(sys.modules); "
                "sys.exit(1 if bad else 0)")
        result = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT / "migration")],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_comparison_pairs(self):
        self.assertEqual(agg.comparison_pairs(["gold", "source"]), [("source", "gold")])
        self.assertEqual(agg.comparison_pairs(["silver", "bronze", "gold"]),
                         [("bronze", "silver"), ("silver", "gold"), ("bronze", "gold")])
        with self.assertRaises(ValueError):
            agg.comparison_pairs(["source", "platinum"])
        self.assertEqual(agg.report_filename(RUN_ID), f"{RUN_ID}.aggregate-reconciliation.local.json")
        with self.assertRaises(ValueError):
            agg.report_filename("../zq")

    # ------------------------------------------------------------------ report validation

    def test_report_tampering_is_rejected(self):
        clean = self.run_report()
        self.connections["bronze"].execute(f"insert into {BRZ_T} select * from {BRZ_T} where {C_ORD} = 'ZQORD-7101'")
        base = self.run_report(make_contract(min_rows_for_total=12))

        def path_set(*keys_and_value):
            *keys, value = keys_and_value

            def change(record):
                target = record
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
            return change

        tampering = {
            "overall status": path_set("status", "matched"),
            "metric status": path_set("metrics", 0, "status", "matched"),
            "comparison status": path_set("metrics", 0, "comparisons", 0, "status", "matched"),
            "comparison counts": path_set("metrics", 0, "comparisons", 0, "groups_value_mismatch", 0),
            "count exceeds groups": path_set("metrics", 0, "comparisons", 0, "groups_matched", 99),
            "cutover": path_set("cutover_ready", True),
            "scope": path_set("evidence_scope", "cloud"),
            "extra field": path_set("zq_rows", []),
            "extra layer field": path_set("metrics", 0, "layers", 0, "zq_keys", ["k"]),
            "reason": path_set("metrics", 0, "layers", 0, "reason", "table_unavailable"),
            "rows add up": path_set("metrics", 0, "layers", 0, "rows", "kept", 5),
            "suppressed total disclosed": path_set("metrics", 0, "layers", 3, "total", "11"),
            "total status": path_set("metrics", 0, "layers", 0, "total_status", "emitted"),
            "float total": path_set("metrics", 1, "layers", 1, "total", 4083.72),
            "non canonical total": path_set("metrics", 1, "layers", 1, "total", "4083.7"),
            "fingerprint": path_set("contract_fingerprint", "zz"),
            "run id": path_set("run_id", "not-a-uuid"),
            "pair order": path_set("metrics", 0, "comparisons", 0, "left", "silver"),
            "hidden violation": path_set("metrics", 1, "definition", "null_measure", "fail"),
        }
        bronze_revenue = layer(base, "daily_revenue", "bronze")
        self.assertEqual(bronze_revenue["total_status"], "emitted")
        for label, change in tampering.items():
            record = copy.deepcopy(base)
            if label == "hidden violation":
                record["metrics"][1]["layers"][1]["rows"]["null_measure"] = 1
                record["metrics"][1]["layers"][1]["rows"]["scanned"] += 1
            change(record)
            with self.subTest(label), self.assertRaises(ValueError):
                agg.validate_report(record, expected_run_id=RUN_ID)
        with self.assertRaises(ValueError):
            agg.validate_report(base, expected_run_id="223e4567-e89b-42d3-a456-426614174000")
        self.assertEqual(comparison(clean, "daily_revenue", "source", "bronze")["status"], "matched")
        totals = {
            "matched total": path_set("metrics", 1, "layers", 1, "total", "999.00"),
            "row count total": path_set("metrics", 0, "layers", 0, "total", "3"),
            "precounted sum": path_set("metrics", 1, "layers", 0, "aggregation_mode", "precounted"),
        }
        for label, change in totals.items():
            record = copy.deepcopy(clean)
            change(record)
            with self.subTest(label), self.assertRaises(ValueError):
                agg.validate_report(record, expected_run_id=RUN_ID)
        bronze = self.connections["bronze"]
        bronze.execute(f"create temp table zq_one as select distinct * from {BRZ_T}")
        bronze.execute(f"delete from {BRZ_T}")
        bronze.execute(f"insert into {BRZ_T} select * from zq_one")
        self.connections["silver"].execute(
            f"insert into {SLV_T} values ('ZQORD-7198', 'ZQCUS-5501', 'complete', 1.00, date '2031-07-19')")
        small = self.run_report()
        self.assertEqual(small["metrics"][1]["layers"][2]["total_status"], "suppressed_small_difference")
        for label, change in {
            "small difference disclosed": lambda r: r["metrics"][1]["layers"][2].update(
                total_status="emitted", total="4084.72"),
            "suppressed total disclosed": path_set("metrics", 1, "layers", 2, "total", "4084.72"),
        }.items():
            record = copy.deepcopy(small)
            change(record)
            with self.subTest(label), self.assertRaises(ValueError):
                agg.validate_report(record, expected_run_id=RUN_ID)
        self.assertIs(agg.validate_report(base, expected_run_id=RUN_ID), base)

    # ------------------------------------------------------------------ CLI

    def _cli(self, *args) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = agg.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_writes_private_report_and_refuses_overwrite(self):
        for connection in self.connections.values():
            connection.close()
        contract_path = self.root / "zqcontract_c1.json"
        contract_path.write_text(json.dumps(make_contract()), encoding="utf-8")
        output = self.root / "zqout_dir_o1"
        output.mkdir(mode=0o700)
        os.chmod(output, 0o700)
        args = ["--run-id", RUN_ID, "--contract", str(contract_path), "--output-dir", str(output)]
        for name, path in self.paths.items():
            args += ["--layer", f"{name}={path}"]
        code, out, err = self._cli(*args)
        self.assertEqual(code, 0, err)
        report_path = output / agg.report_filename(RUN_ID)
        self.assertEqual(stat.S_IMODE(report_path.stat().st_mode), 0o600)
        text = report_path.read_text(encoding="utf-8")
        record = json.loads(text)
        agg.validate_report(record, expected_run_id=RUN_ID)
        self.assertEqual(record["status"], "matched")
        self.assertNotIn("zqout_dir_o1", text + out + err)
        self.assertNotIn("zqcontract_c1", text + out + err)
        self.assert_private(text + out + err)
        before = report_path.read_bytes()
        code, out, err = self._cli(*args)
        self.assertEqual(code, 1)
        self.assertIn("refusing to overwrite", err)
        self.assertEqual(report_path.read_bytes(), before)
        self.assert_private(out + err)

    def test_cli_missing_layer_file_is_unavailable_and_not_created(self):
        contract_path = self.root / "zqcontract_c2.json"
        contract_path.write_text(json.dumps(make_contract()), encoding="utf-8")
        output = self.root / "zqout_dir_o2"
        output.mkdir(mode=0o700)
        os.chmod(output, 0o700)
        missing = self.root / "zqmissing_m1.duckdb"
        for connection in self.connections.values():
            connection.close()
        code, out, err = self._cli("--run-id", RUN_ID, "--contract", str(contract_path), "--output-dir", str(output),
                                   "--layer", f"source={self.paths['source']}", "--layer", f"bronze={missing}",
                                   "--layer", f"silver={self.paths['silver']}", "--layer", f"gold={self.paths['gold']}")
        self.assertEqual(code, 0, err)
        self.assertFalse(missing.exists())
        record = json.loads((output / agg.report_filename(RUN_ID)).read_text(encoding="utf-8"))
        self.assertEqual(layer(record, "order_count", "bronze")["reason"], "connection_unavailable")
        self.assertNotIn("zqmissing_m1", json.dumps(record) + out + err)

    def test_cli_rejects_bad_arguments_and_group_readable_output(self):
        contract_path = self.root / "zqcontract_c3.json"
        contract_path.write_text(json.dumps(make_contract()), encoding="utf-8")
        output = self.root / "zqout_dir_o3"
        output.mkdir()
        os.chmod(output, 0o755)
        base = ["--run-id", RUN_ID, "--contract", str(contract_path), "--output-dir", str(output),
                "--layer", f"source={self.paths['source']}", "--layer", f"silver={self.paths['silver']}"]
        code, out, err = self._cli(*base)
        self.assertEqual(code, 1)
        self.assertEqual(list(output.iterdir()), [])
        self.assert_private(out + err)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            agg.main(base + ["--layer", "platinum=/x"])
        os.chmod(output, 0o700)
        code, _out, err = self._cli(*base, "--layer", f"source={self.paths['source']}")
        self.assertEqual(code, 2)
        self.assertIn("more than once", err)
        code, _out, err = self._cli("--run-id", "nope", *base[2:])
        self.assertEqual(code, 2)
        bad_contract = self.root / "zqcontract_c4.json"
        bad_contract.write_text('{"contract_version": 1, "metrics": [], "zq_secret_field_s1": 1}', encoding="utf-8")
        code, _out, err = self._cli("--run-id", RUN_ID, "--contract", str(bad_contract), *base[4:])
        self.assertEqual(code, 2)
        self.assertNotIn("zq_secret_field_s1", err)
        self.assert_private(err)


if __name__ == "__main__":
    unittest.main()
