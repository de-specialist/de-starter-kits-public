"""Read-only client-workspace and collision checks (contract proposal 3.1, 6.1-6.3).

Everything here uses lstat or no-follow opens and never creates, modifies, or removes a path.
Absolute paths are used internally only and are never returned to callers that print.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from . import reasons as R
from .plan import OUTPUT_PARENT, STAGING_ROOT
from .reasons import Refusal


STARTER_REPO_ROOT = Path(__file__).resolve().parents[2]
# Edition -> (workspace-relative dbt project directory, required dbt project name).
EDITION_PROJECTS = {
    "bigquery": (".", "retail_analytics"),
    "snowflake": ("kit", "reliable_pipeline"),
}
_STARTER_LAYOUT_MARKERS = (("editions", "Bigquery"), ("editions", "Snowflake"))
_MAX_DBT_PROJECT_BYTES = 1024 * 1024
_DBT_PROJECT_NAME = re.compile(
    r"^name:[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[ \t]*(?:#[^\n]*)?$", re.MULTILINE
)


def entry_kind(path: str) -> str:
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    mode = info.st_mode
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _walk(base: str, components: tuple[str, ...]) -> list[str]:
    """Entry kind of each successive component, stopping after a non-directory."""
    kinds = []
    current = base
    for component in components:
        current = os.path.join(current, component)
        kind = entry_kind(current)
        kinds.append(kind)
        if kind != "dir":
            break
    return kinds


def _is_within(path: str, root: str) -> bool:
    try:
        Path(path).relative_to(root)
    except ValueError:
        return False
    return True


def _dbt_project_name_matches(project: str, expected: str) -> bool:
    if not hasattr(os, "O_NOFOLLOW"):
        return False
    flags = (os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0))
    try:
        descriptor = os.open(os.path.join(project, "dbt_project.yml"), flags)
    except OSError:
        return False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_DBT_PROJECT_BYTES:
            return False
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(_MAX_DBT_PROJECT_BYTES + 1)
    except OSError:
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > _MAX_DBT_PROJECT_BYTES:
        return False
    try:
        text = raw.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        return False
    return [match.group(2) for match in _DBT_PROJECT_NAME.finditer(text)] == [expected]


def check_workspace(raw_workspace: str, edition: str, *, starter_root: Path = STARTER_REPO_ROOT) -> str:
    """Section 3.1 workspace checks. Returns the absolute project directory (never printed)."""
    project_relative, project_name = EDITION_PROJECTS[edition]
    absolute = os.path.abspath(raw_workspace)
    project = os.path.normpath(os.path.join(absolute, project_relative))
    repo = os.path.realpath(starter_root)
    for candidate in (os.path.realpath(raw_workspace), absolute, os.path.realpath(project)):
        if _is_within(candidate, repo):
            raise Refusal(R.WORKSPACE_INSIDE_STARTER_REPO)
    # `..` is refused so lexical and filesystem resolution cannot disagree.
    if ".." in Path(raw_workspace).parts or entry_kind(absolute) != "dir":
        raise Refusal(R.WORKSPACE_INVALID)
    if any(entry_kind(os.path.join(absolute, *marker)) != "missing" for marker in _STARTER_LAYOUT_MARKERS):
        raise Refusal(R.WORKSPACE_IS_STARTER_CHECKOUT)
    if entry_kind(project) != "dir":
        raise Refusal(R.WORKSPACE_INVALID)
    if not _dbt_project_name_matches(project, project_name):
        raise Refusal(R.EDITION_PROJECT_MISMATCH)
    kinds = _walk(project, ("models", "migration"))
    if "symlink" in kinds:
        raise Refusal(R.SYMLINK_REFUSED)
    if kinds != ["dir", "dir"]:
        raise Refusal(R.WORKSPACE_MISSING_MIGRATION_MODELS)
    return project


def _staging_root_is_private(path: str) -> bool:
    if not hasattr(os, "geteuid"):
        return False
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0


def check_collisions(project: str, source_id: str) -> None:
    """Section 6.2 step 1 collision precedence, then section 6.3 staging-root validity."""
    output = _walk(project, (*OUTPUT_PARENT, source_id))
    staging = _walk(project, (STAGING_ROOT, source_id))
    if "symlink" in output or "symlink" in staging:
        raise Refusal(R.SYMLINK_REFUSED)
    if "other" in output or "other" in staging:
        raise Refusal(R.NOT_REGULAR_FILE)
    # An existing file or directory at generated/<source_id>; a regular file at generated/
    # itself also blocks the output path (the contract names no separate code).
    if len(output) == 4 and output[3] in {"dir", "file"}:
        raise Refusal(R.OUTPUT_EXISTS)
    if len(output) == 3 and output[2] == "file":
        raise Refusal(R.OUTPUT_EXISTS)
    if len(staging) == 2 and staging[1] in {"dir", "file"}:
        raise Refusal(R.STAGING_EXISTS)
    if staging[0] == "file" or (
        staging[0] == "dir" and not _staging_root_is_private(os.path.join(project, STAGING_ROOT))
    ):
        raise Refusal(R.STAGING_ROOT_INVALID)
