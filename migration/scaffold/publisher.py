"""Create-only, atomic no-replace publication of one rendered Silver scaffold.

`publish()` is the only code in this package that writes. It takes text that already passed
the renderer contract and either publishes the complete output directory or publishes
nothing:

1. Refuse before any write when the platform has no atomic no-replace directory rename
   (Linux `renameat2(RENAME_NOREPLACE)`, macOS `renameatx_np(RENAME_EXCL)`).
2. Open the project, `models/`, and `models/migration/` as no-follow directory descriptors
   and re-check the output path through them.
3. Create (or admit) the private staging root `.scaffold-staging/` and exclusively create
   the stage directory `.scaffold-staging/<source_id>/`.
4. Create every file exclusively (`O_CREAT | O_EXCL | O_NOFOLLOW`), write, set it read-only,
   and fsync it; fsync the stage directory and the staging root.
5. Create `models/migration/generated/` if it is missing.
6. Verify the staged tree through the held descriptors immediately before publication:
   exact entry names, ownership, modes, link counts, sizes, SHA-256 of every file, and that
   the stage and target names still resolve as expected.
7. Rename the stage directory onto `generated/<source_id>` with the no-replace flag.
8. Restore editable modes (files 0600, directory 0700) through the held descriptors and
   fsync the affected directories.

The stage directory stays owner-writable (0700): a directory needs owner write permission to
be renamed between parents, so a sealed read-only directory cannot be published. Staged
files are read-only (0400) until the rename.

Guarantee, and its limits: with the directory descriptors held, the tree verified in step 6,
and an atomic no-replace rename, the published directory is exactly the verified tree and
never replaces or merges with an existing path. This assumes no malicious process running as
the same user (or a privileged one) changes staged content between step 6 and step 7; that
window cannot be closed with a renameable directory.

Nothing is ever removed, replaced, or renamed by a fallback. On failure the stage directory
is left in place for inspection and the caller reports one fixed reason code. There is no
plain-rename, copy, or per-file publication fallback. Errors never carry paths, names, or
exception text.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import stat
import sys
from dataclasses import dataclass
from typing import Callable, Mapping

from . import reasons as R
from .plan import MAX_RENDERED_FILE_BYTES, OUTPUT_PARENT, STAGING_ROOT


STAGED_FILE_MODE = 0o400
PUBLISHED_FILE_MODE = 0o600
DIRECTORY_MODE = 0o700
_RENAME_NOREPLACE = 1  # linux/fs.h
_RENAME_EXCL = 0x00000004  # darwin sys/stdio.h
_UNSUPPORTED_ERRNOS = frozenset(
    value for value in (
        errno.ENOSYS, errno.EINVAL, errno.EXDEV,
        getattr(errno, "ENOTSUP", None), getattr(errno, "EOPNOTSUPP", None),
    ) if value is not None
)
_TARGET_EXISTS_ERRNOS = frozenset({errno.EEXIST, errno.ENOTEMPTY})
_FILE_NAME = re.compile(r"\.?[A-Za-z0-9_][A-Za-z0-9_.-]*")
_SOURCE_ID = re.compile(r"[a-z][a-z0-9_]{0,39}")

NoReplaceRename = Callable[[int, str, int, str], None]


@dataclass(frozen=True)
class PublishResult:
    """Outcome of one publication attempt. Carries no paths, names, or exception text.

    code:             None on success, otherwise one fixed reason code.
    published:        True once the no-replace rename succeeded (the output directory exists
                      and is complete, even if `code` reports an incomplete finalize step).
    staging_retained: True when this run created `.scaffold-staging/<source_id>/` and left it.
    touched:          True when this run created any directory or file in the workspace.
    """

    code: str | None
    published: bool = False
    staging_retained: bool = False
    touched: bool = False


class _Stop(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _libc_rename(symbol: str, flag: int) -> NoReplaceRename | None:
    try:
        function = getattr(ctypes.CDLL(None, use_errno=True), symbol)
    except (OSError, AttributeError):
        return None
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int

    def rename(source_fd: int, source_name: str, target_fd: int, target_name: str) -> None:
        ctypes.set_errno(0)
        if function(source_fd, os.fsencode(source_name), target_fd, os.fsencode(target_name), flag) != 0:
            raise OSError(ctypes.get_errno() or errno.EIO, "no-replace rename failed")

    return rename


def resolve_noreplace_rename() -> NoReplaceRename | None:
    """The platform's atomic no-replace rename, or None. There is no replacing fallback."""
    if sys.platform.startswith("linux"):
        return _libc_rename("renameat2", _RENAME_NOREPLACE)
    if sys.platform == "darwin":
        return _libc_rename("renameatx_np", _RENAME_EXCL)
    return None


def _platform_supported() -> bool:
    return (
        all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "geteuid", "fchmod", "fsync"))
        and {os.open, os.mkdir, os.stat} <= os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
        and os.listdir in os.supports_fd
    )


def _entry_kind(name: str, dir_fd: int) -> str:
    try:
        mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _open_dir(name: str, dir_fd: int | None, failure: str) -> int:
    """Open one directory component without following a symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        if dir_fd is None:
            return os.open(name, flags)
        return os.open(name, flags, dir_fd=dir_fd)
    except OSError:
        if dir_fd is not None and _entry_kind(name, dir_fd) == "symlink":
            raise _Stop(R.SYMLINK_REFUSED) from None
        raise _Stop(failure) from None


def _same_entry(info: os.stat_result, name: str, dir_fd: int) -> bool:
    try:
        named = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(named.st_mode) and (named.st_dev, named.st_ino) == (info.st_dev, info.st_ino)


def _refuse_existing_target(kind: str) -> None:
    if kind == "symlink":
        raise _Stop(R.SYMLINK_REFUSED)
    if kind == "other":
        raise _Stop(R.NOT_REGULAR_FILE)
    if kind != "missing":
        raise _Stop(R.OUTPUT_EXISTS)


def _write_staged_file(stage_fd: int, name: str, data: bytes) -> None:
    """Exclusively create one staged file, write it, make it read-only, and fsync it."""
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=stage_fd)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(errno.EIO, "short write")
            view = view[written:]
        os.fchmod(descriptor, STAGED_FILE_MODE)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_staged_file(stage_fd: int, name: str, data: bytes, euid: int) -> int:
    """Re-read one staged file through the stage descriptor; return the still-open descriptor."""
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=stage_fd)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != euid or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != STAGED_FILE_MODE or info.st_size != len(data)
        ):
            raise _Stop(R.STAGING_VERIFICATION_FAILED)
        digest = hashlib.sha256()
        remaining = len(data) + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining != 1 or digest.digest() != hashlib.sha256(data).digest():
            raise _Stop(R.STAGING_VERIFICATION_FAILED)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def publish(project: str, source_id: str, files: Mapping[str, str]) -> PublishResult:
    """Publish `{bare file name: text}` as `<project>/models/migration/generated/<source_id>/`.

    `project` is the absolute dbt project directory returned by `check_workspace`. Never raises
    for filesystem conditions: every failure is one fixed reason code in the result.
    """
    held: list[int] = []
    file_fds: list[int] = []
    state = {"staged": False, "published": False, "touched": False}

    def result(code: str | None) -> PublishResult:
        return PublishResult(
            code=code, published=state["published"],
            staging_retained=state["staged"] and not state["published"], touched=state["touched"],
        )

    def hold(descriptor: int) -> int:
        held.append(descriptor)
        return descriptor

    try:
        if (
            not isinstance(source_id, str) or _SOURCE_ID.fullmatch(source_id) is None or not files
            or any(not isinstance(name, str) or _FILE_NAME.fullmatch(name) is None for name in files)
            or any(not isinstance(text, str) for text in files.values())
        ):
            return result(R.PUBLISH_FAILED)
        try:
            payload = {name: files[name].encode("utf-8") for name in sorted(files)}
        except UnicodeEncodeError:
            return result(R.PUBLISH_FAILED)
        if any(len(data) > MAX_RENDERED_FILE_BYTES for data in payload.values()):
            return result(R.PUBLISH_FAILED)

        rename = resolve_noreplace_rename() if _platform_supported() else None
        if rename is None:
            return result(R.ATOMIC_PUBLISH_UNSUPPORTED)
        euid = os.geteuid()

        # Held no-follow descriptors for every directory the publication touches.
        project_fd = hold(_open_dir(project, None, R.PUBLISH_FAILED))
        parent_fd = project_fd
        for component in OUTPUT_PARENT[:-1]:
            parent_fd = hold(_open_dir(component, parent_fd, R.PUBLISH_FAILED))
        generated_name = OUTPUT_PARENT[-1]
        generated_fd = None
        kind = _entry_kind(generated_name, parent_fd)
        if kind == "dir":
            generated_fd = hold(_open_dir(generated_name, parent_fd, R.PUBLISH_FAILED))
            _refuse_existing_target(_entry_kind(source_id, generated_fd))
        else:
            _refuse_existing_target(kind)

        # Private staging root: created 0700, or admitted only if already owner-only.
        try:
            os.mkdir(STAGING_ROOT, DIRECTORY_MODE, dir_fd=project_fd)
            created_root = True
            state["touched"] = True
        except FileExistsError:
            created_root = False
        except OSError:
            raise _Stop(R.STAGING_FAILED) from None
        root_fd = hold(_open_dir(STAGING_ROOT, project_fd, R.STAGING_ROOT_INVALID))
        root_info = os.fstat(root_fd)
        if created_root and root_info.st_uid == euid:
            os.fchmod(root_fd, DIRECTORY_MODE)
            root_info = os.fstat(root_fd)
        if root_info.st_uid != euid or stat.S_IMODE(root_info.st_mode) != DIRECTORY_MODE:
            raise _Stop(R.STAGING_ROOT_INVALID)
        if root_info.st_dev != os.fstat(parent_fd).st_dev:
            raise _Stop(R.ATOMIC_PUBLISH_UNSUPPORTED)
        if generated_fd is not None and root_info.st_dev != os.fstat(generated_fd).st_dev:
            raise _Stop(R.ATOMIC_PUBLISH_UNSUPPORTED)

        # Exclusive stage directory. It stays 0700: a directory must be owner-writable to be
        # renamed between parents.
        try:
            os.mkdir(source_id, DIRECTORY_MODE, dir_fd=root_fd)
        except FileExistsError:
            if _entry_kind(source_id, root_fd) == "symlink":
                raise _Stop(R.SYMLINK_REFUSED) from None
            raise _Stop(R.STAGING_EXISTS) from None
        except OSError:
            raise _Stop(R.STAGING_FAILED) from None
        state["staged"] = state["touched"] = True
        stage_fd = hold(_open_dir(source_id, root_fd, R.STAGING_FAILED))
        if os.fstat(stage_fd).st_uid != euid:
            raise _Stop(R.STAGING_FAILED)
        os.fchmod(stage_fd, DIRECTORY_MODE)
        for name, data in payload.items():
            _write_staged_file(stage_fd, name, data)
        os.fsync(stage_fd)
        os.fsync(root_fd)

        if generated_fd is None:
            try:
                os.mkdir(generated_name, DIRECTORY_MODE, dir_fd=parent_fd)
                created_generated = True
            except FileExistsError:
                created_generated = False
            except OSError:
                raise _Stop(R.PUBLISH_FAILED) from None
            generated_fd = hold(_open_dir(generated_name, parent_fd, R.PUBLISH_FAILED))
            if created_generated:
                if os.fstat(generated_fd).st_uid != euid:
                    raise _Stop(R.PUBLISH_FAILED)
                os.fchmod(generated_fd, DIRECTORY_MODE)
                os.fsync(parent_fd)

        # Final verification through the held descriptors, immediately before publication.
        try:
            stage_info = os.fstat(stage_fd)
            if (
                not stat.S_ISDIR(stage_info.st_mode) or stage_info.st_uid != euid
                or stat.S_IMODE(stage_info.st_mode) != DIRECTORY_MODE
                or sorted(os.listdir(stage_fd)) != list(payload)
            ):
                raise _Stop(R.STAGING_VERIFICATION_FAILED)
            for name, data in payload.items():
                file_fds.append(_verify_staged_file(stage_fd, name, data, euid))
            if stage_info.st_dev != os.fstat(generated_fd).st_dev:
                raise _Stop(R.ATOMIC_PUBLISH_UNSUPPORTED)
            # Last, because the rename addresses the stage by name: the names must still
            # resolve to the verified directories.
            if (
                sorted(os.listdir(stage_fd)) != list(payload)
                or not _same_entry(os.fstat(root_fd), STAGING_ROOT, project_fd)
                or not _same_entry(stage_info, source_id, root_fd)
            ):
                raise _Stop(R.STAGING_VERIFICATION_FAILED)
        except OSError:
            raise _Stop(R.STAGING_VERIFICATION_FAILED) from None
        _refuse_existing_target(_entry_kind(source_id, generated_fd))

        # Atomic no-replace publication. No fallback of any kind.
        try:
            rename(root_fd, source_id, generated_fd, source_id)
        except OSError as error:
            if error.errno in _TARGET_EXISTS_ERRNOS:
                raise _Stop(R.OUTPUT_EXISTS) from None
            if error.errno in _UNSUPPORTED_ERRNOS:
                raise _Stop(R.ATOMIC_PUBLISH_UNSUPPORTED) from None
            raise _Stop(R.PUBLISH_FAILED) from None
        state["published"] = True

        # Restore editable modes on exactly the published inodes, then make it durable.
        if not _same_entry(stage_info, source_id, generated_fd):
            raise _Stop(R.PUBLISHED_OUTPUT_UNVERIFIED)
        for descriptor in file_fds:
            os.fchmod(descriptor, PUBLISHED_FILE_MODE)
            os.fsync(descriptor)
        os.fchmod(stage_fd, DIRECTORY_MODE)
        os.fsync(stage_fd)
        os.fsync(generated_fd)
        os.fsync(root_fd)
        return result(None)
    except _Stop as stop:
        return result(stop.code)
    except Exception:
        if state["published"]:
            return result(R.PUBLISHED_FINALIZE_INCOMPLETE)
        return result(R.STAGING_FAILED if state["staged"] else R.PUBLISH_FAILED)
    finally:
        for descriptor in (*file_fds, *reversed(held)):
            try:
                os.close(descriptor)
            except OSError:
                pass
