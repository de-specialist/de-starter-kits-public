"""Offline filesystem tests for migration's private JSON helpers."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "migration"))

from _private_json import create_private_json, read_private_json, update_private_json  # noqa: E402


@unittest.skipUnless(os.name == "posix", "private JSON helper requires POSIX ownership and no-follow flags")
class PrivateJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def test_create_read_update_and_owner_only_mode(self) -> None:
        create_private_json(self.directory, "record.json", {"status": "created", "count": 2})
        path = self.directory / "record.json"
        self.assertEqual(read_private_json(self.directory, path.name), {"status": "created", "count": 2})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

        original_inode = path.stat().st_ino
        update_private_json(self.directory, path.name, {"status": "updated", "count": 3})
        self.assertEqual(read_private_json(self.directory, path.name), {"status": "updated", "count": 3})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(path.stat().st_ino, original_inode)

    def test_create_is_exclusive_and_preserves_existing_content(self) -> None:
        create_private_json(self.directory, "record.json", {"value": "original"})

        with self.assertRaises(FileExistsError):
            create_private_json(self.directory, "record.json", {"value": "replacement"})

        self.assertEqual(read_private_json(self.directory, "record.json"), {"value": "original"})

    def test_hard_linked_files_are_rejected_without_changing_contents(self) -> None:
        path = self.directory / "record.json"
        create_private_json(self.directory, path.name, {"value": "original"})
        linked_path = self.directory / "linked-record.json"
        os.link(path, linked_path)
        original_contents = path.read_bytes()

        with self.assertRaisesRegex(ValueError, "single-link"):
            read_private_json(self.directory, path.name)
        with self.assertRaisesRegex(ValueError, "single-link"):
            update_private_json(self.directory, path.name, {"value": "replacement"})

        self.assertEqual(path.read_bytes(), original_contents)
        self.assertEqual(linked_path.read_bytes(), original_contents)

    def test_unsafe_file_and_directory_modes_are_rejected(self) -> None:
        path = self.directory / "record.json"
        path.write_text('{"value":"private"}\n', encoding="utf-8")
        path.chmod(0o644)
        original_contents = path.read_bytes()

        with self.assertRaisesRegex(ValueError, "owner-only"):
            read_private_json(self.directory, path.name)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            update_private_json(self.directory, path.name, {"value": "replacement"})
        self.assertEqual(path.read_bytes(), original_contents)

        path.chmod(0o600)
        self.directory.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            read_private_json(self.directory, path.name)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            create_private_json(self.directory, "new.json", {"value": "blocked"})
        self.directory.chmod(0o700)

    def test_duplicate_keys_and_invalid_json_are_rejected(self) -> None:
        duplicate = self.directory / "duplicate.json"
        duplicate.write_text('{"status":"first","status":"second"}\n', encoding="utf-8")
        duplicate.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "duplicate keys"):
            read_private_json(self.directory, duplicate.name)

        invalid = self.directory / "invalid.json"
        invalid.write_text("{not-json\n", encoding="utf-8")
        invalid.chmod(0o600)
        with self.assertRaises(ValueError):
            read_private_json(self.directory, invalid.name)

    def test_symlinks_are_rejected_for_read_and_update(self) -> None:
        target = self.directory / "target.json"
        create_private_json(self.directory, target.name, {"value": "safe"})
        link = self.directory / "link.json"
        link.symlink_to(target.name)

        with self.assertRaises(OSError):
            read_private_json(self.directory, link.name)
        with self.assertRaises(OSError):
            update_private_json(self.directory, link.name, {"value": "unsafe"})
        self.assertEqual(read_private_json(self.directory, target.name), {"value": "safe"})

    def test_fifo_is_rejected_without_blocking_read_or_update(self) -> None:
        fifo = self.directory / "record.json"
        os.mkfifo(fifo, 0o600)
        helper_code = "\n".join((
            "import sys",
            "from pathlib import Path",
            "from _private_json import read_private_json, update_private_json",
            "directory = Path(sys.argv[1])",
            "operation = sys.argv[2]",
            "try:",
            "    if operation == 'read':",
            "        read_private_json(directory, 'record.json')",
            "    else:",
            "        update_private_json(directory, 'record.json', {'x': 1})",
            "except ValueError:",
            "    sys.exit(0)",
            "sys.exit(2)",
        ))
        environment = os.environ.copy()
        current_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(REPOSITORY_ROOT / "migration"), current_pythonpath) if part
        )
        for operation in ("read", "update"):
            with self.subTest(operation=operation):
                result = subprocess.run(
                    [sys.executable, "-c", helper_code, str(self.directory), operation],
                    capture_output=True,
                    text=True,
                    env=environment,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_size_limit_is_enforced(self) -> None:
        path = self.directory / "large.json"
        path.write_text('{"payload":"large"}\n', encoding="utf-8")
        path.chmod(0o600)

        with self.assertRaisesRegex(ValueError, "size limit"):
            read_private_json(self.directory, path.name, max_bytes=8)


if __name__ == "__main__":
    unittest.main()
