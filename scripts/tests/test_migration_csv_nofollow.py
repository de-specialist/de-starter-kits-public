"""Offline checks that CSV readers never follow a replaced symlink."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "migration"))
migration = importlib.import_module("migration.run_migration")


class CsvNoFollowTests(unittest.TestCase):
    def test_symlink_is_rejected_without_reading_external_csv(self):
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("platform has no O_NOFOLLOW; production opener fails closed")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "outside.csv"
            outside.write_text("id\nsecret-marker\n", encoding="utf-8")
            link = root / "input.csv"
            link.symlink_to(outside)

            with self.assertRaises(OSError):
                with migration.open_csv_input(link) as stream:
                    self.fail(f"unexpectedly opened symlink content: {stream.read()}")
            with self.assertRaises(OSError):
                migration.count_csv_rows(link)

            with mock.patch.object(migration, "discover_csv_files", return_value=[link]):
                with self.assertRaises(OSError):
                    migration.inventory_csv_inputs(root, "*.csv", None)

            self.assertEqual(outside.read_text(encoding="utf-8"), "id\nsecret-marker\n")

    def test_missing_o_nofollow_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.csv"
            path.write_text("id\n1\n", encoding="utf-8")
            without_nofollow = SimpleNamespace(
                O_RDONLY=os.O_RDONLY,
                O_CLOEXEC=getattr(os, "O_CLOEXEC", 0),
                O_NONBLOCK=getattr(os, "O_NONBLOCK", 0),
                open=os.open,
                fdopen=os.fdopen,
                fstat=os.fstat,
                close=os.close,
            )
            with mock.patch.object(migration, "os", without_nofollow):
                with self.assertRaisesRegex(OSError, "unavailable"):
                    with migration.open_csv_input(path):
                        self.fail("CSV opened without O_NOFOLLOW")


if __name__ == "__main__":
    unittest.main()
