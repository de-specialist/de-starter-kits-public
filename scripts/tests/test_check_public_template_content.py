"""Offline tests for the public template content guard.

Every rejected sample is assembled at runtime from fragments so this file never
contains a literal that the guard, or a secret scanner, would report.
"""

from __future__ import annotations

import contextlib
import io
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import check_public_template_content as guard  # noqa: E402


AT = "@"
BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"
KEY_HEADER = "-----BEGIN " + "PRIVATE KEY-----"
REJECTED_LINES = {
    "contact-email": [
        "owner: mock.user" + AT + "synthetic-domain.io",
        "Contact <ops+alerts" + AT + "mail.synthetic-domain.co.uk> for access",
    ],
    "contact-phone": [
        "phone: 0438 " + "291 765",
        "phone: (02) " + "9382 7461",
        "phone: 0438" + "291765",
        "phone: 1300 " + "829 746",
        "phone: +61 4" + "38 291 765",
        "call (415) 5" + "55-2671 today",
        "support,212-5" + "55-8930",
    ],
    "credential-token": [
        "aws_access_key_id = AK" + "IA" + "Q7ZD3MPL5XW2RTBN",
        "auth: gh" + "p_" + "a8Kd92LmQx71ZpRt56VbNc03HyWe48UsJf2G",
        "key=AI" + "za" + "Sy" + "D4k9Lm2Qx7Zp1Rt5Vb6Nc0Hy3We8Us4Jf",
        "bearer ey" + "JhbGciOiJIUzI1NiJ9.ey" + "JzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJlLXZhbHVl",
    ],
    "credential-assignment": [
        "password: " + "Hunter2" + "Pass99xyz",
        "secret: " + "Tr0ub4dor" + "$Horse9",
        "password: " + "q7Zd3mPl5xW2rTbN9k",
        'API_KEY="' + "a8Kd92LmQx71ZpRt56Vb" + '"',
        "client_secret = " + "f3b9c1d27a6e4085b1c9",
    ],
    "credential-url": [
        "dsn = postgresql://loader:" + "Tr0ub4dor" + "$Horse9" + AT + "localhost/db",
        "dsn = postgresql://loader:" + "ab%40cd%2Fef%3Agh%25ij" + AT + "localhost/db",
        "dsn = postgresql://loader:" + "q7Zd3mPl5x" + AT + "db.internal:5432/retail",
    ],
}
ACCEPTED_LINES = [
    # Generic documented fields and references.
    "| email | string | Customer contact email, dropped in staging |",
    "- name: phone_number",
    "      password: \"{{ env_var('SNOWFLAKE_PASSWORD') }}\"",
    "password: ${SNOWFLAKE_PASSWORD}",
    "private_key_path: ~/.ssh/snowflake_rsa_key.p8",
    "api_key = os.environ['PROVIDER_API_KEY_V2']",
    "token: <paste-your-token-here-123>",
    "password_env_var: SNOWFLAKE_PASSWORD_2",
    "password: sentinel-password-7f3a",
    "access_key: AK" + "IAIOSFODNN7EXAMPLE",
    "pattern = rb\"" + KEY_HEADER + "\",",
    # Reserved addresses, numbers and hosts.
    "email: casey" + AT + "example.com, robin" + AT + "shop.example.org, ci" + AT + "runner.test",
    "git clone git" + AT + "code-host.dev:team/starter-kit.git",
    "uses: actions/checkout" + AT + "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "phone: +1 202-555-0143 or (303) 555-0199",
    "url: postgresql://user:password" + AT + "localhost:5432/retail",
    "url: snowflake://{user}:{password}" + AT + "{account}/db",
    # Unrelated synthetic retail examples.
    "customer_id,first_name,order_total,ordered_at",
    "1001,Avery,249.90,2024-03-17T10:15:00+10:00",
    "order_id: ORD-2024-000187",
    "sku = 'SKU-48213-BLK'",
    "--hash=sha256:9f2c1d7a6e4085b1c93b9c1d27a6e4085b1c9f2c1d7a6e4085b1c93b9c1d27a6",
]


def rules(findings):
    return sorted({finding.rule for finding in findings})


class LineRuleTests(unittest.TestCase):
    def test_real_looking_contact_and_credential_material_is_rejected(self):
        for rule, lines in REJECTED_LINES.items():
            for index, line in enumerate(lines):
                with self.subTest(rule=rule, sample=index):
                    self.assertIn(rule, guard.line_rules(line))

    def test_generic_fields_reserved_values_and_retail_examples_pass(self):
        for index, line in enumerate(ACCEPTED_LINES):
            with self.subTest(sample=index):
                self.assertEqual(guard.line_rules(line), set())

    def test_long_lines_are_matched_in_overlapping_windows(self):
        secret = REJECTED_LINES["credential-token"][0]
        line = "x" * (guard.LINE_WINDOW - 10) + " " + secret + " " + "y" * guard.LINE_WINDOW
        self.assertIn("credential-token", guard.line_rules(line))


class ContentTests(unittest.TestCase):
    def test_private_key_needs_key_material(self):
        pem = KEY_HEADER + "\n" + BODY + "\n"
        escaped = '{"private_key": "' + KEY_HEADER + "\\n" + BODY + '"}'
        armored = "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----\nVersion: 1\n\n" + BODY + "\n"
        for name, text in (("pem", pem), ("escaped", escaped), ("armored", armored)):
            with self.subTest(sample=name):
                findings = guard.scan_content("docs/setup.md", text.encode())
                self.assertIn("private-key", rules(findings))
        prose = "Paste the block that starts with `" + KEY_HEADER + "`.\n"
        self.assertEqual(guard.scan_content("docs/setup.md", prose.encode()), [])

    def test_findings_report_location_without_the_matched_value(self):
        secret = "q7Zd3mPl5x" + "W2rTbN9k"
        text = "first: ok\npassword: " + secret + "\n"
        (finding,) = guard.scan_content("config/app.yml", text.encode())
        self.assertEqual((finding.path, finding.line, finding.rule), ("config/app.yml", 2, "credential-assignment"))
        self.assertNotIn(secret, finding.render())

    def test_archive_and_report_binaries_are_rejected_by_extension_and_magic(self):
        samples = {
            "exports/source.zip": b"",
            "reports/summary.PDF": b"plain text",
            "dashboards/sales.pbix": b"",
            "docs/notes.txt": b"PK\x03\x04rest",
            "docs/readme.md": b"%PDF-1.7\n",
            "data/extract.csv": b"\x1f\x8b\x08\x00",
            "data/bundle.dat": b"\x00" * 257 + b"ustar\x0000",
        }
        for name, data in samples.items():
            with self.subTest(path=name):
                self.assertEqual(rules(guard.scan_content(name, data)), ["archive-or-report-binary"])

    def test_images_pass_and_other_binaries_are_unscannable(self):
        self.assertEqual(guard.scan_content("docs/img/dag.png", b"\x89PNG\r\n\x1a\n\x00\x00"), [])
        self.assertEqual(rules(guard.scan_content("docs/img/dag.png", b"\x00\x01\x02")), ["unscannable-binary"])
        utf16 = ("mail mockuser" + AT + "synthetic-domain.io").encode("utf-16")
        self.assertEqual(rules(guard.scan_content("notes.txt", utf16)), ["unscannable-binary"])

    def test_inline_pragma_silences_only_the_named_rule_on_that_line(self):
        email = "mockuser" + AT + "synthetic-domain.io"
        phone = "+61 4" + "38 291 765"
        pragma = "  # public-template-guard: allow contact-email"
        text = f"a: {email} {phone}{pragma}\nb: {email}\n"
        found = [(f.line, f.rule) for f in guard.scan_content("docs/a.yml", text.encode())]
        self.assertEqual(found, [(1, "contact-phone"), (2, "contact-email")])


class MockFixtureTests(unittest.TestCase):
    def mock_rules(self, name, text, globs=()):
        return [(f.line, f.rule) for f in guard.scan_content(name, text.encode(), globs)]

    def test_designated_paths(self):
        self.assertTrue(guard.is_mock_fixture_path("migration/tests/fixtures/public_mock/customers.csv"))
        self.assertTrue(guard.is_mock_fixture_path("docs/Public-Mock/a/b.json"))
        self.assertTrue(guard.is_mock_fixture_path("demo/data/x.csv", ["demo/data/*"]))
        self.assertFalse(guard.is_mock_fixture_path("editions/Snowflake/kit/seeds/raw_customers.csv"))
        self.assertFalse(guard.is_mock_fixture_path("docs/public_mock"))

    def test_csv_identifier_columns_must_be_mock(self):
        text = (
            "customer_id,accountNumber,line_number,name,amount\n"
            "MOCK-C-0001,MOCK_ACC_1,1,Avery,10.50\n"
            "C-0002,MOCK_ACC_2,2,Blake,11.00\n"
            ",,3,Casey,0\n"
            "MOCK-C-0004,40021987,4,Drew,5\n"
        )
        self.assertEqual(
            self.mock_rules("fixtures/public_mock/customers.csv", text),
            [(3, "mock-identifier"), (5, "mock-identifier")],
        )

    def test_same_rows_pass_outside_designated_paths(self):
        text = "customer_id,name\n1001,Avery\nC-0002,Blake\n"
        self.assertEqual(self.mock_rules("kit/seeds/raw_customers.csv", text), [])
        self.assertEqual(
            self.mock_rules("kit/seeds/raw_customers.csv", text, ["kit/seeds/*"]),
            [(2, "mock-identifier"), (3, "mock-identifier")],
        )

    def test_json_and_jsonl_identifier_keys(self):
        good = '{"orders": [{"orderId": "MOCK-O-1", "customer_id": null, "item_ids": ["MOCK-I-1"], "qty": 2}]}'
        bad = '{"orders": [{"orderId": "MOCK-O-1", "item_ids": ["MOCK-I-1", "I-2"]}]}'
        numeric = '{"id": 48213}'
        self.assertEqual(self.mock_rules("public_mock/orders.json", good), [])
        self.assertEqual(self.mock_rules("public_mock/orders.json", bad), [(0, "mock-identifier")])
        self.assertEqual(self.mock_rules("public_mock/orders.json", numeric), [(0, "mock-identifier")])
        lines = '{"id": "MOCK-1"}\n\n{"id": "A-2"}\n'
        self.assertEqual(self.mock_rules("public_mock/orders.jsonl", lines), [(3, "mock-identifier")])

    def test_key_value_text_fixtures(self):
        text = (
            "source_key: MOCK_SRC_1\n"
            "- tenant_id: 'T-9'  # real-looking\n"
            "store_ids = [MOCK-S-1, \"S-2\"]\n"
            "parent_id:\n"
            "description: order_id is the key\n"
            "version_number: 3\n"
        )
        self.assertEqual(
            self.mock_rules("public_mock/config.yml", text),
            [(2, "mock-identifier"), (3, "mock-identifier")],
        )

    def test_unparseable_fixture_fails_closed(self):
        self.assertEqual(
            self.mock_rules("public_mock/orders.json", '{"id": '),
            [(0, "mock-fixture-unparseable")],
        )


class TreeAndCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path

    def run_main(self, *arguments):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = guard.main([str(self.root), *arguments])
        return status, stderr.getvalue()

    def test_checkout_includes_untracked_but_excludes_ignored_files(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write(".gitignore", "cache/\n")
        self.write("cache/secret.txt", REJECTED_LINES["credential-assignment"][0])
        self.write("new.txt", REJECTED_LINES["credential-assignment"][1])
        status, output = self.run_main()
        self.assertEqual(status, 1)
        self.assertIn("new.txt:1: credential-assignment", output)
        self.assertNotIn("cache/", output)
        for line in REJECTED_LINES["credential-assignment"]:
            self.assertNotIn(line.split(": ")[-1], output)

    def test_clean_tree_passes(self):
        self.write("README.md", "Contact support" + AT + "example.com.\n")
        self.write("seeds/raw_orders.csv", "order_id,customer_id\n1,1001\n")
        self.write("fixtures/public_mock/orders.csv", "order_id\nMOCK-O-1\n")
        self.assertEqual(self.run_main(), (0, ""))

    def test_findings_exit_one_and_list_each_path(self):
        self.write("docs/team.md", "Ask mockuser" + AT + "synthetic-domain.io\n")
        self.write("archive/export.zip", b"PK\x03\x04")
        self.write("fixtures/public_mock/orders.csv", "order_id\n1\n")
        self.write(".git/config", "token = " + "q7Zd3mPl5xW2rTbN9k\n")
        status, output = self.run_main()
        self.assertEqual(status, 1)
        self.assertIn("docs/team.md:1: contact-email", output)
        self.assertIn("archive/export.zip: archive-or-report-binary", output)
        self.assertIn("fixtures/public_mock/orders.csv:2: mock-identifier", output)
        self.assertNotIn(".git/config", output)
        self.assertNotIn("synthetic-domain", output)

    def test_mock_path_option_designates_more_fixtures(self):
        self.write("demo/customers.csv", "customer_id\n1001\n")
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(self.run_main("--mock-path", "demo/*")[0], 1)

    def test_allowlist_is_scoped_to_rule_and_path(self):
        self.write("data/contacts.csv", "email\nmockuser" + AT + "synthetic-domain.io\n")
        self.write("data/other.csv", "email\nsam" + AT + "synthetic-domain.io\n")
        allowlist = self.root.parent / (self.root.name + "-allow.txt")
        self.addCleanup(allowlist.unlink)
        allowlist.write_text("# reviewed: vendor sample\ncontact-email data/contacts.csv\n")
        status, output = self.run_main("--allowlist", str(allowlist))
        self.assertEqual(status, 1)
        self.assertNotIn("data/contacts.csv", output)
        self.assertIn("data/other.csv:2: contact-email", output)
        allowlist.write_text("not-a-rule data/*\n")
        self.assertEqual(self.run_main("--allowlist", str(allowlist))[0], 2)

    def test_symlinks_are_not_followed(self):
        outside = self.root.parent / (self.root.name + "-outside.txt")
        self.addCleanup(outside.unlink)
        outside.write_text("mockuser" + AT + "synthetic-domain.io\n")
        (self.root / "link.txt").symlink_to(outside)
        self.assertEqual(self.run_main()[0], 0)

    def test_scan_bounds(self):
        self.write("a.txt", "one\n")
        self.write("b.txt", "two\n")
        with self.assertRaises(guard.ScanBoundExceeded):
            guard.scan_tree(self.root, max_files=1)
        with self.assertRaises(guard.ScanBoundExceeded):
            guard.scan_tree(self.root, max_total_bytes=5)
        findings, truncated = guard.scan_tree(self.root, max_file_bytes=3)
        self.assertEqual(rules(findings), ["oversized-file"])
        self.assertFalse(truncated)
        findings, truncated = guard.scan_tree(self.root, max_file_bytes=3, max_findings=1)
        self.assertEqual((len(findings), truncated), (1, True))

    def test_missing_root_is_a_usage_error(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(guard.main([str(self.root / "absent")]), 2)

    def test_this_repository_passes(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = guard.main([
                "--mock-path", "migration/templates/**/mock/**",
                "--mock-path", "migration/templates/**/fixtures/**",
            ])
        self.assertEqual(status, 0, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
