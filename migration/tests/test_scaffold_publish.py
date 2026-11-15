"""Offline tests for the Silver scaffolder write mode (scaffold/publisher.py).

All fixtures are SYNTHETIC and live in temporary directories outside the repository. Nothing
here contacts a source, warehouse, or network, and no credentials or profiles are read.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scaffold_fixtures as F  # noqa: E402
from test_scaffold_core import ScaffoldCase  # noqa: E402

from scaffold import cli, plan as plan_module, publisher, reasons as R  # noqa: E402

OUTPUT = Path("models") / "migration" / "generated" / F.SOURCE_ID
STAGING_ROOT = ".scaffold-staging"
EDITION_MARKERS = {
    "bigquery": ("bigquery-silver-v1", "cast(`", "::varchar"),
    "snowflake": ("snowflake-silver-v1", "::varchar", "cast(`"),
}


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


class PublishCase(ScaffoldCase):
    def setUp(self):
        super().setUp()
        # The core base class hides the edition renderers; write mode needs the real ones.
        plan_module.RENDERER_MODULES.update(self._saved_modules)
        self.output = self.project / OUTPUT
        self.staging_root = self.project / STAGING_ROOT
        self.stage = self.staging_root / F.SOURCE_ID
        # Rendered output from an independent in-memory pass, taken while the workspace is clean.
        self.expected = self.expected_files()

    def generate(self, workspace=None, paths=None, edition=None):
        args = F.argv(workspace or self.workspace, paths or self.paths, edition=edition or self.edition, dry_run=False)
        code, stdout, stderr = cli.run(args, now=F.NOW)
        self.assertEqual(stderr, "")
        self.assertNotIn(str(self.root), stdout)
        self.assertNotIn(tempfile.gettempdir(), stdout)
        return code, json.loads(stdout)

    def assertNotPublished(self, report, expected, *, staging_retained):
        self.assertEqual(report["status"], "refused")
        self.assertEqual(report["reason_codes"], [expected])
        self.assertEqual(report["planned_files"], [])
        self.assertEqual(report["staging_retained"], staging_retained)
        self.assertFalse(self.output.exists() or self.output.is_symlink())

    def expected_files(self) -> dict[str, bytes]:
        args = cli.build_parser().parse_args(F.argv(self.workspace, self.paths, edition=self.edition))
        _, _, files = cli.plan_from_args(args, now=F.NOW)
        return {path.rsplit("/", 1)[1]: text.encode("utf-8") for path, text in files.items()}


class PublishSuccessTests(PublishCase):
    def test_publishes_complete_edition_output(self):
        expected = self.expected
        code, report = self.generate()
        self.assertEqual(code, cli.EXIT_PUBLISHED)
        self.assertEqual(report["status"], "published")
        self.assertEqual(report["mode"], "generate")
        self.assertEqual(report["reason_codes"], [])
        self.assertTrue(report["writes_performed"])
        self.assertFalse(report["staging_retained"])
        self.assertEqual(report["edition"], self.edition)
        self.assertEqual(report["tables"], ["customers", "order_items", "orders", "products"])
        published = {path.name: path.read_bytes() for path in self.output.iterdir()}
        self.assertEqual(published, expected)
        self.assertEqual(
            sorted(f"{OUTPUT.as_posix()}/{name}" for name in published), report["planned_files"]
        )
        manifest = json.loads(published[plan_module.MANIFEST_FILE])
        self.assertEqual(manifest["edition"], self.edition)
        for entry in manifest["files"]:
            name = entry["path"].rsplit("/", 1)[1]
            self.assertEqual(entry["sha256"], hashlib.sha256(published[name]).hexdigest())
        template_id, present, absent = EDITION_MARKERS[self.edition]
        orders = published[f"silver_{F.SOURCE_ID}__orders.sql"].decode("utf-8")
        self.assertIn(template_id, orders)
        self.assertIn(present, orders)
        self.assertNotIn(absent, orders)
        self.assertNotIn(str(self.root), b"".join(published.values()).decode("utf-8"))
        # Nothing but the staging root, now empty, remains outside the output directory.
        self.assertEqual(list(self.staging_root.iterdir()), [])

    def test_published_modes_are_editable_and_ignore_umask(self):
        previous = os.umask(0o027)
        self.addCleanup(os.umask, previous)
        code, _ = self.generate()
        self.assertEqual(code, cli.EXIT_PUBLISHED)
        self.assertEqual(mode_of(self.output), 0o700)
        self.assertEqual(mode_of(self.output.parent), 0o700)
        self.assertEqual(mode_of(self.staging_root), 0o700)
        files = list(self.output.iterdir())
        self.assertEqual(len(files), 6)
        for path in files:
            self.assertTrue(path.is_file() and not path.is_symlink())
            self.assertEqual(mode_of(path), 0o600, path.name)
            self.assertEqual(path.lstat().st_nlink, 1)
        with open(files[0], "ab") as stream:  # operator edits work without a chmod
            stream.write(b"-- operator edit\n")

    def test_stage_is_read_only_files_in_a_writable_directory_until_rename(self):
        seen = {}
        real = publisher.resolve_noreplace_rename()

        def observing(source_fd, source_name, target_fd, target_name):
            seen["dir"] = mode_of(self.stage)
            seen["files"] = {mode_of(path) for path in self.stage.iterdir()}
            seen["output_absent"] = not self.output.exists()
            real(source_fd, source_name, target_fd, target_name)

        with mock.patch.object(publisher, "resolve_noreplace_rename", return_value=observing):
            code, _ = self.generate()
        self.assertEqual(code, cli.EXIT_PUBLISHED)
        self.assertEqual(seen, {"dir": 0o700, "files": {0o400}, "output_absent": True})

    def test_generation_is_byte_identical_in_fresh_workspaces(self):
        self.assertEqual(self.generate()[0], cli.EXIT_PUBLISHED)
        other_root = Path(tempfile.mkdtemp(prefix="scaffold-synthetic-"))
        self.addCleanup(__import__("shutil").rmtree, other_root, True)
        other_workspace = F.make_workspace(other_root, self.edition)
        dry, manifest = F.build_evidence(F.default_mapping(), destination=self.edition)
        other_paths = F.write_bundle(other_root, F.default_mapping(), dry, manifest)
        code, report = self.generate(other_workspace, other_paths)
        self.assertEqual(code, cli.EXIT_PUBLISHED)
        first = F.tree_snapshot(self.project / "models")
        second = F.tree_snapshot(F.project_dir(other_workspace, self.edition) / "models")
        self.assertEqual(first, second)
        self.assertEqual(len([key for key, value in first.items() if value[0] == "file"]), 6)

    def test_existing_generated_parent_and_sibling_are_preserved(self):
        sibling = self.output.parent / "other_source"
        sibling.mkdir(parents=True)
        (sibling / "edited.sql").write_text("select 1\n", "utf-8")
        os.chmod(self.output.parent, 0o755)
        before = F.tree_snapshot(sibling)
        self.assertEqual(self.generate()[0], cli.EXIT_PUBLISHED)
        self.assertEqual(before, F.tree_snapshot(sibling))
        self.assertEqual(mode_of(self.output.parent), 0o755)

    def test_rerun_never_overwrites_published_or_edited_files(self):
        self.assertEqual(self.generate()[0], cli.EXIT_PUBLISHED)
        edited = self.output / f"silver_{F.SOURCE_ID}__orders.sql"
        edited.write_text("-- operator edit\nselect 1\n", "utf-8")
        (self.output / "operator_added.sql").write_text("select 2\n", "utf-8")
        (self.output / f"silver_{F.SOURCE_ID}__products.sql").unlink()
        before = F.tree_snapshot(self.root)
        for _ in range(2):
            code, report = self.generate()
            self.assertEqual(code, cli.EXIT_REFUSED)
            self.assertEqual(report["reason_codes"], [R.OUTPUT_EXISTS])
            self.assertFalse(report["writes_performed"])
            self.assertEqual(before, F.tree_snapshot(self.root))
        self.assertRefused(R.OUTPUT_EXISTS)

    def test_dry_run_is_read_only_before_and_after_publication(self):
        code, report, first, _ = self.invoke()  # invoke() asserts the tree is unchanged
        self.assertEqual(code, cli.EXIT_PLAN_ONLY)
        self.assertEqual(report["status"], "plan_only")
        self.assertTrue(report["renderer_available"])
        self.assertFalse(report["writes_performed"])
        self.assertFalse(report["staging_retained"])
        self.assertEqual(first, self.invoke()[2])
        self.assertFalse(self.output.parent.exists())
        self.assertFalse(self.staging_root.exists())
        self.assertEqual(self.generate()[0], cli.EXIT_PUBLISHED)
        code, report, _, _ = self.invoke()
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertEqual(report["reason_codes"], [R.OUTPUT_EXISTS])


class SnowflakePublishSuccessTests(PublishSuccessTests):
    edition = "snowflake"


class PublishRefusalTests(PublishCase):
    """Fail closed: existing paths, symlinks, and stale staging are never touched."""

    def test_existing_output_is_preserved(self):
        self.output.mkdir(parents=True)
        (self.output / "edited.sql").write_text("select 1\n", "utf-8")
        self.assertRefused(R.OUTPUT_EXISTS)
        self.output.joinpath("edited.sql").unlink()
        self.assertRefused(R.OUTPUT_EXISTS)  # an empty directory still blocks
        self.output.rmdir()
        self.output.write_text("", "utf-8")
        self.assertRefused(R.OUTPUT_EXISTS)

    def test_symlinks_are_refused_without_writing_through_them(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.output.parent.mkdir()
        self.output.symlink_to(elsewhere, target_is_directory=True)
        self.assertRefused(R.SYMLINK_REFUSED)
        self.output.unlink()
        self.output.symlink_to(self.root / "dangling")
        self.assertRefused(R.SYMLINK_REFUSED)
        self.output.unlink()
        self.output.parent.rmdir()
        self.output.parent.symlink_to(elsewhere, target_is_directory=True)
        self.assertRefused(R.SYMLINK_REFUSED)
        self.output.parent.unlink()
        self.staging_root.symlink_to(elsewhere, target_is_directory=True)
        self.assertRefused(R.SYMLINK_REFUSED)
        self.staging_root.unlink()
        self.staging_root.mkdir(mode=0o700)
        self.stage.symlink_to(elsewhere, target_is_directory=True)
        self.assertRefused(R.SYMLINK_REFUSED)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_stale_staging_is_never_reused_or_removed(self):
        self.staging_root.mkdir(mode=0o700)
        self.stage.mkdir()
        (self.stage / "leftover.sql").write_text("select 0\n", "utf-8")
        self.assertRefused(R.STAGING_EXISTS)
        self.assertEqual([path.name for path in self.stage.iterdir()], ["leftover.sql"])

    def test_non_private_staging_root_is_refused(self):
        self.staging_root.mkdir()
        os.chmod(self.staging_root, 0o750)
        self.assertRefused(R.STAGING_ROOT_INVALID)

    def test_publisher_rechecks_paths_itself(self):
        """Direct calls skip the CLI preflight, as a path swapped after it would."""
        files = {"model.sql": "select 1\n"}
        project = str(self.project)

        def attempt():
            before = F.tree_snapshot(self.root)
            outcome = publisher.publish(project, F.SOURCE_ID, files)
            self.assertEqual(before, F.tree_snapshot(self.root))
            self.assertFalse(outcome.published or outcome.staging_retained or outcome.touched)
            return outcome.code

        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.output.parent.mkdir()
        self.output.symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(attempt(), R.SYMLINK_REFUSED)
        self.output.unlink()
        self.output.mkdir()
        self.assertEqual(attempt(), R.OUTPUT_EXISTS)
        self.output.rmdir()
        os.mkfifo(self.output)
        self.assertEqual(attempt(), R.NOT_REGULAR_FILE)
        self.output.unlink()
        self.staging_root.mkdir()
        os.chmod(self.staging_root, 0o755)
        self.assertEqual(attempt(), R.STAGING_ROOT_INVALID)
        os.chmod(self.staging_root, 0o700)
        self.stage.mkdir()
        self.assertEqual(attempt(), R.STAGING_EXISTS)
        self.stage.rmdir()
        self.stage.symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(attempt(), R.SYMLINK_REFUSED)
        self.stage.unlink()
        migration = self.project / "models" / "migration"
        moved = self.root / "moved_migration"
        migration.rename(moved)
        migration.symlink_to(moved, target_is_directory=True)
        self.assertEqual(attempt(), R.SYMLINK_REFUSED)
        for bad in ({}, {"../x.sql": "select 1\n"}, {"a/b.sql": "select 1\n"}, {"..": "x\n"}, {"x.sql": b"x"}):
            self.assertEqual(publisher.publish(project, F.SOURCE_ID, bad).code, R.PUBLISH_FAILED)
        self.assertEqual(publisher.publish(project, "../escape", files).code, R.PUBLISH_FAILED)


class PublishFailureTests(PublishCase):
    """Injected failures: staging is left for inspection and no partial output appears."""

    def forbid_replacing_fallbacks(self):
        """While the publisher runs, any replacing rename or removal fails the test."""
        real = cli.publish

        def guarded(*args, **kwargs):
            patches = [
                mock.patch.object(publisher.os, name, side_effect=AssertionError("replace or removal attempted"))
                for name in ("rename", "replace", "renames", "unlink", "rmdir", "remove")
            ]
            for patch in patches:
                patch.start()
            try:
                return real(*args, **kwargs)
            finally:
                for patch in patches:
                    patch.stop()

        patcher = mock.patch.object(cli, "publish", side_effect=guarded)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertStageComplete(self):
        self.assertEqual({path.name: path.read_bytes() for path in self.stage.iterdir()}, self.expected)
        self.assertEqual({mode_of(path) for path in self.stage.iterdir()}, {0o400})

    def test_unsupported_platform_refuses_before_any_write(self):
        self.forbid_replacing_fallbacks()
        before = F.tree_snapshot(self.root)
        for patch in (
            mock.patch.object(publisher, "resolve_noreplace_rename", return_value=None),
            mock.patch.object(publisher, "_platform_supported", return_value=False),
            mock.patch.object(publisher.sys, "platform", "synthetic_os"),
            mock.patch.object(publisher.ctypes, "CDLL", side_effect=OSError("no libc /abs/path")),
        ):
            with patch:
                code, report = self.generate()
            self.assertEqual(code, cli.EXIT_REFUSED)
            self.assertNotPublished(report, R.ATOMIC_PUBLISH_UNSUPPORTED, staging_retained=False)
            self.assertFalse(report["writes_performed"])
            self.assertEqual(before, F.tree_snapshot(self.root))

    def test_unsupported_filesystem_leaves_staging_and_never_falls_back(self):
        self.forbid_replacing_fallbacks()
        for number in (errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EXDEV):
            with self.subTest(errno=number):
                def unsupported(*_args, _number=number):
                    raise OSError(_number, f"synthetic failure {self.root}")

                with mock.patch.object(publisher, "resolve_noreplace_rename", return_value=unsupported):
                    code, report = self.generate()
                self.assertEqual(code, cli.EXIT_REFUSED)
                self.assertNotPublished(report, R.ATOMIC_PUBLISH_UNSUPPORTED, staging_retained=True)
                self.assertTrue(report["writes_performed"])
                self.assertStageComplete()
                self.assertEqual(list(self.output.parent.iterdir()), [])
                # The retained stage blocks the next run until the operator removes it.
                self.assertRefused(R.STAGING_EXISTS)
                __import__("shutil").rmtree(self.stage)

    def test_other_rename_errors_are_one_fixed_code(self):
        def denied(*_args):
            raise OSError(errno.EACCES, f"synthetic failure {self.root}")

        with mock.patch.object(publisher, "resolve_noreplace_rename", return_value=denied):
            code, report = self.generate()
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertNotPublished(report, R.PUBLISH_FAILED, staging_retained=True)
        self.assertStageComplete()

    def test_race_to_target_keeps_the_winner_untouched(self):
        self.forbid_replacing_fallbacks()
        real = publisher.resolve_noreplace_rename()
        for winner in ("directory", "empty_directory", "file", "symlink"):
            with self.subTest(winner=winner):
                def racing(source_fd, source_name, target_fd, target_name, _winner=winner):
                    # Another actor creates the target after the final check, before the rename.
                    if _winner == "file":
                        self.output.write_text("winner\n", "utf-8")
                    elif _winner == "symlink":
                        os.symlink(self.root, self.output)
                    else:
                        os.mkdir(self.output)
                        if _winner == "directory":
                            (self.output / "edited.sql").write_text("select 1\n", "utf-8")
                    racing.snapshot = F.tree_snapshot(self.output.parent)
                    real(source_fd, source_name, target_fd, target_name)

                with mock.patch.object(publisher, "resolve_noreplace_rename", return_value=racing):
                    code, report = self.generate()
                self.assertEqual(code, cli.EXIT_REFUSED)
                self.assertEqual(report["reason_codes"], [R.OUTPUT_EXISTS])
                self.assertEqual(report["status"], "refused")
                self.assertTrue(report["staging_retained"])
                self.assertEqual(racing.snapshot, F.tree_snapshot(self.output.parent))
                self.assertStageComplete()
                __import__("shutil").rmtree(self.stage)
                if self.output.is_dir() and not self.output.is_symlink():
                    __import__("shutil").rmtree(self.output)
                else:
                    self.output.unlink()

    def test_partial_staging_failure_publishes_nothing(self):
        self.forbid_replacing_fallbacks()
        real = publisher._write_staged_file
        calls = []

        def failing(stage_fd, name, data):
            if len(calls) == 2:
                raise OSError(errno.ENOSPC, f"synthetic failure {self.root}")
            calls.append(name)
            real(stage_fd, name, data)

        with mock.patch.object(publisher, "_write_staged_file", side_effect=failing):
            code, report = self.generate()
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertNotPublished(report, R.STAGING_FAILED, staging_retained=True)
        self.assertTrue(report["writes_performed"])
        self.assertEqual(sorted(path.name for path in self.stage.iterdir()), sorted(calls))
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.output.parent.exists())  # not even the generated/ parent
        before = F.tree_snapshot(self.root)
        code, report = self.generate()  # no cleanup, no resume
        self.assertEqual(report["reason_codes"], [R.STAGING_EXISTS])
        self.assertEqual(before, F.tree_snapshot(self.root))

    def test_fsync_failure_publishes_nothing(self):
        with mock.patch.object(publisher.os, "fsync", side_effect=OSError(errno.EIO, "synthetic")):
            code, report = self.generate()
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertNotPublished(report, R.STAGING_FAILED, staging_retained=True)

    def test_unexpected_exception_is_sanitized(self):
        with mock.patch.object(publisher, "_write_staged_file", side_effect=RuntimeError(f"boom {self.root}")):
            code, report = self.generate()
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertNotPublished(report, R.STAGING_FAILED, staging_retained=True)

    def test_staged_content_mismatch_fails_final_verification(self):
        real = publisher._write_staged_file
        cases = {
            "content": lambda fd, name, data: real(fd, name, data[:-1] + b"\x00"),
            "length": lambda fd, name, data: real(fd, name, data + b"-- extra\n"),
            "extra_entry": lambda fd, name, data: (real(fd, name, data), real(fd, f"extra_{name}", b"x\n")),
            "mode": lambda fd, name, data: (real(fd, name, data), os.chmod(name, 0o644, dir_fd=fd)),
            "hard_link": lambda fd, name, data: (
                real(fd, name, data), os.link(name, str(self.root / f"link_{name}"), src_dir_fd=fd)
            ),
        }
        for label, tamper in cases.items():
            with self.subTest(label):
                with mock.patch.object(publisher, "_write_staged_file", side_effect=tamper):
                    code, report = self.generate()
                self.assertEqual(code, cli.EXIT_REFUSED)
                self.assertNotPublished(report, R.STAGING_VERIFICATION_FAILED, staging_retained=True)
                __import__("shutil").rmtree(self.stage)

    def test_stage_name_swapped_during_verification_is_refused(self):
        real = publisher._verify_staged_file
        swapped = []

        def swapping(stage_fd, name, data, euid):
            if not swapped:
                self.stage.rename(self.staging_root / "moved_aside")
                self.stage.mkdir()
                swapped.append(name)
            return real(stage_fd, name, data, euid)

        with mock.patch.object(publisher, "_verify_staged_file", side_effect=swapping):
            code, report = self.generate()
        self.assertTrue(swapped)
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertNotPublished(report, R.STAGING_VERIFICATION_FAILED, staging_retained=True)

    def test_stage_name_swapped_after_verification_is_reported_not_trusted(self):
        """Outside the guarantee (same-user actor in the verify-to-rename window), but detected."""
        real = publisher.resolve_noreplace_rename()

        def swapping(source_fd, source_name, target_fd, target_name):
            self.stage.rename(self.staging_root / "moved_aside")
            self.stage.mkdir()
            real(source_fd, source_name, target_fd, target_name)

        with mock.patch.object(publisher, "resolve_noreplace_rename", return_value=swapping):
            code, report = self.generate()
        self.assertEqual(code, cli.EXIT_PUBLISHED_INCOMPLETE)
        self.assertEqual(report["reason_codes"], [R.PUBLISHED_OUTPUT_UNVERIFIED])
        self.assertEqual(report["status"], "published")

    def test_post_publish_finalize_failure_reports_published_output(self):
        expected = self.expected
        real = os.fchmod

        def failing(descriptor, mode):
            if mode == publisher.PUBLISHED_FILE_MODE:
                raise OSError(errno.EPERM, f"synthetic failure {self.root}")
            real(descriptor, mode)

        with mock.patch.object(publisher.os, "fchmod", side_effect=failing):
            code, report = self.generate()
        self.assertEqual(code, cli.EXIT_PUBLISHED_INCOMPLETE)
        self.assertEqual(report["status"], "published")
        self.assertEqual(report["reason_codes"], [R.PUBLISHED_FINALIZE_INCOMPLETE])
        self.assertFalse(report["staging_retained"])
        self.assertTrue(report["writes_performed"])
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()}, expected)
        self.assertFalse(self.stage.exists())
        self.assertEqual(self.generate()[1]["reason_codes"], [R.OUTPUT_EXISTS])


class NoReplaceRenameTests(unittest.TestCase):
    """The real platform primitive: it must refuse every existing target kind."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="scaffold-synthetic-"))
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        self.rename = publisher.resolve_noreplace_rename()
        if self.rename is None:
            self.fail("this platform has no atomic no-replace rename; write mode would refuse")

    def test_renames_only_onto_a_missing_name(self):
        source, target = self.root / "a", self.root / "b"
        source.mkdir()
        target.mkdir()
        (source / "staged").write_text("staged\n", "utf-8")
        source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
        target_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, source_fd)
        self.addCleanup(os.close, target_fd)
        (target / "dir").mkdir()
        (target / "file").write_text("keep\n", "utf-8")
        (target / "link").symlink_to(self.root / "dangling")
        (source / "stage").mkdir()
        for existing in ("dir", "file", "link"):
            with self.assertRaises(OSError) as caught:
                self.rename(source_fd, "stage", target_fd, existing)
            self.assertEqual(caught.exception.errno, errno.EEXIST)
        self.assertEqual((target / "file").read_text("utf-8"), "keep\n")
        self.assertTrue((source / "stage").is_dir())
        self.rename(source_fd, "stage", target_fd, "fresh")
        self.assertTrue((target / "fresh").is_dir())
        self.assertFalse((source / "stage").exists())


if __name__ == "__main__":
    unittest.main()
