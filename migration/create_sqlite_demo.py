#!/usr/bin/env python3
"""Create a disposable SQLite source database from the synthetic retail fixture."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="new SQLite database path outside the repository")
    args = parser.parse_args()
    destination = args.database.expanduser().resolve()
    repo_root = Path(__file__).resolve().parents[1]
    try:
        destination.relative_to(repo_root)
    except ValueError:
        pass
    else:
        parser.error("database must be created outside this repository")
    if destination.exists():
        parser.error("database already exists; choose a new disposable path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    sql_path = Path(__file__).resolve().parent / "examples" / "sqlite_source.sql"
    try:
        with sqlite3.connect(destination) as connection:
            connection.executescript(sql_path.read_text(encoding="utf-8"))
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    print(f"Created synthetic SQLite source with no customer data: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
