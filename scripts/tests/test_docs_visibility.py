"""Offline tests for the docs-site private page rules, using synthetic fixtures."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
SCRIPT = SCRIPTS / "docs_visibility.py"
SPEC = importlib.util.spec_from_file_location("docs_visibility", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
docs_visibility = importlib.util.module_from_spec(SPEC)
sys.modules["docs_visibility"] = docs_visibility
SPEC.loader.exec_module(docs_visibility)

SEEDED_PAGES = {
    "docs/product-backlog.md",
    "docs/customer-validation.md",
    "docs/product-discovery-kit.md",
    "docs/landing-and-acquisition-experiment.md",
    "docs/support-economics-tracker.md",
    "docs/delivery-path-decision.md",
    "docs/ai-feature-gate.md",
}

CONFIG = textwrap.dedent(
    """
    private_pages = ["docs/listed-plan.md"]
    private_markers = ["Synthetic internal marker"]
    """
)


class FixtureRepo:
    """A throwaway repository tree with public and private synthetic pages."""

    def __init__(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.write("website/docs-visibility.toml", CONFIG)
        self.write("README.md", "# Home\n")
        self.write("docs/public-guide.md", "# Public guide\n")
        self.write("docs/listed-plan.md", "# Listed plan\n")
        self.write("docs/flagged-visibility.md", "---\ntitle: Plan\nvisibility: private\n---\n# Plan\n")
        self.write("docs/flagged-internal.md", "---\ninternal: true  # never publish\n---\n# Draft\n")
        self.write("docs/decisions/0002-private.md", "---\nvisibility: 'Internal'\n---\n# Decision\n")
        self.write("docs/public-with-meta.md", "---\nvisibility: public\ninternal: false\n---\n# Meta\n")
        self.write("docs/unterminated.md", "---\nvisibility: private\n# Never closed, so not front matter\n")
        self.write("website/content/docs/stale.md", "---\ninternal: true\n---\n")
        self.write("node_modules/pkg/README.md", "---\ninternal: true\n---\n")

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def close(self) -> None:
        self._temp.cleanup()


class FrontMatterTests(unittest.TestCase):
    def test_private_keys_are_recognised(self) -> None:
        cases = {
            "---\nvisibility: private\n---\n": True,
            "---\nVisibility: \"PRIVATE\"\n---\n": True,
            "---\nvisibility: internal\n---\n": True,
            "---\ninternal: true\n---\n": True,
            "---\ninternal: yes\n...\n": True,
            "﻿---\ninternal: true\n---\n": True,
            "---\nvisibility: unlisted\n---\n": True,
            "---\nvisibility: >-\n  private\n---\n": True,
            "---\ninternal: maybe\n---\n": True,
            "---\nvisibility: public\n---\n": False,
            "---\ninternal: false\n---\n": False,
            "---\nnested:\n  internal: true\n---\n": False,
            "# Title\n\nvisibility: private\n": False,
            "---\ninternal: true\n": False,
            "": False,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertIs(docs_visibility.is_private_front_matter(text), expected)

    def test_site_candidates_follow_mkdocs_urls(self) -> None:
        self.assertEqual(
            docs_visibility.site_candidates("docs/plan.md"),
            ["docs/plan/index.html", "docs/plan.html"],
        )
        self.assertEqual(docs_visibility.site_candidates("editions/X/README.md"), ["editions/X/index.html"])
        self.assertEqual(docs_visibility.staged_name("README.md"), "index.md")


class ConfigTests(unittest.TestCase):
    def load(self, text: str):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.toml"
            path.write_text(text, encoding="utf-8")
            return docs_visibility.load_config(path)

    def test_rejects_unsafe_or_malformed_entries(self) -> None:
        for text in (
            'private_pages = ["../outside.md"]',
            'private_pages = ["/abs/page.md"]',
            'private_pages = ["docs/page.html"]',
            'private_pages = [""]',
            'private_pages = "docs/page.md"',
            'private_markers = [""]',
            'private_page = ["docs/typo.md"]',
            "not toml = [",
        ):
            with self.subTest(text=text), self.assertRaises(docs_visibility.ConfigError):
                self.load(text)

    def test_missing_config_is_an_error(self) -> None:
        with self.assertRaises(docs_visibility.ConfigError):
            docs_visibility.load_config(Path("/nonexistent/docs-visibility.toml"))

    def test_public_snapshot_contains_no_private_pages(self) -> None:
        config = docs_visibility.load_config(REPO / "website" / "docs-visibility.toml")
        self.assertFalse(config.private_pages)
        self.assertTrue(config.private_markers)
        for page in (REPO / "docs").rglob("*.md"):
            self.assertFalse(docs_visibility.is_private_front_matter(page.read_text()), str(page))


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = FixtureRepo()
        self.addCleanup(self.repo.close)

    def test_list_private_combines_denylist_and_front_matter(self) -> None:
        result = self.repo.run("list-private")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.split(),
            [
                "docs/decisions/0002-private.md",
                "docs/flagged-internal.md",
                "docs/flagged-visibility.md",
                "docs/listed-plan.md",
            ],
        )

    def test_missing_denylist_entry_is_an_error(self) -> None:
        # A renamed listed page must not silently become public.
        (self.repo.root / "docs/listed-plan.md").rename(self.repo.root / "docs/renamed-plan.md")
        for command in (["list-private"], ["check-site", str(self.repo.root)]):
            with self.subTest(command=command[0]):
                result = self.repo.run(*command)
                self.assertEqual(result.returncode, 2)
                self.assertIn("docs/listed-plan.md", result.stderr)

    def test_check_staged_passes_for_public_pages_only(self) -> None:
        content = self.repo.root / "staged"
        (content / "docs").mkdir(parents=True)
        (content / "index.md").write_text("# Home\n")
        (content / "docs/public-guide.md").write_text("# Public guide\n")
        result = self.repo.run("check-staged", str(content))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_check_staged_fails_for_listed_and_flagged_pages(self) -> None:
        content = self.repo.root / "staged"
        (content / "docs").mkdir(parents=True)
        (content / "docs/listed-plan.md").write_text("# Listed plan\n")
        (content / "docs/renamed.md").write_text("---\nvisibility: private\n---\n")
        result = self.repo.run("check-staged", str(content))
        self.assertEqual(result.returncode, 1)
        self.assertIn("private page staged: docs/listed-plan.md", result.stderr)
        self.assertIn("private front matter staged: docs/renamed.md", result.stderr)

    def build_site(self) -> Path:
        site = self.repo.root / "built"
        (site / "docs/public-guide").mkdir(parents=True)
        (site / "index.html").write_text("<p>Home</p>")
        (site / "docs/public-guide/index.html").write_text("<p>Public</p>")
        return site

    def test_check_site_passes_for_clean_tree(self) -> None:
        result = self.repo.run("check-site", str(self.build_site()))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)

    def test_check_site_detects_each_leak_shape(self) -> None:
        site = self.build_site()
        (site / "docs/listed-plan").mkdir()
        (site / "docs/listed-plan/index.html").write_text("<p>plan</p>")
        (site / "docs/flagged-internal.html").write_text("<p>draft</p>")
        (site / "docs/decisions/0002-private").mkdir(parents=True)
        (site / "docs/decisions/0002-private/index.html").write_text("<p>decision</p>")
        (site / "search").mkdir()
        (site / "search/search_index.json").write_text('{"text": "Synthetic internal marker"}')
        result = self.repo.run("check-site", str(site))
        self.assertEqual(result.returncode, 1)
        for expected in (
            "docs/listed-plan/index.html",
            "docs/flagged-internal.html",
            "docs/decisions/0002-private/index.html",
            "found in search/search_index.json",
        ):
            self.assertIn(expected, result.stderr)

    def test_check_site_missing_directory_is_an_error(self) -> None:
        result = self.repo.run("check-site", str(self.repo.root / "absent"))
        self.assertEqual(result.returncode, 2)

    def test_invalid_config_is_an_error(self) -> None:
        self.repo.write("website/docs-visibility.toml", 'private_pages = ["../escape.md"]')
        result = self.repo.run("list-private")
        self.assertEqual(result.returncode, 2)
        self.assertIn("relative .md path", result.stderr)


@unittest.skipUnless(sys.platform.startswith("linux"), "build script uses GNU sed -i")
class BuildScriptStagingTests(unittest.TestCase):
    """Run the real staging step against a synthetic repository tree."""

    PUBLIC = {
        "README.md": "# Home\n",
        "CHANGELOG.md": "# Changelog\n",
        "CONTRIBUTING.md": "# Contributing\n",
        "SUPPORT.md": "# Support\n",
        "SECURITY.md": "# Security\n",
        "LICENSE": "Synthetic licence\n",
        "NOTICE": "Synthetic notice\n",
        "docs/guide.md": "# Guide\n",
        "docs/decisions/0001-public.md": "# Decision\n",
        "migration/README.md": "# Migration\n",
        "migration/mappings/map.md": "# Map\n",
        "editions/Bigquery/README.md": "# BigQuery\n",
        "editions/Bigquery/docs/setup.md": "# Setup\n",
        "editions/Snowflake/README.md": "# Snowflake\n",
        "editions/Snowflake/docs/setup.md": "# Setup\n",
    }
    PRIVATE = {
        "docs/listed-plan.md": "# Listed\n",
        "docs/flagged.md": "---\nvisibility: private\n---\n# Flagged\n",
        "docs/decisions/0002-private.md": "---\ninternal: true\n---\n# Private decision\n",
        "migration/mappings/private-map.md": "---\ninternal: true\n---\n",
        "editions/Bigquery/docs/internal.md": "---\nvisibility: private\n---\n",
        "editions/Snowflake/docs/listed.md": "# Listed\n",
    }

    def test_private_pages_are_never_staged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "scripts").mkdir()
            for name in ("build_docs_site.sh", "docs_visibility.py"):
                (root / "scripts" / name).write_bytes((SCRIPTS / name).read_bytes())
            config = 'private_pages = ["docs/listed-plan.md", "editions/Snowflake/docs/listed.md"]\n'
            for rel, text in {**self.PUBLIC, **self.PRIVATE, "website/docs-visibility.toml": config}.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text, encoding="utf-8")
            (root / "migration/mappings/map.json").write_text("{}", encoding="utf-8")

            result = subprocess.run(
                ["bash", str(root / "scripts" / "build_docs_site.sh")],
                env={"PATH": os.environ.get("PATH", ""), "PYTHON_BIN": sys.executable, "DOCS_STAGE_ONLY": "1"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            content = root / "website" / "content"
            staged = {p.relative_to(content).as_posix() for p in content.rglob("*") if p.is_file()}
            for rel in self.PRIVATE:
                self.assertNotIn(rel, staged)
            expected = {"index.md" if rel == "README.md" else rel for rel in self.PUBLIC if rel not in ("LICENSE", "NOTICE")}
            self.assertTrue(expected <= staged, sorted(expected - staged))
            self.assertIn("migration/mappings/map.json", staged)
            self.assertIn("check-staged", result.stdout)

    def test_staging_fails_when_a_listed_page_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "scripts").mkdir()
            for name in ("build_docs_site.sh", "docs_visibility.py"):
                (root / "scripts" / name).write_bytes((SCRIPTS / name).read_bytes())
            (root / "website").mkdir()
            (root / "website/docs-visibility.toml").write_text('private_pages = ["docs/gone.md"]\n')
            (root / "README.md").write_text("# Home\n")
            result = subprocess.run(
                ["bash", str(root / "scripts" / "build_docs_site.sh")],
                env={"PATH": os.environ.get("PATH", ""), "PYTHON_BIN": sys.executable, "DOCS_STAGE_ONLY": "1"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "website/content/index.md").exists())


class BuildScriptWiringTests(unittest.TestCase):
    def test_build_script_skips_private_pages_and_checks_staging(self) -> None:
        script = (REPO / "scripts" / "build_docs_site.sh").read_text(encoding="utf-8")
        self.assertIn("docs_visibility.py\" --root \"${ROOT}\" list-private", script)
        self.assertIn("check-staged \"${CONTENT}\"", script)
        # Markdown must go through stage_md, never a bare copy that bypasses the filter.
        self.assertNotRegex(script, r"-name '\*\.md'[^\n]*-exec cp")
        self.assertNotRegex(script, r"\bcp \"\$\{ROOT\}/[^\"]*\.md\"")

    def test_docs_workflow_keeps_site_backstop(self) -> None:
        workflow = (REPO / ".github" / "workflows" / "docs-site.yml").read_text(encoding="utf-8")
        self.assertIn("python scripts/docs_visibility.py check-site website/site", workflow)


if __name__ == "__main__":
    unittest.main()
