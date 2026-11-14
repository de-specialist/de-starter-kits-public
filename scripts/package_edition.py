#!/usr/bin/env python3
"""Create a clean edition archive without local profiles, secrets, or artifacts."""

from __future__ import annotations

import argparse
import subprocess
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from credential_safety import credential_findings, is_sensitive_path


IGNORED_PARTS = {
    ".ci",
    ".dbt",
    ".git",
    ".venv",
    "__pycache__",
    "dbt_packages",
    "logs",
    "target",
}

# Repository-level terms copied to each bundle root when present. They are
# optional so packaging keeps working before the licence decision lands.
ROOT_TERMS_FILES = ("LICENSE", "NOTICE", "SUPPORT.md")

# Repository helper scripts copied into each bundle's scripts/ directory.
BUNDLED_SCRIPTS = ("first_run_preflight.py", "terraform_lifecycle.sh")


def include(relative: Path, candidate: Path) -> bool:
    if candidate.is_symlink() or any(part.lower() in IGNORED_PARTS for part in relative.parts):
        return False
    if is_sensitive_path(relative):
        return False
    return candidate.is_file()


def git_ignored_paths(repo_root: Path, source: Path) -> tuple[set[str], tuple[str, ...]]:
    """Return Git-ignored files and directories under source, relative to source.

    Ignored local state such as ``terraform.tfstate.<n>.backup``, ``crash.log``,
    or ``override.tf`` is invisible to ``git status`` and must never be packaged.
    Outside a Git checkout (for example an exported tree) nothing is filtered.
    """
    if not (repo_root / ".git").exists():
        return set(), ()
    command = [
        "git", "-C", str(source), "ls-files", "-z", "--others", "--ignored",
        "--exclude-standard", "--directory", "--full-name", "--", ".",
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"could not list Git-ignored files; refusing to package: {error}") from error
    prefix = source.relative_to(repo_root).as_posix() + "/"
    files: set[str] = set()
    directories: list[str] = []
    for raw in result.stdout.split(b"\0"):
        name = raw.decode("utf-8", "surrogateescape")
        if not name.startswith(prefix):
            continue
        name = name[len(prefix):]
        if name.endswith("/"):
            directories.append(name)
        elif name:
            files.add(name)
    return files, tuple(directories)


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("edition", choices=("bigquery", "snowflake"))
    parser.add_argument("archive", type=Path, help="new .zip path outside this repository")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    source = repo_root / "editions" / ("Bigquery" if args.edition == "bigquery" else "Snowflake")
    archive = args.archive.expanduser().resolve()

    if archive.suffix.lower() != ".zip":
        parser.error("archive path must end in .zip")
    try:
        archive.relative_to(repo_root)
    except ValueError:
        pass
    else:
        parser.error("archive must be written outside the starter-kit repository")
    if archive.exists():
        parser.error("archive already exists; choose a new path")
    checksum_file = Path(f"{archive}.sha256")
    if checksum_file.exists():
        parser.error("checksum file already exists; choose a new archive path")

    ignored_files, ignored_directories = git_ignored_paths(repo_root, source)

    def is_git_ignored(relative: Path) -> bool:
        name = relative.as_posix()
        return name in ignored_files or name.startswith(ignored_directories)

    members = [
        (path, path.relative_to(source))
        for path in sorted(source.rglob("*"))
        if include(path.relative_to(source), path) and not is_git_ignored(path.relative_to(source))
    ]
    for name in ROOT_TERMS_FILES:
        terms_path = repo_root / name
        if terms_path.is_symlink():
            parser.error(f"refusing to package symlinked repository terms file: {name}")
        if not terms_path.exists():
            continue
        if not terms_path.is_file():
            parser.error(f"repository terms path is not a regular file: {name}")
        if any(relative == Path(name) for _, relative in members):
            parser.error(f"edition already contains {name}; remove it or the repository-level copy")
        members.append((terms_path, Path(name)))
    for name in BUNDLED_SCRIPTS:
        relative = Path("scripts") / name
        if any(existing == relative for _, existing in members):
            parser.error(f"edition already contains {relative.as_posix()}; remove it or the repository-level copy")
        members.append((repo_root / "scripts" / name, relative))
    members.sort(key=lambda item: item[1].as_posix())
    credential_payloads = [
        f"{relative.as_posix()} ({', '.join(markers)})"
        for path, relative in members
        if (markers := credential_findings(path))
    ]
    if credential_payloads:
        parser.error(
            "refusing to package credential-like content from included files: "
            + "; ".join(credential_payloads)
        )

    archive.parent.mkdir(parents=True, exist_ok=True)
    root = f"de-starter-kit-{args.edition}"
    manifest = "".join(
        f"{sha256_file(path)}  {(Path(root) / relative).as_posix()}\n"
        for path, relative in members
    )

    def write_member(bundle: ZipFile, name: str, content: bytes, executable: bool = False) -> None:
        info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = ZIP_DEFLATED
        info.create_system = 3
        # Normalise to 0644/0755 so archives stay reproducible while documented
        # commands such as ./scripts/terraform_lifecycle.sh stay executable.
        info.external_attr = ((0o100755 if executable else 0o100644) & 0xFFFF) << 16
        bundle.writestr(info, content, compress_type=ZIP_DEFLATED, compresslevel=9)

    with ZipFile(archive, "x", compression=ZIP_DEFLATED, compresslevel=9) as bundle:
        for path, relative in members:
            write_member(
                bundle,
                (Path(root) / relative).as_posix(),
                path.read_bytes(),
                executable=bool(path.stat().st_mode & 0o111),
            )
        write_member(bundle, (Path(root) / "MANIFEST.sha256").as_posix(), manifest.encode("utf-8"))

    archive_digest = sha256_file(archive)
    checksum_file.write_text(f"{archive_digest}  {archive.name}\n", encoding="utf-8")

    print(f"Packaged {len(members)} files into {archive}")
    print(f"Wrote payload manifest and archive checksum {checksum_file}")
    print("Inspect the archive and complete docs/release-readiness.md before distribution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
