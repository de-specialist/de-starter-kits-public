#!/usr/bin/env python3
"""Reject content that does not belong in a public starter-kit template.

The check is offline, reads files only, and never prints a matched value. It
reports three families of problem:

1. Real-looking contact or credential material anywhere in the scanned tree:
   private key blocks with key material, vendor-format access tokens, secret assignments with a
   high-entropy literal, URLs with an embedded password, email addresses
   outside the reserved documentation domains, and formatted phone numbers
   outside the reserved fictional ranges.
2. Archive and report binaries (ZIP, tar, PDF, office documents, BI report
   files, ...), detected by extension and by leading magic bytes, plus any
   other file that cannot be read as UTF-8 text and is not a known image type.
3. Identifier fields without a ``MOCK-`` or ``MOCK_`` prefix inside designated
   public mock fixture paths (a ``public_mock``/``public-mock`` directory, or
   any ``--mock-path`` glob). Outside those paths identifiers are not checked,
   so ordinary synthetic retail examples such as ``customer_id,1001`` pass.

Field names and documentation of fields (``email``, ``phone``, ``password:
"{{ env_var('X') }}"``) are never findings: only literal values are judged.

Bounds
------
The scan stops with exit status 2 when it would exceed ``MAX_FILES`` files or
``MAX_TOTAL_BYTES`` bytes. A single file above ``MAX_FILE_BYTES`` is reported
as ``oversized-file`` instead of being read. Lines are matched in overlapping
windows of ``LINE_WINDOW`` characters, and reporting stops after
``MAX_FINDINGS`` findings.

False positives
---------------
The patterns are heuristics. When a finding is a false positive, prefer making
the content unambiguous (use ``example.com``, a ``555-01xx`` number, an
environment variable reference, or a ``MOCK-`` identifier). When that is not
possible there are two reviewed escape hatches:

* Inline, for formats that allow comments: put
  ``public-template-guard: allow <rule-id>`` on the reported line. Several
  rule ids may be separated by commas. The pragma only silences the named
  rules on that line.
* By path, for data files: pass ``--allowlist FILE`` (or create
  ``scripts/public_template_content_allowlist.txt``, which is used when
  present). Each non-comment line is ``<rule-id> <path-glob>``; the glob is
  matched with ``fnmatch`` against the POSIX path relative to the scan root,
  so ``*`` also crosses ``/``.

Both forms are visible in review; every entry should say why it is safe.

Exit status: 0 clean, 1 findings, 2 usage error or scan bound exceeded.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import io
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import unquote


MAX_FILES = 20_000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FINDINGS = 200
LINE_WINDOW = 4096
LINE_OVERLAP = 512

DEFAULT_ALLOWLIST = Path(__file__).resolve().with_name("public_template_content_allowlist.txt")
MOCK_DIRECTORY_NAMES = {"public_mock", "public-mock"}
PRAGMA = re.compile(r"public-template-guard:\s*allow\s+([a-z-]+(?:\s*,\s*[a-z-]+)*)")

RULES = (
    "archive-or-report-binary",
    "unscannable-binary",
    "oversized-file",
    "private-key",
    "credential-token",
    "credential-assignment",
    "credential-url",
    "contact-email",
    "contact-phone",
    "mock-identifier",
    "mock-fixture-unparseable",
)

ARCHIVE_OR_REPORT_EXTENSIONS = {
    ".7z", ".bz2", ".doc", ".docm", ".docx", ".gz", ".jar", ".odp", ".ods", ".odt",
    ".pbit", ".pbix", ".pdf", ".ppt", ".pptm", ".pptx", ".rar", ".tar", ".tbz2",
    ".tgz", ".twb", ".twbx", ".txz", ".whl", ".xls", ".xlsb", ".xlsm", ".xlsx",
    ".xz", ".zip", ".zst",
}
ARCHIVE_OR_REPORT_MAGIC = (
    b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08",  # ZIP and ZIP-based office/report files
    b"%PDF-",
    b"\x1f\x8b",  # gzip
    b"BZh",
    b"\xfd7zXZ\x00",
    b"7z\xbc\xaf\x27\x1c",
    b"Rar!\x1a\x07",
    b"\x28\xb5\x2f\xfd",  # zstd
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",  # legacy office compound document
)
TAR_MAGIC_OFFSET = 257
IMAGE_MAGIC = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".webp": (b"RIFF",),
    ".ico": (b"\x00\x00\x01\x00",),
}

# RFC 2606 / RFC 6761 names that can never belong to a real mailbox.
RESERVED_EMAIL_DOMAINS = ("example.com", "example.net", "example.org")
RESERVED_EMAIL_TLDS = ("example", "invalid", "localhost", "test")

PLACEHOLDER_WORDS = (
    "change_me", "changeme", "dummy", "example", "fake", "mock", "placeholder",
    "redacted", "replace", "sample", "sentinel", "synthetic", "todo", "xxxx", "your",
)
# A value that names what it is ("...-password-...") is a label, not a generated secret.
SELF_DESCRIBING_PLACEHOLDERS = PLACEHOLDER_WORDS + ("pass", "password", "pwd", "secret", "token")

# A header only counts when key material follows it (directly, after escaped or real
# newlines, or after armor headers), so a bare header quoted in code or prose passes.
PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ ){0,4}PRIVATE KEY(?: BLOCK)?-----"
    r"(?:\\[rn]|\s|[A-Za-z-]{1,20}: [^\n]{0,200}\n){0,12}[A-Za-z0-9+/=]{16,}"
)
TOKEN_PATTERNS = (
    re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"),
    re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"(?<![A-Za-z0-9])xox[abeprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{32,}"),
    re.compile(r"(?<![A-Za-z0-9])[rs]k_live_[A-Za-z0-9]{16,}"),
    re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
)
SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9])[A-Za-z0-9_.-]{0,40}"
    r"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)"
    r"[A-Za-z0-9_-]{0,40}[\"']?\s{0,4}[:=]\s{0,4}[\"']?([^\s\"',;]{12,200})"
)
CREDENTIAL_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{1,20}://[^\s/:@\"']{1,80}:([^\s/@\"']{1,200})@")
EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.){1,8}[A-Za-z]{2,24})(?![A-Za-z0-9-])"
)
PHONE_PATTERNS = (
    re.compile(r"(?<![\w.+-])(?:04\d{2}[ .-]?\d{3}[ .-]?\d{3}|(?:\(0[2378]\)|0[2378])[ .-]?\d{4}[ .-]?\d{4}|1[38]00[ .-]?\d{3}[ .-]?\d{3})(?![\w.-])"),
    re.compile(
        r"(?<![\w+.-])\+[1-9]\d{0,2}[ .-]?(?:\(\d{1,4}\)[ .-]?)?\d{1,4}(?:[ .-]?\d{2,4}){1,4}(?![\w-])"
    ),
    re.compile(r"(?<![\w.+-])(?:\(\d{3}\) ?|\d{3}[.-])\d{3}[.-]\d{4}(?![\w.-])"),
)

IDENTIFIER_FIELD = re.compile(
    r"^(?:id|ids|uuid|guid|identifier|.+_(?:id|ids|key|uuid|guid|identifier|number|no|ref|reference))$"
)
ORDINAL_FIELDS = {
    "batch_number", "line_number", "page_number", "row_number", "sequence_number", "version_number",
}
MOCK_VALUE = re.compile(r"^MOCK[-_][A-Za-z0-9][A-Za-z0-9._-]*$")
KEY_VALUE_LINE = re.compile(r"^\s*(?:-\s+)?[\"']?([A-Za-z][A-Za-z0-9_-]*)[\"']?\s*[:=]\s*(.*)$")
NULL_WORDS = {"", "null", "none", "nil", "~"}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    message: str

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"{location}: {self.rule}: {self.message}"


class ScanBoundExceeded(Exception):
    """Raised when the scan would exceed a configured file or byte bound."""


# --------------------------------------------------------------------------- helpers


def is_placeholder(value: str, words: Iterable[str] = PLACEHOLDER_WORDS) -> bool:
    lowered = value.lower()
    return any(re.search(r"(?:^|[^a-z0-9])" + re.escape(word) + r"(?:$|[^a-z0-9])", lowered) for word in words)


def shannon_entropy(value: str) -> float:
    counts = Counter(value)
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def looks_like_secret_literal(value: str) -> bool:
    """Return True for a literal that reads as a generated secret, not a reference."""
    if is_placeholder(value, SELF_DESCRIBING_PLACEHOLDERS):
        return False
    if re.match(r"[A-Za-z_][A-Za-z0-9_.]*\(", value):
        return False
    if any(char in value for char in "{}[]<>") or value.startswith(("$(", "${")) or "::" in value:
        return False
    if value.startswith(("/", "./", "~", "http", "env.", "var.", "os.", "secrets.")):
        return False
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", value):  # environment variable name
        return False
    if not (re.search(r"[A-Za-z]", value) and re.search(r"\d", value)):
        return False
    return shannon_entropy(value) >= 3.0


def is_reserved_email_domain(domain: str) -> bool:
    domain = domain.lower()
    if any(domain == name or domain.endswith("." + name) for name in RESERVED_EMAIL_DOMAINS):
        return True
    return domain.rsplit(".", 1)[-1] in RESERVED_EMAIL_TLDS


def is_fictional_phone(digits: str) -> bool:
    """Return True for reserved fictional (555-01xx) and obvious filler numbers."""
    return "55501" in digits or len(set(digits)) <= 3 or "123456" in digits


def is_mock_fixture_path(relative: str, extra_globs: Iterable[str] = ()) -> bool:
    parts = relative.split("/")
    if any(part.lower() in MOCK_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    return any(fnmatch.fnmatchcase(relative, glob) for glob in extra_globs)


def is_identifier_field(name: str) -> bool:
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).replace("-", "_").lower()
    return snake not in ORDINAL_FIELDS and IDENTIFIER_FIELD.match(snake) is not None


def load_allowlist(path: Path) -> list[tuple[str, str]]:
    entries = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split(None, 1)
        if len(fields) != 2 or fields[0] not in RULES:
            raise ValueError(f"allowlist line {number} must be '<rule-id> <path-glob>'")
        entries.append((fields[0], fields[1].strip()))
    return entries


def windows(line: str) -> Iterator[str]:
    if len(line) <= LINE_WINDOW:
        yield line
        return
    step = LINE_WINDOW - LINE_OVERLAP
    for start in range(0, len(line), step):
        yield line[start:start + LINE_WINDOW]
        if start + LINE_WINDOW >= len(line):
            return


# --------------------------------------------------------------------------- rules


def line_rules(line: str) -> set[str]:
    """Return the contact/credential rule ids that one line of text violates."""
    hits: set[str] = set()
    for text in windows(line):
        if any(
            not (is_placeholder(match.group(0)) or match.group(0) == "AKIA" + "IOSFODNN7EXAMPLE")
            for pattern in TOKEN_PATTERNS
            for match in pattern.finditer(text)
        ):
            hits.add("credential-token")
        if any(looks_like_secret_literal(m.group(1)) for m in SECRET_ASSIGNMENT.finditer(text)):
            hits.add("credential-assignment")
        for match in CREDENTIAL_URL.finditer(text):
            password = unquote(match.group(1))
            if not is_placeholder(password, SELF_DESCRIBING_PLACEHOLDERS) and not any(
                char in password for char in "{}[]<>"
            ):
                hits.add("credential-url")
        for match in EMAIL.finditer(text):
            # ``user@host:path`` is an SSH remote, not a mailbox.
            if re.match(r":[\w~/.]", text[match.end():match.end() + 2]):
                continue
            if not is_reserved_email_domain(match.group(1)):
                hits.add("contact-email")
        for pattern in PHONE_PATTERNS:
            for match in pattern.finditer(text):
                digits = re.sub(r"\D", "", match.group(0))
                if 10 <= len(digits) <= 15 and not is_fictional_phone(digits):
                    hits.add("contact-phone")
    return hits


MESSAGES = {
    "archive-or-report-binary": "archive or report binary; publish source text instead",
    "unscannable-binary": "binary or non-UTF-8 file cannot be checked",
    "oversized-file": f"file exceeds the {MAX_FILE_BYTES}-byte scan bound",
    "private-key": "private key block",
    "credential-token": "vendor-format access token",
    "credential-assignment": "secret assigned a real-looking literal; reference an environment variable",
    "credential-url": "URL embeds a password",
    "contact-email": "email address outside the reserved example domains",
    "contact-phone": "phone number outside the reserved fictional ranges",
    "mock-identifier": "identifier in a public mock fixture must start with MOCK- or MOCK_",
    "mock-fixture-unparseable": "public mock fixture could not be parsed",
}


def _bad_mock_value(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    text = str(value).strip()
    return text.lower() not in NULL_WORDS and MOCK_VALUE.match(text) is None


def _json_identifier_violations(node: object, under_identifier: bool = False) -> int:
    if isinstance(node, dict):
        return sum(
            _json_identifier_violations(value, is_identifier_field(str(key)))
            for key, value in node.items()
        )
    if isinstance(node, list):
        return sum(_json_identifier_violations(item, under_identifier) for item in node)
    return int(under_identifier and _bad_mock_value(node))


def mock_identifier_lines(relative: str, text: str) -> tuple[list[int], bool]:
    """Return (line numbers with non-MOCK identifiers, parsed) for a mock fixture.

    Line 0 means the violation could not be tied to one line (multi-line JSON).
    """
    suffix = Path(relative).suffix.lower()
    lines = text.splitlines()
    if suffix == ".json":
        try:
            return ([0] if _json_identifier_violations(json.loads(text)) else []), True
        except (ValueError, RecursionError):
            return [], False
    if suffix in {".jsonl", ".ndjson"}:
        bad = []
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                if _json_identifier_violations(json.loads(line)):
                    bad.append(number)
            except (ValueError, RecursionError):
                return bad, False
        return bad, True
    if suffix in {".csv", ".tsv"}:
        bad = []
        try:
            reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t" if suffix == ".tsv" else ",")
            header = next(reader, [])
            columns = [i for i, name in enumerate(header) if is_identifier_field(name.strip())]
            for row in reader:
                if any(i < len(row) and _bad_mock_value(row[i]) for i in columns):
                    bad.append(reader.line_num)
        except csv.Error:
            return bad, False
        return bad, True
    # YAML, TOML, Markdown and other text: judge ``key: value`` / ``key = value`` lines.
    bad = []
    for number, line in enumerate(lines, start=1):
        match = KEY_VALUE_LINE.match(line)
        if not match or not is_identifier_field(match.group(1)):
            continue
        value = match.group(2).split(" #", 1)[0].strip().rstrip(",").strip()
        items = value[1:-1].split(",") if value.startswith("[") and value.endswith("]") else [value]
        if any(_bad_mock_value(item.strip().strip("\"'")) for item in items):
            bad.append(number)
    return bad, True


def scan_content(relative: str, data: bytes, mock_globs: Iterable[str] = ()) -> list[Finding]:
    """Return findings for one file's bytes. ``relative`` is its POSIX path in the tree."""
    suffix = Path(relative).suffix.lower()
    if (
        suffix in ARCHIVE_OR_REPORT_EXTENSIONS
        or data.startswith(ARCHIVE_OR_REPORT_MAGIC)
        or data[TAR_MAGIC_OFFSET:TAR_MAGIC_OFFSET + 5] == b"ustar"
    ):
        return [Finding(relative, 0, "archive-or-report-binary", MESSAGES["archive-or-report-binary"])]
    if suffix in IMAGE_MAGIC and data.startswith(IMAGE_MAGIC[suffix]):
        return []
    try:
        if b"\x00" in data:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "NUL byte")
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [Finding(relative, 0, "unscannable-binary", MESSAGES["unscannable-binary"])]

    found: dict[int, set[str]] = {}
    lines = text.splitlines()
    for number, line in enumerate(lines, start=1):
        hits = line_rules(line)
        if hits:
            found.setdefault(number, set()).update(hits)
    for match in PRIVATE_KEY.finditer(text):
        found.setdefault(text.count("\n", 0, match.start()) + 1, set()).add("private-key")
    if is_mock_fixture_path(relative, mock_globs):
        bad_lines, parsed = mock_identifier_lines(relative, text)
        for number in bad_lines:
            found.setdefault(number, set()).add("mock-identifier")
        if not parsed:
            found.setdefault(0, set()).add("mock-fixture-unparseable")

    findings = []
    for number in sorted(found):
        allowed: set[str] = set()
        if number:
            for pragma in PRAGMA.finditer(lines[number - 1]):
                allowed.update(rule.strip() for rule in pragma.group(1).split(","))
        findings.extend(
            Finding(relative, number, rule, MESSAGES[rule])
            for rule in sorted(found[number] - allowed)
        )
    return findings


# --------------------------------------------------------------------------- tree scan


def tracked_or_walked_files(root: Path) -> Iterator[str]:
    """Yield POSIX relative paths: tracked and non-ignored untracked files when root is a checkout, else a walk."""
    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"], capture_output=True, check=False,
        )
        if result.returncode == 0:
            yield from sorted(name for name in result.stdout.decode("utf-8", "surrogateescape").split("\0") if name)
            return
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if name != ".git")
        for name in sorted(files):
            yield (Path(directory) / name).relative_to(root).as_posix()


def scan_tree(
    root: Path,
    mock_globs: Iterable[str] = (),
    allowlist: Iterable[tuple[str, str]] = (),
    *,
    max_files: int = MAX_FILES,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_findings: int = MAX_FINDINGS,
) -> tuple[list[Finding], bool]:
    """Scan ``root`` and return (findings, truncated). Symlinks are not followed."""
    mock_globs = tuple(mock_globs)
    allowlist = tuple(allowlist)
    findings: list[Finding] = []
    total = 0
    for count, relative in enumerate(tracked_or_walked_files(root), start=1):
        if count > max_files:
            raise ScanBoundExceeded(f"more than {max_files} files")
        path = root / relative
        if path.is_symlink() or not path.is_file():
            continue
        size = path.stat().st_size
        if size > max_file_bytes:
            results = [Finding(relative, 0, "oversized-file", MESSAGES["oversized-file"])]
            if Path(relative).suffix.lower() in ARCHIVE_OR_REPORT_EXTENSIONS:
                results = scan_content(relative, b"", mock_globs)
        else:
            total += size
            if total > max_total_bytes:
                raise ScanBoundExceeded(f"more than {max_total_bytes} bytes")
            results = scan_content(relative, path.read_bytes(), mock_globs)
        for finding in results:
            if any(
                rule == finding.rule and fnmatch.fnmatchcase(relative, glob)
                for rule, glob in allowlist
            ):
                continue
            if len(findings) >= max_findings:
                return findings, True
            findings.append(finding)
    return findings, False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reject contact/credential material, archive/report binaries, and "
        "non-MOCK identifiers in public mock fixtures. See the module docstring for "
        "bounds and false-positive handling.",
    )
    parser.add_argument(
        "root", type=Path, nargs="?", default=Path(__file__).resolve().parents[1],
        help="tree to scan (default: this repository); Git-tracked files when it is a checkout",
    )
    parser.add_argument(
        "--mock-path", action="append", default=[], metavar="GLOB",
        help="extra public mock fixture path glob, relative to root (repeatable)",
    )
    parser.add_argument(
        "--allowlist", type=Path,
        help="file of '<rule-id> <path-glob>' false-positive entries "
        f"(default: {DEFAULT_ALLOWLIST.name} next to this script, when present)",
    )
    args = parser.parse_args(argv)
    root = args.root
    if root.is_symlink() or not root.is_dir():
        print("Scan root is missing or invalid.", file=sys.stderr)
        return 2
    allowlist_path = args.allowlist or (DEFAULT_ALLOWLIST if DEFAULT_ALLOWLIST.is_file() else None)
    try:
        allowlist = load_allowlist(allowlist_path) if allowlist_path else []
    except (OSError, UnicodeDecodeError, ValueError) as error:
        print(f"Allowlist could not be read: {error}", file=sys.stderr)
        return 2
    try:
        findings, truncated = scan_tree(root, args.mock_path, allowlist)
    except ScanBoundExceeded as error:
        print(f"Scan bound exceeded: {error}.", file=sys.stderr)
        return 2
    except OSError:
        print("Scan root could not be inspected.", file=sys.stderr)
        return 2
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    if truncated:
        print(f"Stopped after {MAX_FINDINGS} findings; more may remain.", file=sys.stderr)
    if findings:
        print(
            f"{len(findings)} public template content finding(s). "
            "See the false-positive notes in scripts/check_public_template_content.py.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
