"""Offline tests for the built-site internal link checker."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CHECKER = Path(__file__).resolve().parents[1] / "check_site_links.py"


class CheckSiteLinksTests(unittest.TestCase):
    def build_site(self, root: Path, home_body: str) -> Path:
        site = root / "site"
        (site / "docs" / "faq").mkdir(parents=True)
        (site / "editions" / "Bigquery" / "dbt-docs").mkdir(parents=True)
        (site / "assets").mkdir()
        (site / "assets" / "app.js").write_text("")
        (site / "docs" / "faq" / "index.html").write_text('<h2 id="licence">Licence</h2>')
        # dbt docs pages use client-side routes and are not scanned.
        (site / "editions" / "Bigquery" / "dbt-docs" / "index.html").write_text(
            '<a href="missing-route.html">route</a>'
        )
        (site / "index.html").write_text(home_body)
        return site

    def run_checker(self, home_body: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            site = self.build_site(Path(temp_dir), home_body)
            return subprocess.run(
                [sys.executable, str(CHECKER), str(site)],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_valid_internal_and_external_links_pass(self) -> None:
        result = self.run_checker(
            '<a href="docs/faq/">faq</a>'
            '<a href="docs/faq/#licence">anchor</a>'
            '<a href="/docs/faq/index.html">root</a>'
            '<script src="assets/app.js"></script>'
            '<iframe src="editions/Bigquery/dbt-docs/index.html"></iframe>'
            '<h1 id="top">Home</h1><a href="#top">same page</a>'
            '<a href="#__drawer">theme toggle</a>'
            '<a href="https://example.com/missing">external</a>'
            '<a href="mailto:someone@example.com">mail</a>'
            '<a href="//cdn.example.com/x.js">protocol relative</a>'
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_file_fails(self) -> None:
        result = self.run_checker('<iframe src="dbt-docs/index.html"></iframe>')
        self.assertEqual(result.returncode, 1)
        self.assertIn("broken link 'dbt-docs/index.html'", result.stderr)

    def test_missing_anchor_on_other_page_fails(self) -> None:
        result = self.run_checker('<a href="docs/faq/#no-such-anchor">x</a>')
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing anchor", result.stderr)

    def test_missing_same_page_anchor_fails(self) -> None:
        result = self.run_checker('<a href="#missing-same-page">x</a>')
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing anchor '#missing-same-page'", result.stderr)

    def test_link_escaping_site_root_fails(self) -> None:
        result = self.run_checker('<a href="../outside.html">x</a>')
        self.assertEqual(result.returncode, 1)
        self.assertIn("broken link", result.stderr)

    def test_directory_without_index_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [sys.executable, str(CHECKER), temp_dir],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
