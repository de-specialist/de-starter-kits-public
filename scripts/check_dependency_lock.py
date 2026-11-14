#!/usr/bin/env python3
"""Check direct package constraints against an edition's exact hash-lock pins."""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.version import Version
except ImportError:
    # Python installs commonly include pip even when the dbt environment does
    # not install packaging as a direct dependency.
    from pip._vendor.packaging.markers import default_environment
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.version import Version


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def direct_requirements(path: Path) -> list[Requirement]:
    parsed = []
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.split("#", maxsplit=1)[0].strip()
        if not line:
            continue
        if line.startswith(("-", ".", "/")):
            raise ValueError(
                f"{path}:{line_number}: include, editable, and path directives are "
                "unsupported; declare direct packages by name."
            )
        parsed.append(Requirement(line))
    return parsed


def locked_versions(path: Path) -> dict[str, set[Version]]:
    pins: dict[str, set[Version]] = {}
    pin_pattern = re.compile(
        r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)(?:\s*;.*)?(?:\s*\\)?$"
    )
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        match = pin_pattern.match(line)
        if not match:
            continue
        name, version = match.groups()
        try:
            parsed_version = Version(version)
        except Exception as error:
            raise ValueError(f"{path}:{line_number}: invalid locked version {version!r}") from error
        pins.setdefault(normalize(name), set()).add(parsed_version)
    return pins


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: check_dependency_lock.py REQUIREMENTS LOCK", file=sys.stderr)
        return 2

    requirements_path, lock_path = map(Path, sys.argv[1:])
    roots = direct_requirements(requirements_path)
    pins = locked_versions(lock_path)
    environment = default_environment()
    errors = []

    for requirement in roots:
        if requirement.url:
            errors.append(f"{requirement.name}: direct URL requirements are not supported")
            continue
        if requirement.marker and not requirement.marker.evaluate(environment=environment):
            continue

        versions = pins.get(normalize(requirement.name), set())
        if not versions:
            errors.append(f"{requirement.name}: no exact version pin found in {lock_path}")
            continue
        if not any(requirement.specifier.contains(version, prereleases=True) for version in versions):
            found = ", ".join(sorted(map(str, versions)))
            errors.append(
                f"{requirement.name}: lock pin(s) {found} do not satisfy "
                f"{requirement.specifier or '(any version)'}"
            )

    if errors:
        print("Direct requirements do not match the dependency lock:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        print("Regenerate the lock and commit it with requirements.txt.", file=sys.stderr)
        return 1

    print(f"Direct requirements have compatible exact pins in {lock_path}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(2)
