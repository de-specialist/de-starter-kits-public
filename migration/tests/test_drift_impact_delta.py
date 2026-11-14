"""Offline tests for drift_impact.delta: stable deltas and value-free input rejection."""

from __future__ import annotations

import copy
import sys
import traceback
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drift_impact import delta as delta_module
from drift_impact.delta import DELTA_ALGORITHM, DeltaInputError, normalize_deltas
from schema_drift import (
    schema_confirmation_fingerprint,
    snapshot_name_fingerprint,
    snapshot_typed_fingerprint,
)


SECRET = "secret_customer_ssn"
MAPPING = "a" * 64
FIXED_MESSAGES = {
    f"The {side} input {suffix}"
    for side in ("old", "new")
    for suffix in (
        "must be a verified reviewed-schema record.",
        "has an unsupported reviewed-schema kind.",
        "is missing a SHA-256 fingerprint.",
        "snapshot is not bounded schema metadata.",
        "snapshot does not match its recorded fingerprints.",
        "is not a usable reviewed-schema record.",
    )
} | {"The reviewed-schema inputs could not be normalized."}
# Unique sentinel so callers can pass an explicit None as the new-side record.
_DEFAULT_NEW = object()


def _column(name: str, family: str) -> dict[str, str]:
    return {"name": name, "type_family": family}


def _record(snapshot: list[dict[str, object]], kind: str = "dry_run") -> dict[str, object]:
    names = snapshot_name_fingerprint(snapshot)
    typed = snapshot_typed_fingerprint(snapshot)
    return {
        "kind": kind,
        "snapshot": snapshot,
        "source_schema_fingerprint": names,
        "typed_schema_fingerprint": typed,
        "source_mapping_fingerprint": MAPPING,
        "schema_confirmation_fingerprint": schema_confirmation_fingerprint(names, typed, MAPPING),
    }


def _valid() -> dict[str, object]:
    return _record([{"table": "customers", "columns": [_column("id", "integer")]}])


def _with_snapshot(snapshot: object) -> dict[str, object]:
    """A record whose fingerprint fields are well-formed but whose snapshot is arbitrary."""
    record = _valid()
    record["snapshot"] = snapshot
    return record


def _table(columns: object, table: object = "customers") -> list[object]:
    return [{"table": table, "columns": columns}]


class _HostileStr(str):
    """A str subclass whose hooks would leak the identifier if any of them were called."""

    def casefold(self):
        raise RuntimeError(f"casefold leaked {SECRET}")

    def __len__(self):
        raise RuntimeError(f"len leaked {SECRET}")

    def startswith(self, *args):
        raise RuntimeError(f"startswith leaked {SECRET}")


class _HostileKey:
    def __hash__(self):
        return 1

    def __eq__(self, other):
        raise RuntimeError(f"key eq leaked {SECRET}")

    def __repr__(self):
        return SECRET


class _HostileDict(dict):
    def keys(self):
        raise RuntimeError(f"keys leaked {SECRET}")

    def __iter__(self):
        raise RuntimeError(f"iter leaked {SECRET}")


class _HostileList(list):
    def __iter__(self):
        raise RuntimeError(f"iter leaked {SECRET}")

    def __len__(self):
        raise RuntimeError(f"len leaked {SECRET}")


class StableDeltaTests(unittest.TestCase):
    OLD = [
        {"table": "customers", "columns": [
            _column("id", "integer"), _column("email", "text"), _column("dup", "text"),
            _column("dup", "integer"), _column("created_at", "timestamp"),
        ]},
        {"table": "legacy", "columns": [_column("x", "integer")]},
        {"table": "Orders", "columns": [
            _column("id", "integer"), _column("total", "numeric"), _column("status", "text"),
        ]},
    ]
    NEW = [
        {"table": "customers", "columns": [
            _column("email", "text"), _column("id", "numeric"), _column("dup", "text"),
            _column("Created_At", "timestamp"), _column("phone", "other:geography"),
        ]},
        {"table": "orders", "columns": [_column("id", "integer")]},
        {"table": "shipments", "columns": [_column("id", "integer"), _column("carrier", "text")]},
    ]
    # (kind, scope, table, column, occurrence, old_pos, new_pos, old_family, new_family,
    #  via_table_change, unrecognized_type_family), in the documented total order.
    EXPECTED = [
        ("removed", "column", "customers", "dup", 2, 4, None, "integer", None, False, False),
        ("removed", "column", "customers", "created_at", 1, 5, None, "timestamp", None, False, False),
        ("added", "column", "customers", "Created_At", 1, None, 4, None, "timestamp", False, False),
        ("added", "column", "customers", "phone", 1, None, 5, None, "other:geography", False, True),
        ("retyped", "column", "customers", "id", 1, 1, 2, "integer", "numeric", False, False),
        ("reordered", "column", "customers", "id", 1, 1, 2, "integer", "numeric", False, False),
        ("removed", "table", "legacy", None, None, None, None, None, None, False, False),
        ("removed", "column", "legacy", "x", 1, 1, None, "integer", None, True, False),
        ("removed", "table", "Orders", None, None, None, None, None, None, False, False),
        ("removed", "column", "Orders", "id", 1, 1, None, "integer", None, True, False),
        ("removed", "column", "Orders", "total", 1, 2, None, "numeric", None, True, False),
        ("removed", "column", "Orders", "status", 1, 3, None, "text", None, True, False),
        ("added", "table", "orders", None, None, None, None, None, None, False, False),
        ("added", "column", "orders", "id", 1, None, 1, None, "integer", True, False),
        ("added", "table", "shipments", None, None, None, None, None, None, False, False),
        ("added", "column", "shipments", "id", 1, None, 1, None, "integer", True, False),
        ("added", "column", "shipments", "carrier", 1, None, 2, None, "text", True, False),
    ]
    FIELDS = (
        "kind", "scope", "table", "column", "occurrence", "old_position", "new_position",
        "old_type_family", "new_type_family", "via_table_change", "unrecognized_type_family",
    )

    def test_added_removed_retyped_reordered_are_stable_and_ordered(self) -> None:
        old, new = _record(self.OLD), _record(self.NEW, "run_manifest")
        before = copy.deepcopy((old, new))
        result = normalize_deltas(old, new)
        self.assertEqual((old, new), before, "inputs must not be mutated")
        self.assertEqual(
            [tuple(delta[field] for field in self.FIELDS) for delta in result["deltas"]], self.EXPECTED
        )
        self.assertEqual(result["delta_count"], len(self.EXPECTED))
        self.assertEqual(result["counts"], {
            "table": {"removed": 2, "added": 2},
            "column": {"removed": 6, "added": 5, "retyped": 1, "reordered": 1},
        })
        self.assertEqual(result["algorithm"], DELTA_ALGORITHM)
        self.assertEqual(result["old"]["kind"], "dry_run")
        self.assertEqual(result["new"]["kind"], "run_manifest")
        self.assertTrue(result["schema_changed"])
        self.assertFalse(result["fingerprint_change_without_delta"])
        self.assertFalse(result["mapping_fingerprint_changed"])
        ids = [delta["delta_id"] for delta in result["deltas"]]
        self.assertEqual(len(set(ids)), len(ids))
        # Deterministic across repeated calls and fresh copies.
        again = normalize_deltas(copy.deepcopy(old), copy.deepcopy(new))
        self.assertEqual(again, result)

    def test_identical_snapshots_yield_no_deltas(self) -> None:
        result = normalize_deltas(_record(self.OLD), _record(copy.deepcopy(self.OLD)))
        self.assertEqual(result["deltas"], [])
        self.assertFalse(result["schema_changed"])
        self.assertFalse(result["fingerprint_change_without_delta"])


class MalformedInputTests(unittest.TestCase):
    def assert_rejected(self, old: object, new: object = _DEFAULT_NEW) -> DeltaInputError:
        new = _valid() if new is _DEFAULT_NEW else new
        with self.assertRaises(DeltaInputError) as caught:
            normalize_deltas(old, new)
        error = caught.exception
        self.assertIs(type(error), DeltaInputError)
        self.assertIn(str(error), FIXED_MESSAGES)
        self.assertNotIn(SECRET, str(error))
        self.assertNotIn(SECRET, repr(error))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        return error

    def test_top_level_record_shapes(self) -> None:
        good = _valid()
        cases = {
            "none": None,
            "list": [good],
            "string": SECRET,
            "dict_subclass": _HostileDict(good),
            "missing_key": {key: value for key, value in good.items() if key != "snapshot"},
            "extra_key": {**good, SECRET: SECRET},
            "non_str_key": {**{k: v for k, v in good.items() if k != "kind"}, 1: "dry_run"},
            "hostile_key": {**{k: v for k, v in good.items() if k != "kind"}, _HostileKey(): "dry_run"},
            "str_subclass_key": {
                **{k: v for k, v in good.items() if k != "kind"}, _HostileStr("kind"): "dry_run",
            },
        }
        for label, record in cases.items():
            with self.subTest(label):
                self.assert_rejected(record)
                self.assert_rejected(_valid(), record)

    def test_kind_values(self) -> None:
        for label, kind in {
            "list": [SECRET],
            "dict": {SECRET: 1},
            "set": {SECRET},
            "int": 1,
            "none": None,
            "unknown": SECRET,
            "str_subclass": _HostileStr("dry_run"),
        }.items():
            with self.subTest(label):
                record = _valid()
                record["kind"] = kind
                error = self.assert_rejected(record)
                self.assertEqual(str(error), "The old input has an unsupported reviewed-schema kind.")

    def test_fingerprint_values(self) -> None:
        for field in delta_module._FINGERPRINT_KEYS:
            for label, value in {
                "list": ["a" * 64],
                "uppercase": "A" * 64,
                "short": "a" * 63,
                "str_subclass": _HostileStr("a" * 64),
                "none": None,
            }.items():
                with self.subTest(field=field, value=label):
                    record = _valid()
                    record[field] = value
                    self.assert_rejected(record)

    def test_extremely_long_fingerprint_is_rejected_without_leaking(self) -> None:
        # A valid 64-hex prefix followed by ~1 MB of identifier-bearing payload.
        payload = "a" * 64 + SECRET * 50_000
        for side in ("old", "new"):
            for field in delta_module._FINGERPRINT_KEYS:
                with self.subTest(side=side, field=field):
                    record = _valid()
                    record[field] = payload
                    if side == "old":
                        error = self.assert_rejected(record)
                    else:
                        error = self.assert_rejected(_valid(), record)
                    message = f"The {side} input is missing a SHA-256 fingerprint."
                    self.assertEqual(str(error), message)
                    self.assertEqual(error.args, (message,))
                    self.assertNotIn(payload, repr(error))
                    self.assertLess(len(repr(error)), 200)

    def test_fingerprint_mismatch(self) -> None:
        record = _valid()
        record["source_mapping_fingerprint"] = "b" * 64
        error = self.assert_rejected(record)
        self.assertEqual(str(error), "The old input snapshot does not match its recorded fingerprints.")

    def test_nested_snapshot_values(self) -> None:
        ok = [_column("id", "integer")]
        cases = {
            "snapshot_dict": {SECRET: ok},
            "snapshot_str": SECRET,
            "snapshot_list_subclass": _HostileList(_table(ok)),
            "too_many_tables": [
                {"table": f"t{index}", "columns": []}
                for index in range(delta_module._MAX_TABLES + 1)
            ],
            "table_entry_list": [[SECRET, ok]],
            "table_entry_dict_subclass": [_HostileDict({"table": SECRET, "columns": ok})],
            "table_entry_extra_key": [{"table": SECRET, "columns": ok, "rows": [SECRET]}],
            "table_entry_hostile_key": [{"table": SECRET, _HostileKey(): ok}],
            "table_name_list": _table(ok, [SECRET]),
            "table_name_int": _table(ok, 7),
            "table_name_str_subclass": _table(ok, _HostileStr(SECRET)),
            "table_name_empty": _table(ok, ""),
            "table_name_too_long": _table(ok, SECRET * 100),
            "table_name_surrogate": _table(ok, SECRET + "\ud800"),
            "duplicate_tables_ignoring_case": _table(ok, SECRET) + _table(ok, SECRET.upper()),
            "columns_dict": _table({SECRET: "integer"}),
            "columns_list_subclass": _table(_HostileList(ok)),
            "too_many_columns": _table([
                _column(f"c{index}", "text") for index in range(delta_module._MAX_COLUMNS_PER_TABLE + 1)
            ]),
            "column_list": _table([[SECRET, "integer"]]),
            "column_dict_subclass": _table([_HostileDict(_column(SECRET, "integer"))]),
            "column_extra_key": _table([{**_column(SECRET, "integer"), "value": SECRET}]),
            "column_hostile_key": _table([{"name": SECRET, _HostileKey(): "integer"}]),
            "column_name_list": _table([_column([SECRET], "integer")]),
            "column_name_str_subclass": _table([_column(_HostileStr(SECRET), "integer")]),
            "column_name_surrogate": _table([_column(SECRET + "\udfff", "integer")]),
            "column_name_too_long": _table([_column(SECRET * 100, "integer")]),
            "type_family_dict": _table([_column(SECRET, {SECRET: "integer"})]),
            "type_family_str_subclass": _table([_column("id", _HostileStr(SECRET))]),
            "type_family_surrogate": _table([_column("id", "other:" + SECRET + "\ud83d")]),
        }
        for label, snapshot in cases.items():
            with self.subTest(label):
                error = self.assert_rejected(_with_snapshot(snapshot))
                self.assertEqual(str(error), "The old input snapshot is not bounded schema metadata.")
                error = self.assert_rejected(_valid(), _with_snapshot(snapshot))
                self.assertEqual(str(error), "The new input snapshot is not bounded schema metadata.")

    def test_valid_non_bmp_text_is_accepted(self) -> None:
        snapshot = _table([_column("naïve_\U0001f600", "text")], "tbl_é")
        result = normalize_deltas(_record(snapshot), _record(snapshot))
        self.assertEqual(result["deltas"], [])

    def test_unexpected_helper_exception_is_sanitized(self) -> None:
        def leaky(_snapshot):
            raise RuntimeError(f"helper leaked {SECRET}")

        with mock.patch.object(delta_module, "snapshot_name_fingerprint", leaky):
            error = self.assert_rejected(_valid())
        self.assertEqual(str(error), "The old input is not a usable reviewed-schema record.")

    def test_rejected_input_is_not_mutated(self) -> None:
        record = _with_snapshot(_table([_column(SECRET, [SECRET])]))
        before = copy.deepcopy(record)
        self.assert_rejected(record)
        self.assertEqual(record, before)


def _raised(call, *args) -> BaseException:
    """Call and return the escaping exception with its traceback (assertRaises strips it)."""
    try:
        call(*args)
    except BaseException as error:  # noqa: BLE001 - the test inspects whatever escapes
        return error
    raise AssertionError("expected an exception")


class _KeyErrorDict(dict):
    def __getitem__(self, key):
        raise KeyError(f"column leaked {SECRET}")


class ConcurrentMutationTests(unittest.TestCase):
    """Caller mutation after verification must not change, break, or leak from the result."""

    OLD = [{"table": "customers", "columns": [_column("id", "integer"), _column("email", "text")]}]
    NEW = [{"table": "customers", "columns": [_column("email", "text"), _column("id", "bigint")]}]

    def _records(self) -> tuple[dict[str, object], dict[str, object]]:
        return _record(copy.deepcopy(self.OLD)), _record(copy.deepcopy(self.NEW))

    def assert_clean(self, error: BaseException, message: str | None = None) -> None:
        self.assertIs(type(error), DeltaInputError)
        self.assertIn(str(error), FIXED_MESSAGES)
        if message is not None:
            self.assertEqual(str(error), message)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertTrue(error.__suppress_context__ or error.__context__ is None)
        surfaces = [str(error), repr(error), repr(error.args)]
        surfaces.append("".join(traceback.format_exception(error)))
        surfaces.append("".join(traceback.TracebackException.from_exception(error, capture_locals=True).format()))
        for surface in surfaces:
            self.assertNotIn(SECRET, surface)
        frames = [frame.name for frame in traceback.extract_tb(error.__traceback__)]
        # Raised fresh from normalize_deltas: no internal helper frame (or its locals) attached.
        self.assertEqual(frames[-1], "normalize_deltas")
        for helper in ("_normalize", "_verified_side", "_checked_side", "_shared_table", "_delta"):
            self.assertNotIn(helper, frames)

    def _run_with_mutation(self, mutate) -> dict[str, object]:
        """Mutate the caller's new-side snapshot inside the last verification seam."""
        old, new = self._records()
        original = delta_module.schema_confirmation_fingerprint
        calls = {"count": 0}

        def mutating(*args):
            calls["count"] += 1
            if calls["count"] == 2:
                mutate(new["snapshot"])
            return original(*args)

        with mock.patch.object(delta_module, "schema_confirmation_fingerprint", mutating):
            result = normalize_deltas(old, new)
        self.assertEqual(calls["count"], 2)
        return result

    def test_mutation_after_verification_does_not_affect_result(self) -> None:
        expected = normalize_deltas(*self._records())
        mutations = {
            "dict_subclass_column": lambda s: s[0]["columns"].__setitem__(0, _KeyErrorDict(s[0]["columns"][0])),
            "int_column_name": lambda s: s[0]["columns"][0].__setitem__("name", 7),
            "surrogate_type_family": lambda s: s[0]["columns"][0].__setitem__("type_family", SECRET + "\ud800"),
            "int_table_name": lambda s: s[0].__setitem__("table", 7),
            "str_subclass_column_name": lambda s: s[0]["columns"][1].__setitem__("name", _HostileStr(SECRET)),
            "cleared": lambda s: s.clear(),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                self.assertEqual(self._run_with_mutation(mutate), expected)

    def test_output_unaffected_by_post_call_mutation(self) -> None:
        old, new = self._records()
        result = normalize_deltas(old, new)
        before = copy.deepcopy(result)
        for record in (old, new):
            record["kind"] = SECRET
            for entry in record["snapshot"]:
                entry["table"] = SECRET
                for column in entry["columns"]:
                    column["name"] = SECRET
                    column["type_family"] = SECRET
                entry["columns"].append(_column(SECRET, SECRET))
            record["snapshot"].append({"table": SECRET, "columns": []})
        self.assertEqual(result, before)
        self.assertNotIn(SECRET, repr(result))

    def test_exception_during_delta_building_is_sanitized(self) -> None:
        leaks = {
            "unicode": UnicodeEncodeError("utf-8", SECRET + "\ud800", 0, 1, f"surrogate {SECRET}"),
            "key": KeyError(f"column {SECRET}"),
            "runtime": RuntimeError(f"helper {SECRET}"),
            "delta_input": DeltaInputError(f"forged {SECRET}"),
        }
        for label, leak in leaks.items():
            def raising(*_args, _leak=leak):
                raise _leak

            for seam in ("_canonical_digest", "_occurrence_keys", "_stable_indices"):
                with self.subTest(label=label, seam=seam):
                    with mock.patch.object(delta_module, seam, raising):
                        error = _raised(normalize_deltas, *self._records())
                    self.assert_clean(error, "The reviewed-schema inputs could not be normalized.")

    def test_verification_error_carries_no_helper_frames(self) -> None:
        record = _valid()
        record["kind"] = SECRET
        error = _raised(normalize_deltas, _valid(), record)
        self.assertIs(type(error), DeltaInputError)
        self.assertEqual(str(error), "The new input has an unsupported reviewed-schema kind.")
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        frames = [frame.name for frame in traceback.extract_tb(error.__traceback__)]
        self.assertEqual(frames[-1], "normalize_deltas")
        self.assertNotIn("_checked_side", frames)

    def test_subclass_snapshot_containers_and_text_rejected(self) -> None:
        cases = {
            "dict_subclass_table": [_KeyErrorDict({"table": "customers", "columns": []})],
            "dict_subclass_column": _table([_KeyErrorDict(_column("id", "integer"))]),
            "str_subclass_table": _table([_column("id", "integer")], _HostileStr("customers")),
            "str_subclass_column": _table([_column(_HostileStr("id"), "integer")]),
            "str_subclass_family": _table([_column("id", _HostileStr("integer"))]),
        }
        for label, snapshot in cases.items():
            with self.subTest(label):
                error = _raised(normalize_deltas, _valid(), _with_snapshot(snapshot))
                self.assert_clean(error, "The new input snapshot is not bounded schema metadata.")

    def test_result_shares_no_mutable_object_with_input(self) -> None:
        old, new = self._records()
        result = normalize_deltas(old, new)
        inputs = {id(old), id(new), id(old["snapshot"]), id(new["snapshot"])}
        for record in (old, new):
            for entry in record["snapshot"]:
                inputs.add(id(entry))
                inputs.add(id(entry["columns"]))
                inputs.update(id(column) for column in entry["columns"])
        outputs = {id(result), id(result["old"]), id(result["new"]), id(result["deltas"])}
        outputs.update(id(delta) for delta in result["deltas"])
        self.assertFalse(inputs & outputs)


if __name__ == "__main__":
    unittest.main()
