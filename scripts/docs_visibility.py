#!/usr/bin/env python3
"""Decide which Markdown pages are internal and check that none are published.

A page is private when its repository-relative path is listed in
``website/docs-visibility.toml`` (``private_pages``) or when its YAML front
matter sets ``visibility: private`` or ``internal: true``. Front matter fails
closed: any ``visibility`` other than ``public`` and any ``internal`` other than
``false``/``no``/``off`` is private. A denylist entry whose file is missing is a
configuration error, so renaming a listed page cannot silently publish it.

Commands:
  list-private          Print repository-relative paths of private pages.
  check-staged CONTENT  Fail if a private page was staged into CONTENT.
  check-site SITE       Fail if a private page or internal marker reached SITE.

Exit codes: 0 clean, 1 private content found, 2 configuration or usage error.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path("website") / "docs-visibility.toml"
# Generated, vendored, or hidden trees never hold source pages for the site.
SKIP_DIR_NAMES = {"node_modules", "target", "dbt_packages", "logs", "site", "content", "__pycache__"}
# Fail closed: any visibility other than "public", and any internal value other
# than an explicit false, marks the page private (including YAML block scalars).
PUBLIC_VISIBILITY_VALUES = {"public"}
FALSE_VALUES = {"false", "no", "off"}
FRONT_MATTER_MAX_LINES = 200
MARKER_SUFFIXES = {".html", ".json", ".xml", ".txt", ".md"}


class ConfigError(Exception):
    """The visibility configuration is missing or malformed."""


@dataclass(frozen=True)
class Visibility:
    private_pages: frozenset[str]
    private_markers: tuple[str, ...]


def _normalise_page(entry: object) -> str:
    if not isinstance(entry, str) or not entry.strip():
        raise ConfigError("private_pages entries must be non-empty strings")
    path = PurePosixPath(entry.strip())
    if path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".md":
        raise ConfigError(f"private_pages entry must be a relative .md path: {entry!r}")
    return path.as_posix()


def load_config(config_path: Path) -> Visibility:
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"visibility config not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"visibility config is not valid TOML: {config_path}: {exc}") from exc
    unknown = set(raw) - {"private_pages", "private_markers"}
    if unknown:
        raise ConfigError(f"unknown keys in visibility config: {sorted(unknown)}")
    pages = raw.get("private_pages", [])
    markers = raw.get("private_markers", [])
    if not isinstance(pages, list) or not isinstance(markers, list):
        raise ConfigError("private_pages and private_markers must be lists")
    if not all(isinstance(m, str) and m.strip() for m in markers):
        raise ConfigError("private_markers entries must be non-empty strings")
    return Visibility(
        private_pages=frozenset(_normalise_page(p) for p in pages),
        private_markers=tuple(m.strip() for m in markers),
    )


def front_matter(text: str) -> dict[str, str]:
    """Return top-level ``key: value`` pairs from a leading YAML front matter block."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    values: dict[str, str] = {}
    for line in lines[1:FRONT_MATTER_MAX_LINES]:
        if line.strip() in {"---", "..."}:
            return values
        if not line or line[0].isspace() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.split(" #", 1)[0].strip().strip("'\"").strip()
        values[key.strip().lower()] = value.lower()
    # No closing delimiter: not front matter.
    return {}


def is_private_front_matter(text: str) -> bool:
    meta = front_matter(text)
    if "visibility" in meta and meta["visibility"] not in PUBLIC_VISIBILITY_VALUES:
        return True
    return "internal" in meta and meta["internal"] not in FALSE_VALUES


def iter_markdown(base: Path) -> Iterator[Path]:
    for path in sorted(base.rglob("*.md")):
        rel_parts = path.relative_to(base).parts
        if any(part.startswith(".") or part in SKIP_DIR_NAMES for part in rel_parts[:-1]):
            continue
        if path.is_file():
            yield path


def private_by_front_matter(base: Path) -> set[str]:
    found = set()
    for path in iter_markdown(base):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        if is_private_front_matter(text):
            found.add(path.relative_to(base).as_posix())
    return found


def list_private(root: Path, visibility: Visibility) -> list[str]:
    return sorted(visibility.private_pages | private_by_front_matter(root))


def staged_name(repo_rel: str) -> str:
    """Map a repository path to its staged path (the root README becomes index.md)."""
    return "index.md" if repo_rel == "README.md" else repo_rel


def site_candidates(staged_rel: str) -> list[str]:
    """Return built-site files MkDocs may emit for a staged Markdown page."""
    path = PurePosixPath(staged_rel)
    parent = path.parent
    if path.stem.lower() in {"index", "readme"}:
        return [(parent / "index.html").as_posix()]
    return [(parent / path.stem / "index.html").as_posix(), (parent / f"{path.stem}.html").as_posix()]


def check_staged(root: Path, content: Path, visibility: Visibility) -> list[str]:
    problems = []
    for repo_rel in list_private(root, visibility):
        staged = content / staged_name(repo_rel)
        if staged.exists():
            problems.append(f"private page staged: {staged_name(repo_rel)} (from {repo_rel})")
    for staged_rel in sorted(private_by_front_matter(content)):
        problems.append(f"page with private front matter staged: {staged_rel}")
    return sorted(set(problems))


def _marker_files(site: Path) -> Iterable[Path]:
    for path in sorted(site.rglob("*")):
        if path.is_file() and path.suffix.lower() in MARKER_SUFFIXES:
            yield path


def check_site(root: Path, site: Path, visibility: Visibility) -> list[str]:
    if not site.is_dir():
        raise ConfigError(f"built site directory not found: {site}")
    problems = []
    for repo_rel in list_private(root, visibility):
        for candidate in site_candidates(staged_name(repo_rel)):
            if (site / candidate).exists():
                problems.append(f"private page published: {candidate} (from {repo_rel})")
    for path in _marker_files(site):
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in visibility.private_markers:
            if marker in text:
                problems.append(f"internal marker {marker!r} found in {path.relative_to(site).as_posix()}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    parser.add_argument("--config", type=Path, help="visibility config (default: website/docs-visibility.toml)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-private")
    staged = sub.add_parser("check-staged")
    staged.add_argument("content", type=Path)
    site = sub.add_parser("check-site")
    site.add_argument("site", type=Path)
    args = parser.parse_args(argv)

    root = args.root.resolve()
    config = args.config if args.config is not None else root / DEFAULT_CONFIG
    try:
        visibility = load_config(config)
        missing = sorted(page for page in visibility.private_pages if not (root / page).is_file())
        if missing:
            raise ConfigError(
                "private_pages entries do not exist (renamed or deleted? update "
                f"website/docs-visibility.toml and keep the page private): {', '.join(missing)}"
            )
        if args.command == "list-private":
            for page in list_private(root, visibility):
                print(page)
            return 0
        if args.command == "check-staged":
            problems = check_staged(root, args.content, visibility)
        else:
            problems = check_site(root, args.site, visibility)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    if problems:
        print("Internal pages must not be published; see website/docs-visibility.toml", file=sys.stderr)
        return 1
    print(f"OK: no private pages or internal markers found ({args.command})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
