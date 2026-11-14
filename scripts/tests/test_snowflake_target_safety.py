"""Guard Snowflake setup/ops examples and profile defaults against unsafe targets.

Docs and the example profile must name only disposable Terraform-style targets
(DBT_KIT_DEV_<suffix>_DB / _SCHEMA / _WH), never the legacy shared DBT_DEV
target, and the example profile must not fall back to any target identifier.
Offline only: reads repository files, never connects to Snowflake.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EDITION = ROOT / "editions/Snowflake"
SETUP_DOC = EDITION / "docs/setup.md"
OPERATIONS_DOC = EDITION / "docs/operations.md"
PROFILE_EXAMPLE = EDITION / "kit/profiles.example.yml"
FIRST_RUN_DOC = ROOT / "docs/first-run.md"
GUARDED_FILES = (SETUP_DOC, OPERATIONS_DOC, PROFILE_EXAMPLE, FIRST_RUN_DOC)

# Unquoted Snowflake identifiers are case-insensitive; DBT_DEV_ROLE (a role
# name) is allowed, but the bare DBT_DEV database/schema and its warehouse are not.
LEGACY_TARGET = re.compile(r"(?<![A-Za-z0-9_$])DBT_DEV(?:_WH)?(?![A-Za-z0-9_$])", re.IGNORECASE)

PREFIX = r"DBT_KIT_(?:DEV|CI)(?:_(?:<suffix>|[A-Z0-9]+))+"
SAFE_NAME = {
    "DATABASE": re.compile(rf"^{PREFIX}_DB$"),
    "SCHEMA": re.compile(rf"^(?P<db>{PREFIX})_DB\.(?P=db)_SCHEMA$"),
    "SCHEMA_UNQUALIFIED": re.compile(rf"^{PREFIX}_SCHEMA$"),
    "WAREHOUSE": re.compile(rf"^{PREFIX}_WH$"),
}
SQL_OBJECT_REF = re.compile(
    r"\b(DATABASE|SCHEMA|WAREHOUSE)\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?([^\s;']+)",
    re.IGNORECASE,
)
SHOW_WAREHOUSES_LIKE = re.compile(r"\bSHOW\s+WAREHOUSES\s+LIKE\s+'([^']+)'", re.IGNORECASE)
DROP_STATEMENT = re.compile(r"\bDROP\s+(DATABASE|SCHEMA|WAREHOUSE)\b[^;]*;?", re.IGNORECASE)
TARGET_KEYS = ("role", "database", "warehouse", "schema")


def fenced_blocks(markdown: str, language: str) -> list[str]:
    return re.findall(rf"^```{language}\n(.*?)^```", markdown, re.DOTALL | re.MULTILINE)


def legacy_target_hits(text: str) -> list[str]:
    return [m.group(0) for m in LEGACY_TARGET.finditer(text)]


def unsafe_sql_refs(sql: str) -> list[str]:
    problems = []
    for kind, name in SQL_OBJECT_REF.findall(sql):
        if not SAFE_NAME[kind.upper()].fullmatch(name):
            problems.append(f"{kind.upper()} {name}")
    for name in SHOW_WAREHOUSES_LIKE.findall(sql):
        if not SAFE_NAME["WAREHOUSE"].fullmatch(name):
            problems.append(f"SHOW WAREHOUSES LIKE {name}")
    return problems


def unsafe_drops(sql: str) -> list[str]:
    return [m.group(0) for m in DROP_STATEMENT.finditer(sql) if unsafe_sql_refs(m.group(0))]


def profile_output(profile_text: str, output: str) -> dict[str, str]:
    match = re.search(rf"^    {output}:\n((?:      .*\n?)+)", profile_text, re.MULTILINE)
    if match is None:
        raise AssertionError(f"profile output {output!r} not found")
    pairs = re.findall(r"^      (\w+):\s*(.*)$", match.group(1), re.MULTILINE)
    return dict(pairs)


def target_fallbacks(output: dict[str, str]) -> list[str]:
    problems = []
    for key in TARGET_KEYS:
        value = output.get(key, "")
        if not re.fullmatch(r"\"\{\{ env_var\('SNOWFLAKE_[A-Z]+'\) \}\}\"", value):
            problems.append(f"{key}: {value}")
    return problems


class SnowflakeTargetSafetyTests(unittest.TestCase):
    def test_guarded_files_never_reference_legacy_dbt_dev_target(self) -> None:
        for path in GUARDED_FILES:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertEqual(legacy_target_hits(path.read_text(encoding="utf-8")), [])

    def test_example_profile_target_identifiers_have_no_fallback(self) -> None:
        dev = profile_output(PROFILE_EXAMPLE.read_text(encoding="utf-8"), "dev")
        self.assertEqual(target_fallbacks(dev), [])
        self.assertEqual(dev["schema"], "\"{{ env_var('SNOWFLAKE_SCHEMA') }}\"")
        self.assertEqual(dev["database"], "\"{{ env_var('SNOWFLAKE_DATABASE') }}\"")
        self.assertEqual(dev["warehouse"], "\"{{ env_var('SNOWFLAKE_WAREHOUSE') }}\"")

    def test_doc_sql_targets_only_disposable_terraform_names(self) -> None:
        for path in (SETUP_DOC, OPERATIONS_DOC):
            blocks = fenced_blocks(path.read_text(encoding="utf-8"), "sql")
            self.assertTrue(blocks, f"no SQL examples found in {path.name}")
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertEqual([p for b in blocks for p in unsafe_sql_refs(b)], [])

    def test_doc_drop_statements_target_only_disposable_names(self) -> None:
        sql = "\n".join(fenced_blocks(OPERATIONS_DOC.read_text(encoding="utf-8"), "sql"))
        drops = [m.group(0) for m in DROP_STATEMENT.finditer(sql)]
        self.assertTrue(drops, "expected a guarded teardown example")
        self.assertEqual(unsafe_drops(sql), [])
        for drop in drops:
            self.assertIn("DBT_KIT_DEV_<suffix>", drop)

    def test_setup_profile_and_env_examples_use_disposable_names(self) -> None:
        setup = SETUP_DOC.read_text(encoding="utf-8")
        yaml_blocks = [b for b in fenced_blocks(setup, "yaml") if "reliable_pipeline:" in b]
        self.assertEqual(len(yaml_blocks), 1)
        dev = profile_output(yaml_blocks[0], "dev")
        self.assertRegex(dev["database"], SAFE_NAME["DATABASE"])
        self.assertRegex(dev["schema"], SAFE_NAME["SCHEMA_UNQUALIFIED"])
        self.assertRegex(dev["warehouse"], SAFE_NAME["WAREHOUSE"])

        exports = dict(re.findall(r"^export (SNOWFLAKE_[A-Z]+)=(\S+)$", setup, re.MULTILINE))
        self.assertRegex(exports["SNOWFLAKE_DATABASE"], SAFE_NAME["DATABASE"])
        self.assertRegex(exports["SNOWFLAKE_SCHEMA"], SAFE_NAME["SCHEMA_UNQUALIFIED"])
        self.assertRegex(exports["SNOWFLAKE_WAREHOUSE"], SAFE_NAME["WAREHOUSE"])
        for key in ("SNOWFLAKE_DATABASE", "SNOWFLAKE_SCHEMA", "SNOWFLAKE_WAREHOUSE"):
            self.assertIn("DBT_KIT_DEV_<suffix>_", exports[key])

    def test_checks_reject_previous_unsafe_examples(self) -> None:
        """The detectors must flag the exact patterns this guard replaced."""
        self.assertTrue(legacy_target_hits("CREATE DATABASE IF NOT EXISTS dbt_dev;"))
        self.assertTrue(legacy_target_hits("SHOW WAREHOUSES LIKE 'DBT_DEV_WH';"))
        self.assertFalse(legacy_target_hits("GRANT ROLE DBT_DEV_ROLE TO USER u;"))
        self.assertTrue(unsafe_drops("DROP SCHEMA IF EXISTS DBT_DEV.DBT_DEV CASCADE;"))
        self.assertTrue(unsafe_drops("DROP DATABASE PROD_DB;"))
        self.assertTrue(unsafe_sql_refs(
            "DROP SCHEMA IF EXISTS DBT_KIT_DEV_A_DB.DBT_KIT_DEV_B_SCHEMA CASCADE;"
        ))
        self.assertFalse(unsafe_drops(
            "DROP SCHEMA IF EXISTS DBT_KIT_DEV_TF_DB.DBT_KIT_DEV_TF_SCHEMA CASCADE;"
        ))
        self.assertTrue(unsafe_sql_refs("ALTER WAREHOUSE DBT_DEV_WH SUSPEND;"))
        self.assertTrue(unsafe_sql_refs("CREATE DATABASE IF NOT EXISTS DBT_KIT_DEVPROD_DB;"))
        unsafe_profile = (
            "    dev:\n"
            "      role: \"{{ env_var('SNOWFLAKE_ROLE', 'TRANSFORMER') }}\"\n"
            "      database: \"{{ env_var('SNOWFLAKE_DATABASE') }}\"\n"
            "      warehouse: \"{{ env_var('SNOWFLAKE_WAREHOUSE') }}\"\n"
            "      schema: \"{{ env_var('SNOWFLAKE_SCHEMA', 'DBT_DEV') }}\"\n"
        )
        self.assertEqual(
            [p.split(":")[0] for p in target_fallbacks(profile_output(unsafe_profile, "dev"))],
            ["role", "schema"],
        )


if __name__ == "__main__":
    unittest.main()
