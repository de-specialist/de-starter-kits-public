"""Offline tests for the plan-only Silver scaffolder core .

All fixtures are SYNTHETIC and live in temporary directories outside the repository. Nothing
here contacts a source, warehouse, or network, and no credentials or profiles are read.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scaffold_fixtures as F  # noqa: E402

from scaffold import cli, plan as plan_module, reasons as R  # noqa: E402
from scaffold.inputs import validate_inputs  # noqa: E402
from scaffold.plan import (  # noqa: E402
    ColumnPlan,
    ScaffoldPlan,
    build_plan,
    register_renderer,
    render_plan,
    resolve_renderer,
)


FORBIDDEN_MODULES = ("dlt", "sqlalchemy", "run_migration", "dbt", "google", "snowflake", "duckdb", "requests")


class ScaffoldCase(unittest.TestCase):
    edition = "bigquery"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="scaffold-synthetic-"))
        self.addCleanup(self._cleanup)
        self.workspace = F.make_workspace(self.root, self.edition)
        self.project = F.project_dir(self.workspace, self.edition)
        dry, manifest = F.build_evidence(F.default_mapping(), destination=self.edition)
        self.paths = F.write_bundle(self.root, F.default_mapping(), dry, manifest)
        # Renderer modules may be added by other tasks; keep core tests independent of them.
        for edition in plan_module.EDITIONS:
            register_renderer(edition, None)
        self._saved_modules = dict(plan_module.RENDERER_MODULES)
        plan_module.RENDERER_MODULES.update(
            {e: (f"scaffold.templates._absent_{e}", "render") for e in plan_module.EDITIONS}
        )

    def _cleanup(self):
        plan_module.RENDERER_MODULES.clear()
        plan_module.RENDERER_MODULES.update(self._saved_modules)
        for edition in plan_module.EDITIONS:
            register_renderer(edition, None)
        for directory, dirnames, _ in os.walk(self.root):
            for name in dirnames:
                path = Path(directory) / name
                if not path.is_symlink():
                    os.chmod(path, 0o700)
        shutil.rmtree(self.root, ignore_errors=True)

    def invoke(self, args=None, **kwargs):
        kwargs.setdefault("edition", self.edition)
        args = args if args is not None else F.argv(self.workspace, self.paths, **kwargs)
        before = F.tree_snapshot(self.root)
        code, stdout, stderr = cli.run(args, now=F.NOW)
        self.assertEqual(before, F.tree_snapshot(self.root), "scaffolder changed the filesystem")
        report = json.loads(stdout) if stdout else None
        return code, report, stdout, stderr

    def assertRefused(self, expected, **kwargs):
        for dry_run in (True, False):
            code, report, stdout, stderr = self.invoke(dry_run=dry_run, **kwargs)
            self.assertEqual(code, cli.EXIT_REFUSED, stdout)
            self.assertEqual(report["status"], "refused")
            self.assertEqual(report["reason_codes"], [expected])
            self.assertEqual(report["planned_files"], [])
            self.assertFalse(report["writes_performed"])
            self.assertEqual(stderr, "")
            self.assertNotIn(str(self.root), stdout)


class PlanOnlyTests(ScaffoldCase):
    def test_plan_only_lists_project_relative_files(self):
        code, report, stdout, _ = self.invoke()
        self.assertEqual(code, cli.EXIT_PLAN_ONLY)
        self.assertEqual(report["status"], "plan_only")
        self.assertEqual(report["mode"], "plan_only")
        self.assertEqual(report["reason_codes"], [])
        self.assertEqual(report["tables"], ["customers", "order_items", "orders", "products"])
        self.assertEqual(report["output_dir"], "models/migration/generated/synth_src")
        self.assertIn("models/migration/generated/synth_src/silver_synth_src__orders.sql", report["planned_files"])
        self.assertIn("models/migration/generated/synth_src/.scaffold-manifest.json", report["planned_files"])
        self.assertTrue(all(not path.startswith("/") for path in report["planned_files"]))
        self.assertNotIn("ready", stdout.replace("renderer_available", ""))

    def test_output_is_deterministic(self):
        first = self.invoke()[2]
        second = self.invoke()[2]
        self.assertEqual(first, second)

    def test_default_mode_refuses_final_generation(self):
        code, report, _, _ = self.invoke(dry_run=False)
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertEqual(report["reason_codes"], [R.FINAL_GENERATION_GATED])
        self.assertEqual(report["mode"], "generate")
        self.assertFalse((self.project / "models" / "migration" / "generated").exists())
        self.assertFalse((self.project / ".scaffold-staging").exists())

    def test_unknown_update_flag_is_usage_error(self):
        for flag in ("--update", "--force", "--merge"):
            code, report, _, stderr = self.invoke(extra=[flag])
            self.assertEqual(code, cli.EXIT_USAGE)
            self.assertIsNone(report)
            self.assertIn("no update, force, or merge mode", stderr)

    def test_usage_errors_never_echo_values(self):
        canary = "canary_secret_value"
        args = ["--edition", canary, "--workspace", str(self.workspace), "--source-id", "x"]
        code, _, stdout, stderr = self.invoke(args=args)
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertNotIn(canary, stdout + stderr)

    def test_invalid_source_id_and_binding_are_refusals(self):
        for bad in ("Upper", "1abc", "../x", "a" * 41, "has-dash"):
            args = F.argv(self.workspace, self.paths)
            args[args.index("--source-id") + 1] = bad
            code, report, stdout, _ = self.invoke(args=args)
            self.assertEqual(report["reason_codes"], [R.INVALID_SOURCE_ID])
            self.assertNotIn(bad, stdout)
        self.assertRefused(R.INVALID_SOURCE_BINDING_ID, binding="Not-Valid canary")


class SnowflakePlanTests(ScaffoldCase):
    edition = "snowflake"

    def test_snowflake_plan_uses_kit_project(self):
        code, report, _, _ = self.invoke()
        self.assertEqual(code, cli.EXIT_PLAN_ONLY)
        self.assertEqual(report["project_dir"], "kit")

    def test_snowflake_identifiers_fold_to_renderer_case(self):
        evidence = validate_inputs(
            edition="snowflake", mapping_path=str(self.paths["mapping"]), dry_run_path=str(self.paths["dry_run"]),
            run_manifest_path=str(self.paths["run_manifest"]), source_binding_id=F.BINDING_ID,
            max_age_hours=24, now=F.NOW,
        )
        plan = build_plan(evidence, F.SOURCE_ID)
        orders = next(table for table in plan.tables if table.entity == "orders")
        # dlt records Snowflake names upper-cased; unquoted Snowflake names fold losslessly.
        self.assertEqual(orders.bronze_table, "orders")
        self.assertEqual(orders.columns[0].bronze_column, "order_id")
        self.assertEqual(plan.bronze_namespace, F.NAMESPACE)


class WorkspaceTests(ScaffoldCase):
    def test_workspace_inside_starter_repo(self):
        args = F.argv(self.workspace, self.paths)
        args[args.index("--workspace") + 1] = str(F.ROOT / "editions" / "Bigquery")
        self.assertEqual(self.invoke(args=args)[1]["reason_codes"], [R.WORKSPACE_INSIDE_STARTER_REPO])

    def test_symlinked_workspace(self):
        link = self.root / "workspace_link"
        link.symlink_to(self.workspace, target_is_directory=True)
        args = F.argv(link, self.paths)
        self.assertEqual(self.invoke(args=args)[1]["reason_codes"], [R.WORKSPACE_INVALID])

    def test_missing_workspace(self):
        args = F.argv(self.root / "absent", self.paths)
        self.assertEqual(self.invoke(args=args)[1]["reason_codes"], [R.WORKSPACE_INVALID])

    def test_relocated_starter_checkout(self):
        (self.workspace / "editions" / "Bigquery").mkdir(parents=True)
        self.assertRefused(R.WORKSPACE_IS_STARTER_CHECKOUT)

    def test_wrong_or_missing_project_name(self):
        (self.project / "dbt_project.yml").write_text("name: 'reliable_pipeline'\n", "utf-8")
        self.assertRefused(R.EDITION_PROJECT_MISMATCH)
        (self.project / "dbt_project.yml").unlink()
        self.assertRefused(R.EDITION_PROJECT_MISMATCH)

    def test_wrong_edition_for_workspace(self):
        code, report, _, _ = self.invoke(edition="snowflake")
        self.assertEqual(report["reason_codes"], [R.WORKSPACE_INVALID])

    def test_missing_migration_models(self):
        (self.project / "models" / "migration").rmdir()
        self.assertRefused(R.WORKSPACE_MISSING_MIGRATION_MODELS)


class CollisionTests(ScaffoldCase):
    """Dry-run parity: every collision gives the same code in both modes and writes nothing."""

    def generated(self):
        path = self.project / "models" / "migration" / "generated"
        path.mkdir(exist_ok=True)
        return path

    def test_existing_output_directory(self):
        (self.generated() / F.SOURCE_ID).mkdir()
        (self.generated() / F.SOURCE_ID / "edited.sql").write_text("select 1\n", "utf-8")
        self.assertRefused(R.OUTPUT_EXISTS)

    def test_existing_empty_output_and_regular_file(self):
        (self.generated() / F.SOURCE_ID).mkdir()
        self.assertRefused(R.OUTPUT_EXISTS)
        (self.generated() / F.SOURCE_ID).rmdir()
        (self.generated() / F.SOURCE_ID).write_text("", "utf-8")
        self.assertRefused(R.OUTPUT_EXISTS)

    def test_symlinked_output_beats_output_exists(self):
        target = self.root / "elsewhere"
        target.mkdir()
        (self.generated() / F.SOURCE_ID).symlink_to(target, target_is_directory=True)
        self.assertRefused(R.SYMLINK_REFUSED)
        (self.generated() / F.SOURCE_ID).unlink()
        (self.generated() / F.SOURCE_ID).symlink_to(self.root / "dangling")
        self.assertRefused(R.SYMLINK_REFUSED)

    def test_symlinked_generated_parent(self):
        target = self.root / "elsewhere"
        target.mkdir()
        (self.project / "models" / "migration" / "generated").symlink_to(target, target_is_directory=True)
        self.assertRefused(R.SYMLINK_REFUSED)

    def test_fifo_is_not_regular(self):
        os.mkfifo(self.generated() / F.SOURCE_ID)
        self.assertRefused(R.NOT_REGULAR_FILE)

    def test_existing_and_symlinked_staging(self):
        staging = self.project / ".scaffold-staging"
        staging.mkdir(mode=0o700)
        (staging / F.SOURCE_ID).mkdir()
        self.assertRefused(R.STAGING_EXISTS)
        (staging / F.SOURCE_ID).rmdir()
        (staging / F.SOURCE_ID).symlink_to(self.root)
        self.assertRefused(R.SYMLINK_REFUSED)

    def test_staging_root_must_be_owner_only(self):
        staging = self.project / ".scaffold-staging"
        staging.mkdir()
        os.chmod(staging, 0o755)
        self.assertRefused(R.STAGING_ROOT_INVALID)


class PlanDataclassTests(ScaffoldCase):
    def evidence(self, paths=None):
        paths = paths or self.paths
        return validate_inputs(
            edition=self.edition, mapping_path=str(paths["mapping"]), dry_run_path=str(paths["dry_run"]),
            run_manifest_path=str(paths["run_manifest"]), source_binding_id=F.BINDING_ID,
            max_age_hours=24, now=F.NOW,
        )

    def test_plan_is_frozen_sorted_and_canonical(self):
        plan = build_plan(self.evidence(), F.SOURCE_ID)
        self.assertEqual(plan, build_plan(self.evidence(), F.SOURCE_ID))
        self.assertEqual([table.entity for table in plan.tables], sorted(table.entity for table in plan.tables))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.source_id = "other"  # type: ignore[misc]
        customers = plan.tables[0]
        self.assertEqual(customers.entity, "customers")
        self.assertEqual(customers.primary_key, "customer_id")
        self.assertEqual(customers.columns[0], ColumnPlan(
            canonical_name="customer_id", bronze_column="customer_id",
            cast="string", nullable=False, is_primary_key=True, mapped=True,
        ))
        self.assertEqual(plan.bronze_namespace, F.NAMESPACE)
        self.assertEqual([c.canonical_name for c in customers.columns], ["customer_id", "created_at", "country"])
        self.assertTrue(all(c.nullable for c in customers.columns[1:]))
        self.assertEqual(len(plan.relationships), 3)
        self.assertEqual(plan.source_name, "migration_bronze_synth_src")
        self.assertEqual(plan.provenance_columns, ("_migration_run_id", "_ingested_at_utc"))

    def test_extra_source_columns_never_reach_the_plan(self):
        plan = build_plan(self.evidence(), F.SOURCE_ID)
        names = {c.bronze_column for t in plan.tables for c in t.columns}
        self.assertNotIn(F.EXTRA_COLUMN, names)
        raw_sources = {rule["source"] for rules in F.EXAMPLE_MAPPING["tables"].values() for rule in rules}
        self.assertFalse(names & (raw_sources - {c.canonical_name for t in plan.tables for c in t.columns}))

    def test_partial_mapping_with_identically_named_column(self):
        mapping = {"version": 1, "tables": {"orders": [
            rule for rule in F.EXAMPLE_MAPPING["tables"]["orders"] if rule["target"] != "order_status"
        ]}}
        raw = {"orders": [rule["source"] for rule in mapping["tables"]["orders"]] + ["order_status"]}
        dry, manifest = F.build_evidence(mapping, raw_columns=raw)
        root = self.root / "partial"
        root.mkdir()
        plan = build_plan(self.evidence(F.write_bundle(root, mapping, dry, manifest)), F.SOURCE_ID)
        self.assertEqual([table.entity for table in plan.tables], ["orders"])
        self.assertEqual(plan.relationships, ())
        status = next(c for c in plan.tables[0].columns if c.canonical_name == "order_status")
        self.assertFalse(status.mapped)
        self.assertEqual(status.bronze_column, "order_status")


def _good_renderer(plan):
    source_id = plan["source_id"]
    files = {f"silver_{source_id}__{t['entity']}.sql": f"-- synthetic {t['entity']}\nselect 1\n" for t in plan["tables"]}
    files[f"_silver_{source_id}__models.yml"] = "version: 2\n"
    return files


class RendererHookTests(ScaffoldCase):
    def test_missing_renderer_module_is_reported_not_fatal(self):
        self.assertIsNone(resolve_renderer("bigquery"))
        self.assertFalse(self.invoke()[1]["renderer_available"])

    def test_registered_renderer_is_validated_in_memory(self):
        register_renderer("bigquery", _good_renderer)
        code, report, _, _ = self.invoke()
        self.assertEqual(code, cli.EXIT_PLAN_ONLY)
        self.assertTrue(report["renderer_available"])

    def test_renderer_contract_violations_refuse(self):
        calls = []

        def nondeterministic(plan):
            calls.append(1)
            files = _good_renderer(plan)
            files[f"_silver_{plan['source_id']}__models.yml"] = f"version: {len(calls)}\n"
            return files

        bad = {
            "missing_file": lambda plan: {k: v for k, v in list(_good_renderer(plan).items())[1:]},
            "extra_manifest": lambda plan: {**_good_renderer(plan), ".scaffold-manifest.json": "{}\n"},
            "crlf": lambda plan: {k: v.replace("\n", "\r\n") for k, v in _good_renderer(plan).items()},
            "no_newline": lambda plan: {k: v.rstrip("\n") for k, v in _good_renderer(plan).items()},
            "raises": lambda plan: (_ for _ in ()).throw(RuntimeError("boom /abs/path")),
            "not_mapping": lambda plan: ["x"],
            "nondeterministic": nondeterministic,
        }
        for label, renderer in bad.items():
            with self.subTest(label):
                register_renderer("bigquery", renderer)
                self.assertRefused(R.RENDERER_OUTPUT_INVALID)

    def test_render_plan_returns_sorted_files(self):
        evidence = validate_inputs(
            edition="bigquery", mapping_path=str(self.paths["mapping"]), dry_run_path=str(self.paths["dry_run"]),
            run_manifest_path=str(self.paths["run_manifest"]), source_binding_id=F.BINDING_ID,
            max_age_hours=24, now=F.NOW,
        )
        plan = build_plan(evidence, F.SOURCE_ID)
        rendered = render_plan(plan, _good_renderer)
        self.assertEqual(tuple(rendered), plan.renderer_files)


class RendererSeamTests(ScaffoldCase):
    """The integration seam shared with scaffold/templates/{bigquery,snowflake}.py."""

    def plan(self, edition=None):
        edition = edition or self.edition
        evidence = validate_inputs(
            edition=edition, mapping_path=str(self.paths["mapping"]), dry_run_path=str(self.paths["dry_run"]),
            run_manifest_path=str(self.paths["run_manifest"]), source_binding_id=F.BINDING_ID,
            max_age_hours=24, now=F.NOW,
        )
        return evidence, build_plan(evidence, F.SOURCE_ID)

    def test_renderer_input_is_exact_silver_plan_shape(self):
        _, plan = self.plan()
        silver = plan.renderer_input()
        self.assertEqual(set(silver), {"edition", "source_id", "bronze_namespace", "tables"})
        self.assertEqual(silver["edition"], "bigquery")
        self.assertEqual(silver["source_id"], F.SOURCE_ID)
        self.assertEqual(silver["bronze_namespace"], F.NAMESPACE)
        self.assertIsInstance(silver["tables"], list)
        for table in silver["tables"]:
            self.assertEqual(set(table), {"entity", "bronze_table", "columns"})
            self.assertIsInstance(table["columns"], list)
            for column in table["columns"]:
                self.assertEqual(set(column), {"canonical_name", "bronze_column", "cast", "nullable"})
                self.assertIsInstance(column["nullable"], bool)
        orders = next(t for t in silver["tables"] if t["entity"] == "orders")
        self.assertEqual(orders["columns"][0], {
            "canonical_name": "order_id", "bronze_column": "order_id", "cast": "string", "nullable": False,
        })
        # A fresh copy each call: a renderer cannot mutate the frozen plan.
        silver["tables"].clear()
        self.assertTrue(plan.renderer_input()["tables"])

    def test_bigquery_upper_case_bronze_names_refuse(self):
        evidence, _ = self.plan()
        orders = evidence.bronze["orders"]
        bronze = dict(evidence.bronze)
        bronze["orders"] = dataclasses.replace(orders, bronze_table="ORDERS")
        with self.assertRaises(R.Refusal) as caught:
            build_plan(dataclasses.replace(evidence, bronze=bronze), F.SOURCE_ID)
        self.assertEqual(caught.exception.code, R.UNSAFE_INVENTORY_IDENTIFIER)
        with self.assertRaises(R.Refusal) as caught:
            build_plan(dataclasses.replace(evidence, bronze_namespace="Starterkit_migration_x"), F.SOURCE_ID)
        self.assertEqual(caught.exception.code, R.UNSAFE_INVENTORY_IDENTIFIER)

    def test_render_outputs_match_planned_files_and_manifest(self):
        _, plan = self.plan()
        files = plan_module.render_outputs(plan, _good_renderer)
        self.assertEqual(tuple(files), plan.planned_files)
        manifest_path = f"{plan.output_dir}/{plan_module.MANIFEST_FILE}"
        manifest = json.loads(files[manifest_path])
        self.assertEqual(manifest["edition"], "bigquery")
        self.assertEqual(manifest["source_id"], F.SOURCE_ID)
        self.assertEqual(manifest["manifest_schema_version"], plan_module.MANIFEST_SCHEMA_VERSION)
        self.assertEqual(manifest["source_mapping_fingerprint"], plan.source_mapping_fingerprint)
        self.assertEqual(manifest["schema_confirmation_fingerprint"], plan.schema_confirmation_fingerprint)
        self.assertEqual(
            [entry["path"] for entry in manifest["files"]],
            [path for path in plan.planned_files if path != manifest_path],
        )
        import hashlib
        for entry in manifest["files"]:
            self.assertEqual(entry["sha256"], hashlib.sha256(files[entry["path"]].encode("utf-8")).hexdigest())
        text = files[manifest_path]
        self.assertNotIn(F.NAMESPACE, text)
        self.assertNotIn(str(self.root), text)
        self.assertEqual(text, plan_module.render_outputs(plan, _good_renderer)[manifest_path])

    def test_registry_names_template_entry_points(self):
        self.assertEqual(self._saved_modules, {
            "bigquery": ("scaffold.templates.bigquery", "render_bigquery_silver"),
            "snowflake": ("scaffold.templates.snowflake", "render_snowflake_silver"),
        })

    def test_installed_templates_accept_core_plans(self):
        """Runs only once the template PRs have landed; skipped otherwise."""
        plan_module.RENDERER_MODULES.update(self._saved_modules)
        ran = False
        for edition in plan_module.EDITIONS:
            renderer = resolve_renderer(edition)
            if renderer is None:
                continue
            ran = True
            with self.subTest(edition):
                root = self.root / f"seam_{edition}"
                root.mkdir()
                dry, manifest = F.build_evidence(F.default_mapping(), destination=edition)
                paths = F.write_bundle(root, F.default_mapping(), dry, manifest)
                evidence = validate_inputs(
                    edition=edition, mapping_path=str(paths["mapping"]), dry_run_path=str(paths["dry_run"]),
                    run_manifest_path=str(paths["run_manifest"]), source_binding_id=F.BINDING_ID,
                    max_age_hours=24, now=F.NOW,
                )
                plan = build_plan(evidence, F.SOURCE_ID)
                files = plan_module.render_outputs(plan, renderer)
                self.assertEqual(tuple(files), plan.planned_files)
        if not ran:
            self.skipTest("edition template renderers are not installed yet")


class InputContractTests(ScaffoldCase):
    """Admission, field-table, and binding refusals (docs/scaffolder-input-contract.md)."""

    def rebundle(self, mapping=None, dry=None, manifest=None, *, destination=None):
        base_dry, base_manifest = F.build_evidence(F.default_mapping(), destination=destination or self.edition)
        shutil.rmtree(self.root / "evidence")
        self.paths = F.write_bundle(
            self.root,
            F.default_mapping() if mapping is None else mapping,
            base_dry if dry is None else dry,
            base_manifest if manifest is None else manifest,
        )

    def mutated(self, which, mutate):
        dry, manifest = F.build_evidence(F.default_mapping(), destination=self.edition)
        mutate(dry if which == "dry" else manifest)
        self.rebundle(dry=dry, manifest=manifest)

    def test_admission_refusals(self):
        paths = dict(self.paths)
        paths.pop("mapping")
        code, report, _, _ = self.invoke(args=F.argv(self.workspace, paths))
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertEqual(report["reason_codes"], [R.INPUT_MISSING])
        os.chmod(self.paths["dry_run"], 0o644)
        self.assertRefused(R.INPUT_NOT_PRIVATE)
        self.rebundle(dry="{not json")
        self.assertRefused(R.INPUT_MALFORMED)

    def test_field_and_binding_refusals(self):
        cases = [
            ("dry", lambda r: r.update(mode="load"), R.INPUT_UNSUPPORTED_VERSION),
            ("man", lambda r: r.pop("pipeline_name"), R.FIELD_MISSING),
            ("man", lambda r: r.update(status="failed"), R.EVIDENCE_RUN_NOT_COMPLETED),
            ("man", lambda r: r.update(typed_schema_fingerprint_status="mismatched"), R.EVIDENCE_SCHEMA_NOT_MATCHED),
            ("dry", lambda r: r.update(source_schema_fingerprint="0" * 64), R.FINGERPRINT_RECOMPUTE_MISMATCH),
            ("dry", lambda r: r["inventory"][0].update(status="missing"), R.EVIDENCE_NOT_READY),
            ("dry", lambda r: r["blockers"].append("schema_drift_not_confirmed"), R.EVIDENCE_NOT_READY),
            # Readiness must consult an inventory entry for every selected table (contract note 9).
            ("dry", lambda r: r.update(inventory=[]), R.EVIDENCE_NOT_READY),
            ("dry", lambda r: r["inventory"].pop(), R.EVIDENCE_NOT_READY),
            ("dry", lambda r: r["inventory"][0].update(table=None), R.EVIDENCE_NOT_READY),
        ]
        for which, mutate, expected in cases:
            with self.subTest(expected=expected):
                self.mutated(which, mutate)
                self.assertRefused(expected)

    def test_mapping_binding_identity_and_freshness_refusals(self):
        mapping = F.default_mapping()
        mapping["tables"]["orders"] = mapping["tables"]["orders"][:-1]
        self.rebundle(mapping=mapping)
        self.assertRefused(R.MAPPING_FINGERPRINT_MISMATCH)
        self.rebundle()
        self.assertRefused(R.SOURCE_IDENTITY_UNAVAILABLE, binding=None)
        self.assertRefused(R.SOURCE_IDENTITY_MISMATCH, binding="other_alias")
        self.assertRefused(R.EVIDENCE_FRESHNESS_UNAVAILABLE, max_age=None)
        self.assertRefused(R.EVIDENCE_STALE, max_age="1")
        self.rebundle(destination="duckdb")
        self.assertRefused(R.DESTINATION_EDITION_UNBOUND)

    def test_mapping_source_rejects_unicode_line_separators(self):
        from mapping_contract import valid_mapping_source_name
        self.assertTrue(valid_mapping_source_name("Order Id"))
        for bad in ("a b", "a b", "a\nb", "a\x85b"):
            self.assertFalse(valid_mapping_source_name(bad), repr(bad))
        source = (F.MIGRATION / "mapping_contract.py").read_text("utf-8")
        self.assertNotIn(" ", source)
        self.assertNotIn(" ", source)


class IsolationTests(unittest.TestCase):
    """The scaffolder never imports dlt, dbt, warehouse clients, or reads credentials."""

    SCAFFOLD_FILES = sorted((F.MIGRATION / "scaffold").glob("*.py")) + [
        F.MIGRATION / "mapping_contract.py", F.MIGRATION / "scaffold_silver.py",
    ]

    def test_source_binding_id_pattern_matches_loader(self):
        """run_migration imports dlt, so the pattern is mirrored; read the loader's source text."""
        tree = ast.parse((F.MIGRATION / "run_migration.py").read_text("utf-8"))
        patterns = [
            node.value.args[0].value for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_SOURCE_BINDING_ID" for t in node.targets)
            and isinstance(node.value, ast.Call) and node.value.args
            and isinstance(node.value.args[0], ast.Constant)
        ]
        from scaffold.inputs import SOURCE_BINDING_ID
        self.assertEqual(patterns, [SOURCE_BINDING_ID.pattern])

    def test_cli_imports_no_profile_or_warehouse_modules(self):
        root = Path(tempfile.mkdtemp(prefix="scaffold-synthetic-"))
        self.addCleanup(shutil.rmtree, root, True)
        workspace = F.make_workspace(root)
        dry, manifest = F.build_evidence(F.default_mapping())
        paths = F.write_bundle(root, F.default_mapping(), dry, manifest)
        script = (
            "import json, runpy, sys\n"
            f"sys.argv = ['scaffold_silver.py'] + {F.argv(workspace, paths, max_age='8784')!r}\n"
            "try:\n"
            f"    runpy.run_path({str(F.MIGRATION / 'scaffold_silver.py')!r}, run_name='__main__')\n"
            "except SystemExit:\n"
            "    pass\n"
            f"bad = sorted(m for m in sys.modules if m.split('.')[0] in {FORBIDDEN_MODULES!r})\n"
            "print(json.dumps({'bad': bad, 'no_bytecode': sys.dont_write_bytecode}), file=sys.stderr)\n"
        )
        env = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}
        result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, env=env, timeout=60)
        probe = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(probe["bad"], [])
        self.assertTrue(probe["no_bytecode"])
        report = json.loads(result.stdout)
        self.assertIn(report["status"], {"plan_only", "refused"})
        self.assertNotIn("ready", report["status"])

    def test_sources_never_read_environment_or_profiles(self):
        for path in self.SCAFFOLD_FILES:
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, {"environ", "getenv", "environb"}, path.name)
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [alias.name for alias in node.names] + [getattr(node, "module", None) or ""]
                    for name in names:
                        self.assertNotIn(name.split(".")[0], FORBIDDEN_MODULES + ("dotenv", "tomllib", "yaml"), path.name)
            self.assertNotIn("profiles.yml", path.read_text("utf-8"), path.name)

    def test_shared_modules_used_are_dlt_free(self):
        for name in ("_private_json", "schema_drift", "evidence_index", "destination_type_profiles", "mapping_contract"):
            tree = ast.parse((F.MIGRATION / f"{name}.py").read_text("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [alias.name for alias in node.names] + [getattr(node, "module", None) or ""]
                    self.assertFalse(any(n.split(".")[0] in FORBIDDEN_MODULES for n in names), name)

    def test_reason_codes_are_fixed_identifiers(self):
        for code in R.ALL_CODES:
            self.assertRegex(code, r"^[a-z][a-z0-9_]*$")
        with self.assertRaises(ValueError):
            R.Refusal("not_a_code")


if __name__ == "__main__":
    unittest.main()
