"""Ensure Dependabot updates are monthly, grouped, and ignore only proven upstream pins."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
TERRAFORM_DIRECTORIES = {
    "/editions/Bigquery/infra/terraform",
    "/editions/Snowflake/infra/terraform",
}
# Upstream pins that make newer releases uninstallable:
# package -> (ignored range, upstream bound, blocking packages).
DBT_PINS = {
    "pathspec": (">=1.1", "pathspec<1.1", ("dbt-common", "dbt-core")),
    "mashumaro": (">=3.18", "mashumaro<3.18", ("dbt-adapters", "dbt-core")),
    "agate": (">=1.10", "agate<1.10", ("dbt-common",)),
}
EDITION_PIP_IGNORES = {
    "/editions/Bigquery": {
        "tenacity": (">=9.2", "tenacity<9.2.0", ("google-genai",)),
        "google-genai": (">=2.0", "google-genai<2.0.0", ("google-cloud-aiplatform",)),
        "websockets": (">=17.0", "websockets<17.0", ("google-genai",)),
        "google-cloud-storage": (">=3.2", "google-cloud-storage<3.2", ("dbt-bigquery",)),
        **DBT_PINS,
    },
    "/editions/Snowflake": DBT_PINS,
}


class DependabotConfigurationTests(unittest.TestCase):
    def test_every_entry_is_monthly_with_grouped_minor_and_patch_updates(self) -> None:
        content = DEPENDABOT.read_text(encoding="utf-8")
        entries = re.findall(
            r"(?ms)^  - package-ecosystem: ([^\n]+)\n(.*?)(?=^  - package-ecosystem:|\Z)",
            content,
        )
        self.assertTrue(entries)
        for ecosystem, body in entries:
            with self.subTest(ecosystem=ecosystem, body=body.splitlines()[0]):
                self.assertRegex(body, r"(?m)^      interval: monthly$")
                # Minor and patch bumps share one pull request; majors stay separate.
                self.assertRegex(
                    body,
                    r"(?m)^    groups:\n      minor-and-patch:\n        update-types: \[minor, patch\]$",
                )

    def test_both_terraform_roots_have_provider_updates(self) -> None:
        content = DEPENDABOT.read_text(encoding="utf-8")
        entries = re.findall(
            r"(?ms)^  - package-ecosystem: ([^\n]+)\n(.*?)(?=^  - package-ecosystem:|\Z)",
            content,
        )
        terraform_entries = [body for ecosystem, body in entries if ecosystem == "terraform"]
        configured = set()
        for body in terraform_entries:
            directory = re.search(r"(?m)^    directory: ([^\n]+)$", body)
            self.assertIsNotNone(directory, "Terraform update entry is missing its directory")
            assert directory is not None
            configured.add(directory.group(1))

        self.assertEqual(configured, TERRAFORM_DIRECTORIES)

    def test_edition_pip_updates_ignore_only_upstream_pinned_ranges(self) -> None:
        content = DEPENDABOT.read_text(encoding="utf-8")
        entries = re.findall(
            r"(?ms)^  - package-ecosystem: ([^\n]+)\n(.*?)(?=^  - package-ecosystem:|\Z)",
            content,
        )
        rule_pattern = re.compile(
            r"      # ([^\n]+)\n      - dependency-name: ([^\n]+)\n        versions: \[\"([^\"]+)\"\]\n"
        )
        configured_by_directory = {}
        for ecosystem, body in entries:
            directory = re.search(r"(?m)^    directory: ([^\n]+)$", body)
            self.assertIsNotNone(directory)
            assert directory is not None
            key = (ecosystem, directory.group(1))
            ignore_block = re.search(r"(?ms)^    ignore:\n(.*?)(?=^    [a-z]|^\s*$|\Z)", body)
            if ignore_block is None:
                self.assertNotIn("dependency-name:", body, key)
                continue
            block = ignore_block.group(1)
            rules = rule_pattern.findall(block)
            # The block must consist only of commented name/range pairs: no extra
            # keys such as update-types, and no uncommented rules.
            self.assertEqual(rule_pattern.sub("", block), "", key)
            self.assertEqual(body.count("ignore:"), 1, key)
            self.assertEqual(body.count("dependency-name:"), len(rules), f"unparsed ignore rule in {key}")
            names = [name for _, name, _ in rules]
            self.assertEqual(len(names), len(set(names)), f"duplicate ignore rule in {key}")
            configured_by_directory[key] = {name: (versions, comment) for comment, name, versions in rules}

        expected_keys = {("pip", directory) for directory in EDITION_PIP_IGNORES}
        self.assertEqual(set(configured_by_directory), expected_keys)
        for directory, expected in EDITION_PIP_IGNORES.items():
            with self.subTest(directory=directory):
                configured = configured_by_directory[("pip", directory)]
                self.assertEqual(
                    {name: versions for name, (versions, _) in configured.items()},
                    {name: spec[0] for name, spec in expected.items()},
                )
                lock = (ROOT / directory.lstrip("/") / "requirements-lock.txt").read_text(encoding="utf-8")
                for name, (_, bound, blockers) in expected.items():
                    comment = configured[name][1]
                    self.assertIn(bound, comment)
                    for blocker in blockers:
                        self.assertIn(blocker, comment)
                    # Only ignore packages the edition actually locks.
                    self.assertRegex(lock, rf"(?m)^{re.escape(name)}==")


if __name__ == "__main__":
    unittest.main()
