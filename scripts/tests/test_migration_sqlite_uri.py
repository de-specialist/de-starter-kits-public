"""Offline regression checks for SQLite URI filenames and read-only mode."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "migration"))

from sqlalchemy import URL, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from run_migration import create_read_only_sql_engine  # noqa: E402


class SQLiteUriPathTests(unittest.TestCase):
    def test_uri_delimiters_open_exact_existing_database_read_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sqlite-uri-") as temporary:
            directory = Path(temporary)
            database = directory / "source %2F # ? names.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
                connection.execute("INSERT INTO marker(value) VALUES (?)", ("exact-source-database",))
            original_files = {path.name for path in directory.iterdir()}

            connection_url = URL.create("sqlite", database=str(database)).render_as_string(
                hide_password=False
            )
            engine = create_read_only_sql_engine(connection_url)
            try:
                self.assertEqual(engine.url.query["mode"], "ro")
                self.assertEqual(engine.url.query["immutable"], "1")
                self.assertEqual(engine.url.query["uri"], "true")
                with engine.connect() as connection:
                    self.assertEqual(connection.exec_driver_sql("PRAGMA query_only").scalar_one(), 1)
                    self.assertEqual(
                        connection.execute(text("SELECT value FROM marker")).scalar_one(),
                        "exact-source-database",
                    )
                    with self.assertRaises(OperationalError):
                        connection.execute(text("INSERT INTO marker(value) VALUES ('write-attempt')"))
            finally:
                engine.dispose()

            self.assertEqual({path.name for path in directory.iterdir()}, original_files)
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM marker").fetchall(),
                    [("exact-source-database",)],
                )


if __name__ == "__main__":
    unittest.main()
