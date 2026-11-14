"""Offline tests for the fail-closed PostgreSQL snapshot guard.

Every connection here is an in-process fake that imitates the documented PostgreSQL
statements the guard issues. Nothing opens a socket or contacts a database.
"""

from __future__ import annotations

import json
import sys
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "migration"))

import postgres_snapshot_guard as guard_module  # noqa: E402
from postgres_snapshot_guard import (  # noqa: E402
    CHANGED,
    INTERRUPTED,
    UNSUPPORTED,
    UNVERIFIED,
    VERIFIED,
    PostgresSnapshotGuard,
    SnapshotGuardError,
    consistency_ready,
    require_operator_gate,
    sql_source_family,
    unsupported_source_evidence,
)

SNAPSHOT_ID = "0000000A-0000001B-1"
SNAPSHOT_TEXT = "1234:1240:1235,1237"


class FakeServer:
    def __init__(self, *, version=160002, in_recovery=False, snapshot_text=SNAPSHOT_TEXT,
                 snapshot_id=SNAPSHOT_ID):
        self.version = version
        self.in_recovery = in_recovery
        self.snapshot_text = snapshot_text
        self.snapshot_id = snapshot_id
        self.exported_open = False


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._row = None
        self.closed = False

    def execute(self, sql):
        conn = self.conn
        conn.statements.append(sql)
        if conn.fail_on and conn.fail_on in sql:
            raise conn.fail_exc
        if conn.lost:
            raise RuntimeError(f"connection lost while using {conn.server.snapshot_id}")
        if sql.startswith("SET TRANSACTION ISOLATION LEVEL"):
            if conn.queried:
                raise RuntimeError("SET TRANSACTION ISOLATION LEVEL must be called before any query")
            if not conn.autocommit:
                conn.isolation, conn.read_only = "repeatable read", "on"
            self._row = None
        elif sql.startswith("SET TRANSACTION SNAPSHOT"):
            literal = sql.split("'")[1]
            if conn.queried or literal != conn.server.snapshot_id or not conn.server.exported_open:
                raise RuntimeError(f'invalid snapshot identifier: "{literal}"')
            conn.imported = True
            self._row = None
        elif "server_version_num" in sql:
            conn.queried = True
            self._row = (conn.server.version, conn.server.in_recovery, conn.isolation, conn.read_only)
        elif "pg_export_snapshot" in sql:
            conn.queried = True
            conn.server.exported_open = True
            conn.exporter = True
            self._row = (conn.server.snapshot_id, conn.server.snapshot_text)
        elif "pg_current_snapshot" in sql:
            conn.queried = True
            self._row = None if conn.empty_result else (
                conn.isolation, conn.read_only, conn.snapshot_override or conn.server.snapshot_text)
        else:
            conn.queried = True
            self._row = (1,)

    def fetchone(self):
        return self._row

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, server, *, fail_on=None, fail_exc=None, autocommit=False):
        self.server = server
        self.statements: list[str] = []
        self.fail_on = fail_on
        self.fail_exc = fail_exc or RuntimeError(f"driver detail {server.snapshot_id}")
        self.autocommit = autocommit
        self.isolation, self.read_only = "read committed", "off"
        self.queried = False
        self.imported = False
        self.exporter = False
        self.lost = False
        self.empty_result = False
        self.snapshot_override = None
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.rollbacks += 1
        self.queried = False
        self.isolation, self.read_only = "read committed", "off"
        if self.exporter:
            self.server.exported_open = False
            self.exporter = False

    def close(self):
        self.closed = True
        if self.exporter:
            self.server.exported_open = False


def _opened(server=None):
    server = server or FakeServer()
    guard = PostgresSnapshotGuard()
    coordinator = FakeConnection(server)
    guard.open(coordinator)
    return guard, server, coordinator


def _leaks(value, server=None) -> bool:
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    markers = [SNAPSHOT_ID, SNAPSHOT_TEXT] + ([server.snapshot_id, server.snapshot_text] if server else [])
    return any(marker in text for marker in markers)


def _traceback_with_locals(exc: BaseException) -> str:
    """Render the exception the way locals-capturing error reporters would."""
    return "".join(traceback.TracebackException.from_exception(exc, capture_locals=True).format())


class HappyPathTests(unittest.TestCase):
    def test_verified_only_after_participants_and_confirmation(self):
        guard, server, coordinator = _opened()
        self.assertEqual(guard.state, UNVERIFIED)
        self.assertFalse(consistency_ready(guard.evidence()))
        for _ in range(3):
            participant = FakeConnection(server)
            participant.queried = True  # e.g. a connect hook already ran a SELECT
            guard.bind(participant)
            self.assertTrue(participant.imported)
            self.assertGreaterEqual(participant.rollbacks, 1)
            self.assertEqual(participant.statements[0], "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            self.assertTrue(participant.statements[1].startswith("SET TRANSACTION SNAPSHOT"))
        self.assertFalse(consistency_ready(guard.evidence()))
        guard.confirm()
        guard.close()
        self.assertEqual(guard.state, VERIFIED)
        evidence = guard.evidence()
        self.assertTrue(consistency_ready(evidence))
        self.assertEqual(evidence["source_consistency"]["participants_verified"], 3)
        self.assertTrue(coordinator.closed)
        self.assertGreaterEqual(coordinator.rollbacks, 2)
        self.assertFalse(server.exported_open)

    def test_coordinator_uses_read_only_repeatable_read(self):
        _, _, coordinator = _opened()
        self.assertEqual(coordinator.statements[0], "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        self.assertTrue(all("DEFERRABLE" not in sql for sql in coordinator.statements))

    def test_bind_after_confirm_is_refused(self):
        guard, server, _ = _opened()
        guard.bind(FakeConnection(server))
        guard.confirm()
        with self.assertRaises(SnapshotGuardError) as ctx:
            guard.bind(FakeConnection(server))
        self.assertEqual(ctx.exception.code, "already_confirmed")
        self.assertEqual(guard.state, VERIFIED)


class UnsupportedTests(unittest.TestCase):
    def _assert_open_fails(self, server, code, state=UNSUPPORTED):
        guard = PostgresSnapshotGuard()
        coordinator = FakeConnection(server)
        with self.assertRaises(SnapshotGuardError) as ctx:
            guard.open(coordinator)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(guard.state, state)
        self.assertTrue(coordinator.closed)
        self.assertFalse(consistency_ready(guard.evidence()))
        return guard

    def test_old_server(self):
        self._assert_open_fails(FakeServer(version=120015), "unsupported_server_version")

    def test_standby(self):
        self._assert_open_fails(FakeServer(in_recovery=True), "unsupported_standby")

    def test_unrecognized_marker_format_never_inlined(self):
        server = FakeServer(snapshot_id="x'; DROP TABLE t; --")
        guard = self._assert_open_fails(server, "unsupported_marker_format")
        self.assertFalse(_leaks(guard.evidence(), server))
        with self.assertRaises(SnapshotGuardError):
            guard.bind(FakeConnection(server))

    def test_non_postgres_engine_rejected(self):
        from sqlalchemy import create_engine

        guard = PostgresSnapshotGuard()
        with self.assertRaises(SnapshotGuardError) as ctx:
            guard.attach(create_engine("sqlite://"))
        self.assertEqual(ctx.exception.code, "unsupported_engine")
        self.assertEqual(guard.state, UNSUPPORTED)

    def test_autocommit_participant_is_unsupported(self):
        guard, server, _ = _opened()
        with self.assertRaises(SnapshotGuardError) as ctx:
            guard.bind(FakeConnection(server, autocommit=True))
        self.assertEqual(ctx.exception.code, "participant_mode_mismatch")
        self.assertEqual(guard.state, UNSUPPORTED)

    def test_unsupported_source_evidence_is_not_ready(self):
        self.assertFalse(consistency_ready(unsupported_source_evidence()))
        self.assertEqual(unsupported_source_evidence("bogus")["source_consistency"]["reason"], "unsupported_source_family")
        acknowledged = unsupported_source_evidence(acknowledged=True)
        self.assertTrue(acknowledged["source_consistency"]["operator_acknowledged_unverified"])
        self.assertFalse(consistency_ready(acknowledged))


class ChangedTests(unittest.TestCase):
    def test_participant_snapshot_mismatch(self):
        guard, server, coordinator = _opened()
        participant = FakeConnection(server)
        participant.snapshot_override = "1234:1299:"
        with self.assertRaises(SnapshotGuardError) as ctx:
            guard.bind(participant)
        self.assertEqual(ctx.exception.code, "participant_snapshot_mismatch")
        self.assertEqual(guard.state, CHANGED)
        with self.assertRaises(SnapshotGuardError) as again:
            guard.bind(FakeConnection(server))
        self.assertEqual(again.exception.code, "already_failed")
        with self.assertRaises(SnapshotGuardError):
            guard.confirm()
        guard.close()
        self.assertEqual(guard.state, CHANGED)
        self.assertTrue(coordinator.closed)
        self.assertFalse(consistency_ready(guard.evidence()))

    def test_coordinator_snapshot_changed_at_confirmation(self):
        guard, server, coordinator = _opened()
        guard.bind(FakeConnection(server))
        coordinator.snapshot_override = "9999:9999:"
        with self.assertRaises(SnapshotGuardError) as ctx:
            guard.confirm()
        self.assertEqual(ctx.exception.code, "coordinator_snapshot_changed")
        self.assertEqual(guard.state, CHANGED)


class InterruptedTests(unittest.TestCase):
    def test_import_failure_after_exporter_ended(self):
        guard, server, coordinator = _opened()
        coordinator.rollback()  # exporting transaction ended underneath the guard
        participant = FakeConnection(server)
        with self.assertRaises(SnapshotGuardError) as ctx:
            guard.bind(participant)
        self.assertEqual(ctx.exception.code, "participant_import_failed")
        self.assertEqual(guard.state, INTERRUPTED)
        self.assertIsNone(ctx.exception.__cause__)
        self.assertIsNone(ctx.exception.__context__)
        self.assertFalse(_leaks(str(ctx.exception), server))
        self.assertFalse(_leaks(_traceback_with_locals(ctx.exception), server))

    def test_coordinator_lost_before_confirmation(self):
        guard, server, coordinator = _opened()
        guard.bind(FakeConnection(server))
        coordinator.lost = True
        with self.assertRaises(SnapshotGuardError) as ctx:
            guard.confirm()
        self.assertEqual(ctx.exception.code, "coordinator_lost")
        self.assertEqual(guard.state, INTERRUPTED)
        self.assertIsNone(ctx.exception.__context__)
        self.assertFalse(_leaks(_traceback_with_locals(ctx.exception), server))

    def test_confirm_without_participants(self):
        guard, _, _ = _opened()
        with self.assertRaises(SnapshotGuardError) as ctx:
            guard.confirm()
        self.assertEqual(ctx.exception.code, "no_participants")
        self.assertEqual(guard.state, INTERRUPTED)

    def test_confirm_before_open(self):
        guard = PostgresSnapshotGuard()
        with self.assertRaises(SnapshotGuardError) as ctx:
            guard.confirm()
        self.assertEqual(ctx.exception.code, "not_opened")
        self.assertEqual(guard.state, UNVERIFIED)
        self.assertFalse(consistency_ready(guard.evidence()))

    def test_close_without_confirm_is_interrupted(self):
        guard, server, coordinator = _opened()
        guard.bind(FakeConnection(server))
        guard.close()
        guard.close()  # idempotent
        self.assertEqual(guard.state, INTERRUPTED)
        self.assertTrue(coordinator.closed)
        self.assertFalse(consistency_ready(guard.evidence()))

    def test_keyboard_interrupt_during_bind_propagates_and_fails_closed(self):
        guard, server, _ = _opened()
        participant = FakeConnection(server, fail_on="pg_current_snapshot", fail_exc=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            guard.bind(participant)
        self.assertEqual(guard.state, INTERRUPTED)

    def test_keyboard_interrupt_during_open(self):
        guard = PostgresSnapshotGuard()
        coordinator = FakeConnection(FakeServer(), fail_on="pg_export_snapshot", fail_exc=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            guard.open(coordinator)
        self.assertEqual(guard.state, INTERRUPTED)
        self.assertTrue(coordinator.closed)

    def test_driver_error_during_open_is_sanitized(self):
        server = FakeServer()
        guard = PostgresSnapshotGuard()
        coordinator = FakeConnection(server, fail_on="pg_export_snapshot")
        with self.assertRaises(SnapshotGuardError) as ctx:
            guard.open(coordinator)
        self.assertEqual(ctx.exception.code, "coordinator_setup_failed")
        self.assertIsNone(ctx.exception.__context__)
        self.assertFalse(_leaks(_traceback_with_locals(ctx.exception), server))
        self.assertEqual(guard.state, INTERRUPTED)

    def test_context_manager_marks_exception_as_interrupted(self):
        server = FakeServer()
        coordinator = FakeConnection(server)
        with self.assertRaises(ValueError):
            with PostgresSnapshotGuard() as guard:
                guard.open(coordinator)
                guard.bind(FakeConnection(server))
                raise ValueError("extraction failed")
        self.assertEqual(guard.state, INTERRUPTED)
        self.assertTrue(coordinator.closed)
        self.assertFalse(server.exported_open)

    def test_coordinator_close_errors_are_swallowed(self):
        guard, server, coordinator = _opened()

        def boom():
            raise RuntimeError(SNAPSHOT_ID)

        coordinator.rollback = boom
        coordinator.close = boom
        guard.close()
        self.assertEqual(guard.state, INTERRUPTED)


class PrivacyTests(unittest.TestCase):
    def test_marker_never_in_repr_evidence_or_errors(self):
        guard, server, coordinator = _opened()
        guard.bind(FakeConnection(server))
        self.assertFalse(_leaks(repr(guard), server))
        self.assertFalse(_leaks(guard.evidence(), server))
        guard.confirm()
        self.assertFalse(_leaks(guard.evidence(), server))
        self.assertFalse(_leaks(json.dumps(vars(guard), default=lambda _: "<object>"), server))
        for message in guard_module.REASONS.values():
            self.assertFalse(_leaks(message, server))

    def test_evidence_fields_are_allowlisted(self):
        guard, server, _ = _opened()
        guard.bind(FakeConnection(server))
        guard.confirm()
        evidence = guard.evidence()["source_consistency"]
        self.assertEqual(set(evidence), {"state", "mechanism", "reason", "participants_verified",
                                         "operator_acknowledged_unverified", "consistency_ready"})


class ReadinessGateTests(unittest.TestCase):
    def _verified(self):
        guard, server, _ = _opened()
        guard.bind(FakeConnection(server))
        guard.confirm()
        return guard.evidence()

    def test_malformed_or_tampered_evidence_is_not_ready(self):
        good = self._verified()
        self.assertTrue(consistency_ready(good))
        cases = [None, {}, [], {"source_consistency": None}]
        for key, value in [("state", "VERIFIED"), ("consistency_ready", "true"), ("participants_verified", 0),
                           ("participants_verified", True), ("mechanism", None), ("reason", "interrupted"),
                           ("operator_acknowledged_unverified", True)]:
            inner = dict(good["source_consistency"], **{key: value})
            cases.append({"source_consistency": inner})
        cases.append({"source_consistency": dict(good["source_consistency"], extra="x")})
        for case in cases:
            with self.subTest(case=case):
                self.assertFalse(consistency_ready(case))


class OperatorGateTests(unittest.TestCase):
    def test_unguarded_postgres_requires_acknowledgement(self):
        with self.assertRaises(SnapshotGuardError) as ctx:
            require_operator_gate("postgresql", acknowledged=False)
        self.assertEqual(ctx.exception.code, "source_snapshot_unverifiable_not_acknowledged")
        require_operator_gate("postgresql", acknowledged=True)

    def test_guarded_postgres_needs_no_acknowledgement_and_rejects_one(self):
        require_operator_gate("postgresql", acknowledged=False, guard_enabled=True)
        with self.assertRaises(SnapshotGuardError) as ctx:
            require_operator_gate("postgresql", acknowledged=True, guard_enabled=True)
        self.assertEqual(ctx.exception.code, "source_snapshot_acknowledgement_not_applicable")

    def test_acknowledgement_not_applicable_to_other_families(self):
        for family in ("csv", "sqlite"):
            require_operator_gate(family, acknowledged=False)
            with self.subTest(family=family), self.assertRaises(SnapshotGuardError) as ctx:
                require_operator_gate(family, acknowledged=True)
            self.assertEqual(ctx.exception.code, "source_snapshot_acknowledgement_not_applicable")

    def test_unknown_family_and_backend_fail_closed(self):
        with self.assertRaises(ValueError):
            require_operator_gate("mysql", acknowledged=True)
        self.assertEqual(sql_source_family("postgresql"), "postgresql")
        self.assertEqual(sql_source_family("sqlite"), "sqlite")
        with self.assertRaises(ValueError):
            sql_source_family("mssql")


class SqlAlchemyHookTests(unittest.TestCase):
    def test_begin_hook_binds_each_transaction(self):
        from sqlalchemy import event

        guard, server, _ = _opened()
        bound = []

        class FakeDialect:
            name, driver = "postgresql", "psycopg"

        class FakeEngine:
            dialect = FakeDialect()
            dispatch = None

        engine = FakeEngine()
        listeners = {}
        original_listen, original_contains = event.listen, event.contains
        try:
            event.listen = lambda target, name, fn: listeners.setdefault(name, []).append(fn)
            event.contains = lambda target, name, fn: fn in listeners.get(name, [])
            self.assertIs(guard.attach(engine), engine)
            guard.attach(engine)  # idempotent
        finally:
            event.listen, event.contains = original_listen, original_contains
        self.assertEqual(len(listeners["begin"]), 1)

        class SAConnection:
            def __init__(self, dbapi):
                self.connection = type("Fairy", (), {"dbapi_connection": dbapi})()

        for _ in range(2):
            dbapi = FakeConnection(server)
            listeners["begin"][0](SAConnection(dbapi))
            bound.append(dbapi.imported)
        self.assertEqual(bound, [True, True])
        guard.confirm()
        self.assertEqual(guard.evidence()["source_consistency"]["participants_verified"], 2)

    def test_attach_registers_on_real_engine_without_connecting(self):
        from sqlalchemy import create_engine, event

        # The offline test lock deliberately has no PostgreSQL driver, so a
        # psycopg engine cannot be constructed here. Use a real, lazy SQLAlchemy
        # engine (no connection is opened) and present it as postgresql+psycopg
        # so the real event registry, not a stub, is exercised.
        engine = create_engine("sqlite://")
        engine.dialect.name = "postgresql"
        engine.dialect.driver = "psycopg"
        guard = PostgresSnapshotGuard()
        guard.attach(engine)
        guard.attach(engine)
        self.assertTrue(event.contains(engine, "begin", guard._on_begin))
        self.assertEqual(guard.state, UNVERIFIED)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
