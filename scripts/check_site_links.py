#!/usr/bin/env python3
"""Check internal links in a built static site, offline.

Scans every HTML page under the site directory for ``href`` and ``src``
attributes, resolves relative and root-relative URLs against the site tree, and
fails if a target file or ``#fragment`` anchor is missing (Material's ``#__``
UI toggles excepted).
External URLs (any scheme or protocol-relative ``//host``) are skipped: this
script makes no network calls and uses only the standard library.

Embedded dbt docs (``*/dbt-docs/``) are generated single-page apps whose links
are client-side routes, so their pages are not scanned; links *to* them are.

Usage: python scripts/check_site_links.py website/site
"""

from __future__ import annotations

import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

LINK_ATTRS = {"href", "src"}
SKIP_PAGE_PARTS = {"dbt-docs"}
# MkDocs Material uses "#__..." fragments for UI toggles (drawer, search, code lines).
THEME_FRAGMENT_PREFIX = "__"


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if value is None:
                continue
            if name in ("id", "name"):
                self.ids.add(value)
            elif name in LINK_ATTRS:
                self.links.append(value)


def _parse(path: Path) -> _PageParser:
    parser = _PageParser()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    return parser


def _resolve(site: Path, page: Path, path_part: str) -> Path | None:
    """Map a URL path to the file it serves, or None if it is missing."""
    if path_part.startswith("/"):
        target = site / unquote(path_part.lstrip("/"))
    else:
        target = page.parent / unquote(path_part)
    if path_part.endswith("/") or target.is_dir():
        target = target / "index.html"
    try:
        target = target.resolve()
        target.relative_to(site)
    except (OSError, ValueError):
        return None
    return target if target.is_file() else None


def check_site(site: Path) -> list[str]:
    site = site.resolve()
    pages = sorted(site.rglob("*.html"))
    parsed: dict[Path, _PageParser] = {}
    errors: list[str] = []

    def page_data(path: Path) -> _PageParser:
        if path not in parsed:
            parsed[path] = _parse(path)
        return parsed[path]

    for page in pages:
        rel_page = page.relative_to(site)
        if SKIP_PAGE_PARTS.intersection(rel_page.parts):
            continue
        for link in page_data(page).links:
            parts = urlsplit(link)
            if parts.scheme or parts.netloc or link.startswith("//"):
                continue
            if not parts.path:
                # Same-page fragment. Raw HTML anchors are not checked by MkDocs.
                fragment = unquote(parts.fragment)
                if (
                    fragment
                    and not fragment.startswith(THEME_FRAGMENT_PREFIX)
                    and fragment not in page_data(page).ids
                ):
                    errors.append(f"{rel_page}: missing anchor {link!r}")
                continue
            target = _resolve(site, page, parts.path)
            if target is None:
                errors.append(f"{rel_page}: broken link {link!r}")
                continue
            fragment = unquote(parts.fragment)
            if (
                fragment
                and target.suffix == ".html"
                and not SKIP_PAGE_PARTS.intersection(target.relative_to(site).parts)
                and fragment not in page_data(target).ids
            ):
                errors.append(f"{rel_page}: missing anchor {link!r}")
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    site = Path(argv[1])
    if not (site / "index.html").is_file():
        print(f"ERROR: {site} does not look like a built site (no index.html)", file=sys.stderr)
        return 2
    errors = check_site(site)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"{len(errors)} broken internal link(s) in {site}", file=sys.stderr)
        return 1
    print(f"Internal links OK in {site}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
