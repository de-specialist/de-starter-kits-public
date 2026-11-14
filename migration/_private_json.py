"""Owner-only, no-follow JSON file I/O for migration evidence artifacts."""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path


DEFAULT_MAX_BYTES = 10 * 1024 * 1024


def _require_secure_platform() -> None:
    if os.name != "posix" or not hasattr(os, "geteuid"):
        raise ValueError("Cannot verify private JSON file permissions on this platform.")
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "supports_dir_fd")):
        raise ValueError("Secure directory-relative JSON file operations are unavailable.")
    if any(
        function not in os.supports_dir_fd
        for function in (os.open, os.stat, os.unlink, os.rename)
    ):
        raise ValueError("Secure directory-relative JSON file operations are unavailable.")


def _validate_directory(directory_fd: int) -> None:
    info = os.fstat(directory_fd)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("Private JSON parent is not a directory.")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Private JSON directory must be current-user-owned with owner-only permissions.")


def _open_directory_no_symlinks(directory: Path) -> int:
    """Open a directory by walking each component without following symlinks."""
    raw = os.fspath(directory)
    if ".." in Path(raw).parts:
        raise ValueError("Private JSON directory must not contain parent-directory components.")
    absolute = os.path.abspath(raw)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    current_fd = os.open(os.path.sep, flags)
    try:
        for component in Path(absolute).parts[1:]:
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _validate_file(info) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Private JSON path must be a regular file.")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1:
        raise ValueError("Private JSON file must be current-user-owned, owner-only, and single-link.")


def _basename(filename: str) -> str:
    if not isinstance(filename, str) or filename in {"", ".", ".."} or Path(filename).name != filename:
        raise ValueError("Private JSON filename must be a single path component.")
    return filename


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Private JSON object contains duplicate keys.")
        result[key] = value
    return result


def read_private_json(directory: Path, filename: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, object]:
    """Read an owner-only JSON object without following links or exceeding max_bytes."""
    _require_secure_platform()
    name = _basename(filename)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("Private JSON size limit must be a positive integer.")
    directory_fd = _open_directory_no_symlinks(directory)
    try:
        _validate_directory(directory_fd)
        file_fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0),
            dir_fd=directory_fd,
        )
        try:
            info = os.fstat(file_fd)
            _validate_file(info)
            if info.st_size > max_bytes:
                raise ValueError("Private JSON file exceeds the supported size limit.")
            with os.fdopen(file_fd, "rb") as stream:
                file_fd = -1
                raw = stream.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ValueError("Private JSON file exceeds the supported size limit.")
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        finally:
            if file_fd >= 0:
                os.close(file_fd)
    finally:
        os.close(directory_fd)
    if not isinstance(value, dict):
        raise ValueError("Private JSON file must contain an object.")
    return value


def _encode(record: dict[str, object]) -> bytes:
    return (json.dumps(record, indent=2) + "\n").encode("utf-8")


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("Could not complete private JSON write.")
        view = view[written:]
    os.fsync(fd)


def create_private_json(directory: Path, filename: str, record: dict[str, object]) -> None:
    """Create a private JSON object exclusively; an existing path is never replaced."""
    _require_secure_platform()
    name = _basename(filename)
    payload = _encode(record)
    directory_fd = _open_directory_no_symlinks(directory)
    try:
        _validate_directory(directory_fd)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        info = os.fstat(fd)
        try:
            os.fchmod(fd, 0o600)
            _validate_file(os.fstat(fd))
            _write_all(fd, payload)
            os.close(fd)
            fd = -1
            os.fsync(directory_fd)
        except Exception:
            if fd >= 0:
                os.close(fd)
            try:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                    os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(directory_fd)


def update_private_json(directory: Path, filename: str, record: dict[str, object]) -> None:
    """Atomically replace a validated private JSON object in its current directory."""
    _require_secure_platform()
    name = _basename(filename)
    payload = _encode(record)
    directory_fd = _open_directory_no_symlinks(directory)
    temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
    try:
        _validate_directory(directory_fd)
        existing_fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0),
            dir_fd=directory_fd,
        )
        try:
            existing_info = os.fstat(existing_fd)
            _validate_file(existing_info)
        finally:
            os.close(existing_fd)

        temp_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            os.fchmod(temp_fd, 0o600)
            _validate_file(os.fstat(temp_fd))
            _write_all(temp_fd, payload)
            os.close(temp_fd)
            temp_fd = -1
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (existing_info.st_dev, existing_info.st_ino):
                raise ValueError("Private JSON file changed during update.")
            _validate_file(current)
            os.rename(temporary_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(directory_fd)
