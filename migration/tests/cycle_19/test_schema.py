"""Card 321: strict envelope schema with explicit units and no silent defaults."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from resource_budget import schema
from resource_budget.schema import BudgetSchemaError, Limit, load_envelope, parse_envelope_json

SECRET = "secret_customer_ssn"


def _config(**limits: dict) -> dict:
    return {"schema_version": 1, "limits": limits or {"install.scratch_bytes": _entry()}}


def _entry(unit: str = "bytes", limit: object = 1024) -> dict:
    return {"unit": unit, "limit": limit}


class EnvelopeAcceptanceTests(unittest.TestCase):
    def test_valid_config_round_trips_explicit_units(self):
        envelope = load_envelope(
            {
                "schema_version": 1,
                "limits": {
                    "install.scratch_bytes": _entry("bytes", 2048),
                    "loader.table_rows": _entry("rows", 10),
                    "install.scratch_files": _entry("files", 5),
                    "archive.members": _entry("members", 40),
                    "tree.depth": _entry("depth", 64),
                    "install.pip_step_seconds": _entry("seconds", 900),
                },
            }
        )
        self.assertEqual(len(envelope), 6)
        self.assertEqual(envelope.limit("install.scratch_bytes", "bytes"), Limit("install.scratch_bytes", "bytes", 2048))
        self.assertEqual(envelope.limit("install.pip_step_seconds", "seconds").value, 900)
        self.assertEqual(list(envelope), sorted(envelope))

    def test_max_bounded_integer_is_accepted(self):
        envelope = load_envelope(_config(**{"install.scratch_bytes": _entry(limit=2**63 - 1)}))
        self.assertEqual(envelope.limit("install.scratch_bytes", "bytes").value, 2**63 - 1)

    def test_missing_limit_is_an_error_not_a_default(self):
        envelope = load_envelope(_config())
        with self.assertRaises(BudgetSchemaError):
            envelope.limit("disk.reserved_headroom_bytes", "bytes")
        self.assertNotIn("disk.reserved_headroom_bytes", envelope)

    def test_unit_mismatch_on_lookup_is_refused(self):
        envelope = load_envelope(_config())
        with self.assertRaises(BudgetSchemaError):
            envelope.limit("install.scratch_bytes", "rows")

    def test_envelope_is_immutable(self):
        envelope = load_envelope(_config())
        with self.assertRaises(AttributeError):
            envelope._limits = {}
        with self.assertRaises(TypeError):
            envelope._limits["install.scratch_bytes"] = Limit("install.scratch_bytes", "bytes", 10**9)


class EnvelopeRejectionTests(unittest.TestCase):
    def assertRefused(self, config):
        with self.assertRaises(BudgetSchemaError) as caught:
            load_envelope(config)
        self.assertNotIn(SECRET, str(caught.exception))
        return caught.exception

    def test_rejects_non_integer_limits(self):
        for bad in (True, False, 1.0, 1024.5, "1024", "1 GiB", "1e3", None, [1024], {"v": 1}):
            with self.subTest(bad=bad):
                self.assertRefused(_config(**{"install.scratch_bytes": _entry(limit=bad)}))

    def test_rejects_zero_negative_and_huge_limits(self):
        for bad in (0, -1, 2**63, 10**40):
            with self.subTest(bad=bad):
                self.assertRefused(_config(**{"install.scratch_bytes": _entry(limit=bad)}))

    def test_rejects_int_subclasses(self):
        class Sneaky(int):
            pass

        self.assertRefused(_config(**{"install.scratch_bytes": _entry(limit=Sneaky(5))}))

    def test_rejects_unit_strings_and_unknown_units(self):
        for bad in ("MB", "GiB", "Bytes", "byte", "", None, "ratio", 1):
            with self.subTest(bad=bad):
                self.assertRefused(_config(**{"install.scratch_bytes": _entry(unit=bad)}))

    def test_key_must_name_its_unit(self):
        self.assertRefused(_config(**{"install.scratch_bytes": _entry(unit="rows")}))
        self.assertRefused(_config(**{"install.scratch": _entry(unit="bytes")}))

    def test_rejects_bad_keys(self):
        for bad in ("bytes", "Install.scratch_bytes", "a.b.c.d.e_bytes", "x." + "a" * 70 + "_bytes", "a..bytes", "a.b-bytes", SECRET):
            with self.subTest(bad=bad):
                self.assertRefused(_config(**{bad: _entry()}))

    def test_rejects_extra_missing_and_nested_fields(self):
        self.assertRefused({"limits": {"install.scratch_bytes": _entry()}})
        self.assertRefused({**_config(), "defaults": {}})
        self.assertRefused(_config(**{"install.scratch_bytes": {"unit": "bytes", "limit": 5, "note": SECRET}}))
        self.assertRefused(_config(**{"install.scratch_bytes": {"unit": "bytes"}}))
        self.assertRefused(_config(**{"install.scratch_bytes": {"unit": "bytes", "limit": {"limit": 5}}}))
        self.assertRefused({"schema_version": 1, "limits": {}})
        self.assertRefused({"schema_version": 1, "limits": [["install.scratch_bytes", _entry()]]})
        self.assertRefused([("schema_version", 1)])

    def test_rejects_bad_schema_versions(self):
        for bad in (True, 2, "1", 1.0, None):
            with self.subTest(bad=bad):
                self.assertRefused({"schema_version": bad, "limits": {"install.scratch_bytes": _entry()}})

    def test_rejects_too_many_limits(self):
        limits = {f"many.item{i}_bytes": _entry() for i in range(schema.MAX_LIMITS + 1)}
        self.assertRefused({"schema_version": 1, "limits": limits})

    def test_error_messages_never_echo_values(self):
        error = self.assertRefused(_config(**{"install.scratch_bytes": _entry(limit=SECRET)}))
        self.assertNotIn(SECRET, str(error))
        error = self.assertRefused(_config(**{"install.scratch_bytes": _entry(unit=SECRET)}))
        self.assertNotIn(SECRET, str(error))


class EnvelopeJsonTests(unittest.TestCase):
    def test_parses_valid_json(self):
        envelope = parse_envelope_json(json.dumps(_config()))
        self.assertEqual(envelope.limit("install.scratch_bytes", "bytes").value, 1024)

    def test_rejects_huge_integer_literals_before_conversion(self):
        text = '{"schema_version": 1, "limits": {"install.scratch_bytes": {"unit": "bytes", "limit": %s}}}'
        for literal in ("1" * 20, "9" * 5000, "-" + "1" * 20):
            with self.subTest(length=len(literal)):
                with self.assertRaises(BudgetSchemaError):
                    parse_envelope_json(text % literal)
        with self.assertRaises(BudgetSchemaError):
            parse_envelope_json(text % "9223372036854775808")

    def test_rejects_floats_exponents_and_constants(self):
        text = '{"schema_version": 1, "limits": {"install.scratch_bytes": {"unit": "bytes", "limit": %s}}}'
        for literal in ("1024.0", "1e3", "NaN", "Infinity", "-Infinity", '"1024"', "true"):
            with self.subTest(literal=literal):
                with self.assertRaises(BudgetSchemaError):
                    parse_envelope_json(text % literal)

    def test_rejects_deep_nesting_without_recursion(self):
        with self.assertRaises(BudgetSchemaError):
            parse_envelope_json("[" * 50_000 + "]" * 50_000)
        with self.assertRaises(BudgetSchemaError):
            parse_envelope_json('{"schema_version": 1, "limits": {"a.b_bytes": {"unit": "bytes", "limit": [1]}}}')

    def test_brackets_inside_strings_do_not_count_as_nesting(self):
        self.assertEqual(schema._json_depth('{"a": "[[[[{{{{\\"]]"}'), 1)

    def test_rejects_duplicate_keys(self):
        text = '{"schema_version": 1, "schema_version": 1, "limits": {"install.scratch_bytes": {"unit": "bytes", "limit": 1}}}'
        with self.assertRaises(BudgetSchemaError):
            parse_envelope_json(text)

    def test_rejects_oversize_text_non_strings_and_invalid_json(self):
        with self.assertRaises(BudgetSchemaError):
            parse_envelope_json(" " * (schema.MAX_CONFIG_TEXT_CHARS + 1))
        for bad in (b"{}", None, 5):
            with self.assertRaises(BudgetSchemaError):
                parse_envelope_json(bad)
        with self.assertRaises(BudgetSchemaError) as caught:
            parse_envelope_json('{"schema_version": 1, "limits": ' + SECRET)
        self.assertNotIn(SECRET, str(caught.exception))


class ProvisionalExampleTests(unittest.TestCase):
    def test_example_is_valid_but_a_fresh_copy(self):
        first = schema.provisional_example_config()
        envelope = load_envelope(first)
        self.assertIn("disk.reserved_headroom_bytes", envelope)
        first["limits"].clear()
        self.assertTrue(schema.provisional_example_config()["limits"])

    def test_example_is_never_a_silent_default(self):
        source = Path(schema.__file__).read_text(encoding="utf-8")
        self.assertIn("PROVISIONAL EXAMPLE ONLY", source)
        package_dir = Path(schema.__file__).parent
        for module in package_dir.glob("*.py"):
            text = module.read_text(encoding="utf-8")
            uses = text.count("_PROVISIONAL_EXAMPLE_CONFIG")
            if module.name == "schema.py":
                self.assertEqual(uses, 2, "only the definition and the copy function")
            else:
                self.assertEqual(uses, 0)
                self.assertNotIn("provisional_example_config", text)


if __name__ == "__main__":
    unittest.main()
