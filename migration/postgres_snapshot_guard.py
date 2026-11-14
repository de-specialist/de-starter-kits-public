"""Fail-closed PostgreSQL source snapshot guard (offline-tested; not wired into the loader yet).

Mechanism (PostgreSQL documented behaviour, see docs/source-snapshot-contract.md):

1. A *coordinator* DBAPI connection opens one ``REPEATABLE READ, READ ONLY``
   transaction and calls ``pg_export_snapshot()``. The exported snapshot can be
   imported only while that transaction stays open.
2. Every other transaction on the source engine (inventory, preflight,
   reflection, dlt extraction) must import it with ``SET TRANSACTION SNAPSHOT``
   as its first statement, after ``SET TRANSACTION ISOLATION LEVEL REPEATABLE
   READ, READ ONLY``. Each participant re-verifies isolation, read-only mode, and
   a keyed digest of ``pg_current_snapshot()`` against the coordinator's value.
3. After extraction and before any destination load, ``confirm()`` rechecks the
   coordinator transaction and only then reports ``verified``.

Privacy: the raw snapshot identifier and ``pg_current_snapshot()`` text stay in
process memory only. They are never returned, logged, included in ``repr``, or
placed in evidence. Comparisons use an HMAC with a random per-guard key, and
driver exception text (which can echo the identifier) is discarded.

Readiness: only ``verified`` permits a load; every other state, including the
initial ``unverified`` state and any malformed evidence, keeps readiness false.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
from typing import Any

UNVERIFIED = "unverified"
VERIFIED = "verified"
CHANGED = "changed"
UNSUPPORTED = "unsupported"
INTERRUPTED = "interrupted"
STATES = frozenset({UNVERIFIED, VERIFIED, CHANGED, UNSUPPORTED, INTERRUPTED})
_TERMINAL_FAILURES = frozenset({CHANGED, UNSUPPORTED, INTERRUPTED})

MECHANISM = "postgresql_exported_snapshot_repeatable_read_read_only"
EVIDENCE_KEY = "source_consistency"
_EVIDENCE_FIELDS = frozenset({
    "state", "mechanism", "reason", "participants_verified", "operator_acknowledged_unverified", "consistency_ready",
})

FAMILY_CSV = "csv"
FAMILY_SQLITE = "sqlite"
FAMILY_POSTGRESQL = "postgresql"
SOURCE_FAMILIES = frozenset({FAMILY_CSV, FAMILY_SQLITE, FAMILY_POSTGRESQL})

# pg_current_snapshot() exists from PostgreSQL 13.
MIN_SERVER_VERSION_NUM = 130000

# The identifier format is not documented; accept only a conservative
# hyphen-separated hex shape so it can be inlined as a literal safely
# (SET does not accept server-side bound parameters). Anything else fails closed.
_SNAPSHOT_ID = re.compile(r"[0-9A-Fa-f]{1,16}(?:-[0-9A-Fa-f]{1,16}){1,3}")

REASONS = {
    "not_opened": "Snapshot guard has not been opened.",
    "unsupported_engine": "Snapshot guard supports PostgreSQL via psycopg only.",
    "unsupported_server_version": "PostgreSQL 13 or newer is required for snapshot verification.",
    "unsupported_standby": "Snapshot export from a hot standby is not supported by this guard.",
    "unsupported_marker_format": "Exported snapshot marker has an unrecognized format.",
    "coordinator_setup_failed": "Snapshot coordinator transaction could not be established.",
    "participant_import_failed": "A source transaction could not import the coordinator snapshot.",
    "participant_mode_mismatch": "A source transaction is not repeatable-read and read-only.",
    "participant_snapshot_mismatch": "A source transaction observed a different snapshot.",
    "coordinator_lost": "Snapshot coordinator transaction ended before confirmation.",
    "coordinator_snapshot_changed": "Snapshot coordinator observed a different snapshot at confirmation.",
    "no_participants": "No source transaction was bound to the snapshot.",
    "interrupted": "Snapshot guard was interrupted.",
    "already_failed": "Snapshot guard already failed closed.",
    "already_confirmed": "Snapshot guard is already confirmed; no further transactions may bind.",
    "unsupported_source_family": "Source family has no loader-verifiable snapshot.",
    "source_snapshot_unverifiable_not_acknowledged":
        "PostgreSQL load refused: snapshot guard is not enabled and the operator has not acknowledged an unverified source snapshot.",
    "source_snapshot_acknowledgement_not_applicable":
        "The unverified-snapshot acknowledgement applies only to PostgreSQL sources without the snapshot guard.",
}


class SnapshotGuardError(RuntimeError):
    """Fixed-message failure; never carries driver text or marker values."""

    def __init__(self, code: str):
        self.code = code if code in REASONS else "interrupted"
        super().__init__(REASONS[self.code])


def _fetch_one(cursor) -> tuple:
    row = cursor.fetchone()
    if row is None:
        raise SnapshotGuardError("coordinator_setup_failed")
    return tuple(row)


def _is_on(value: Any) -> bool:
    return str(value).strip().casefold() in {"on", "true", "1"}


class PostgresSnapshotGuard:
    """Bind every source transaction to one exported, read-only snapshot."""

    def __init__(self) -> None:
        self._key = os.urandom(32)
        self._lock = threading.Lock()
        self._state = UNVERIFIED
        self._reason: str | None = "not_opened"
        self._coordinator = None
        self._snapshot_id: str | None = None
        self._snapshot_digest: bytes | None = None
        self._participants = 0
        self._confirmed = False

    def __repr__(self) -> str:  # never expose the marker
        return f"PostgresSnapshotGuard(state={self._state!r}, participants={self._participants})"

    # -- state -----------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def reason(self) -> str | None:
        return self._reason

    def _fail(self, state: str, code: str) -> SnapshotGuardError:
        with self._lock:
            if self._state not in _TERMINAL_FAILURES:
                self._state = state
                self._reason = code
            self._snapshot_id = None
        return SnapshotGuardError(code)

    def _digest(self, snapshot_text: Any) -> bytes:
        return hmac.new(self._key, str(snapshot_text).encode("utf-8"), hashlib.sha256).digest()

    def _require_usable(self) -> None:
        if self._state in _TERMINAL_FAILURES:
            raise SnapshotGuardError("already_failed")
        if self._confirmed:
            raise SnapshotGuardError("already_confirmed")
        if self._coordinator is None or self._snapshot_id is None:
            raise SnapshotGuardError("not_opened")

    # -- coordinator -----------------------------------------------------
    def open(self, coordinator_connection) -> None:
        """Start the exporting transaction on a dedicated DBAPI connection.

        The caller must keep that connection out of the pool until ``close``.
        """
        if self._coordinator is not None or self._state != UNVERIFIED:
            raise SnapshotGuardError("already_failed" if self._state in _TERMINAL_FAILURES else "coordinator_setup_failed")
        self._coordinator = coordinator_connection
        setup_failed = False
        try:
            coordinator_connection.rollback()  # discard any implicit transaction left by connect hooks
            cursor = coordinator_connection.cursor()
            try:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                cursor.execute(
                    "SELECT current_setting('server_version_num')::integer, pg_is_in_recovery(), "
                    "current_setting('transaction_isolation'), current_setting('transaction_read_only')"
                )
                version_num, in_recovery, isolation, read_only = _fetch_one(cursor)
                if int(version_num) < MIN_SERVER_VERSION_NUM:
                    raise self._fail(UNSUPPORTED, "unsupported_server_version")
                if bool(in_recovery):
                    raise self._fail(UNSUPPORTED, "unsupported_standby")
                if str(isolation).casefold() != "repeatable read" or not _is_on(read_only):
                    raise self._fail(INTERRUPTED, "coordinator_setup_failed")
                cursor.execute("SELECT pg_export_snapshot(), pg_current_snapshot()::text")
                snapshot_id, snapshot_text = _fetch_one(cursor)
            finally:
                cursor.close()
        except SnapshotGuardError as exc:
            if self._state == UNVERIFIED:
                self._fail(INTERRUPTED, exc.code)
            self._release_coordinator()
            raise
        except Exception:
            # Raise after the handler so the driver exception is not kept as __context__.
            setup_failed = True
        except BaseException:
            self._fail(INTERRUPTED, "interrupted")
            self._release_coordinator()
            raise
        if setup_failed:
            failure = self._fail(INTERRUPTED, "coordinator_setup_failed")
            self._release_coordinator()
            raise failure
        if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID.fullmatch(snapshot_id):
            snapshot_id = snapshot_text = None  # keep the raw marker out of traceback locals
            failure = self._fail(UNSUPPORTED, "unsupported_marker_format")
            self._release_coordinator()
            raise failure
        with self._lock:
            self._snapshot_id = snapshot_id
            self._snapshot_digest = self._digest(snapshot_text)
            self._reason = None

    # -- participants ----------------------------------------------------
    def bind(self, dbapi_connection) -> None:
        """Import the snapshot as the first statement of a new transaction and verify it."""
        self._require_usable()
        snapshot_id = self._snapshot_id
        if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID.fullmatch(snapshot_id):
            # A concurrent failure cleared the marker; never send anything else to the server.
            raise self._fail(INTERRUPTED, "participant_import_failed")
        # Format validated against _SNAPSHOT_ID: hex digits and hyphens only.
        import_statement = f"SET TRANSACTION SNAPSHOT '{snapshot_id}'"
        snapshot_id = None
        import_failed = False
        row = None
        try:
            dbapi_connection.rollback()  # the transaction must not have run a query yet
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                cursor.execute(import_statement)
                cursor.execute(
                    "SELECT current_setting('transaction_isolation'), "
                    "current_setting('transaction_read_only'), pg_current_snapshot()::text"
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
        except Exception:
            # Raise after the handler so the driver exception (which may quote the
            # marker) is not kept as __context__.
            import_failed = True
        except BaseException:
            self._fail(INTERRUPTED, "interrupted")
            raise
        finally:
            import_statement = None  # keep the raw marker out of traceback locals
        if import_failed:
            raise self._fail(INTERRUPTED, "participant_import_failed")
        if row is None:
            raise self._fail(INTERRUPTED, "participant_import_failed")
        isolation, read_only, snapshot_text = tuple(row)
        if str(isolation).casefold() != "repeatable read" or not _is_on(read_only):
            raise self._fail(UNSUPPORTED, "participant_mode_mismatch")
        if self._snapshot_digest is None or not hmac.compare_digest(self._digest(snapshot_text), self._snapshot_digest):
            raise self._fail(CHANGED, "participant_snapshot_mismatch")
        with self._lock:
            self._participants += 1

    def attach(self, engine):
        """Register a SQLAlchemy ``begin`` hook so every new transaction binds first.

        Returns the engine so it can be used as a dlt ``engine_adapter_callback``.
        """
        dialect = getattr(engine, "dialect", None)
        if getattr(dialect, "name", None) != "postgresql" or getattr(dialect, "driver", None) != "psycopg":
            raise self._fail(UNSUPPORTED, "unsupported_engine")
        from sqlalchemy import event

        if not event.contains(engine, "begin", self._on_begin):
            event.listen(engine, "begin", self._on_begin)
        return engine

    def _on_begin(self, connection) -> None:
        self.bind(connection.connection.dbapi_connection)

    # -- completion ------------------------------------------------------
    def confirm(self) -> None:
        """After extraction, before load: recheck the coordinator and mark verified."""
        self._require_usable()
        if self._participants == 0:
            raise self._fail(INTERRUPTED, "no_participants")
        coordinator_lost = False
        row = None
        try:
            cursor = self._coordinator.cursor()
            try:
                cursor.execute(
                    "SELECT current_setting('transaction_isolation'), "
                    "current_setting('transaction_read_only'), pg_current_snapshot()::text"
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
        except Exception:
            # Raise after the handler so the driver exception is not kept as __context__.
            coordinator_lost = True
        except BaseException:
            self._fail(INTERRUPTED, "interrupted")
            raise
        if coordinator_lost or row is None:
            raise self._fail(INTERRUPTED, "coordinator_lost")
        isolation, read_only, snapshot_text = tuple(row)
        if str(isolation).casefold() != "repeatable read" or not _is_on(read_only):
            raise self._fail(INTERRUPTED, "coordinator_lost")
        if not hmac.compare_digest(self._digest(snapshot_text), self._snapshot_digest or b""):
            raise self._fail(CHANGED, "coordinator_snapshot_changed")
        with self._lock:
            if self._state in _TERMINAL_FAILURES:
                raise SnapshotGuardError("already_failed")
            self._confirmed = True
            self._state = VERIFIED
            self._reason = None
            self._snapshot_id = None

    def close(self) -> None:
        """Roll back and close the coordinator. Idempotent; never raises driver text."""
        self._release_coordinator()
        if self._state == UNVERIFIED and self._reason is None:
            self._reason = "interrupted"
            self._state = INTERRUPTED

    def _release_coordinator(self) -> None:
        coordinator, self._coordinator = self._coordinator, None
        self._snapshot_id = None
        if coordinator is None:
            return
        for action in ("rollback", "close"):
            try:
                getattr(coordinator, action)()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None and self._state not in _TERMINAL_FAILURES and self._state != VERIFIED:
            self._fail(INTERRUPTED, "interrupted")
        self.close()

    # -- evidence --------------------------------------------------------
    def evidence(self) -> dict[str, object]:
        """Sanitized, allowlisted evidence. Contains no marker, digest, or URL."""
        return {
            EVIDENCE_KEY: {
                "state": self._state,
                "mechanism": MECHANISM,
                "reason": self._reason,
                "participants_verified": self._participants,
                "operator_acknowledged_unverified": False,
                "consistency_ready": self._state == VERIFIED,
            }
        }


def unsupported_source_evidence(reason: str = "unsupported_source_family", *,
                                acknowledged: bool = False) -> dict[str, object]:
    """Evidence for a run with no loader-verified snapshot (CSV, SQLite, or unguarded PostgreSQL).

    An operator acknowledgement is recorded but never makes the run consistency-ready.
    """
    return {
        EVIDENCE_KEY: {
            "state": UNSUPPORTED,
            "mechanism": None,
            "reason": reason if reason in REASONS else "unsupported_source_family",
            "participants_verified": 0,
            "operator_acknowledged_unverified": bool(acknowledged),
            "consistency_ready": False,
        }
    }


def sql_source_family(backend_name: str) -> str:
    """Map a SQLAlchemy backend name to a source family; unknown backends fail closed."""
    if backend_name == "sqlite":
        return FAMILY_SQLITE
    if backend_name == "postgresql":
        return FAMILY_POSTGRESQL
    raise ValueError("Only read-only SQLite files and PostgreSQL via psycopg are supported SQL sources.")


def require_operator_gate(family: str, *, acknowledged: bool, guard_enabled: bool = False) -> None:
    """Fail closed before any source connection for a real load.

    PostgreSQL without the snapshot guard requires an explicit operator acknowledgement
    (``--acknowledge-unverified-source-snapshot``) that consistency is unverified. The
    acknowledgement is rejected where it does not apply, so it can never be attached to a
    guarded run or to another family and mistaken for verification. A guarded PostgreSQL run
    still needs ``consistency_ready`` before any load.
    """
    if family not in SOURCE_FAMILIES:
        raise ValueError("Source family is not recognized.")
    needs_ack = family == FAMILY_POSTGRESQL and not guard_enabled
    if needs_ack and not acknowledged:
        raise SnapshotGuardError("source_snapshot_unverifiable_not_acknowledged")
    if acknowledged and not needs_ack:
        raise SnapshotGuardError("source_snapshot_acknowledgement_not_applicable")


def consistency_ready(record: object) -> bool:
    """True only for well-formed evidence whose state is ``verified``. Anything else is False."""
    if not isinstance(record, dict):
        return False
    evidence = record.get(EVIDENCE_KEY)
    if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_FIELDS:
        return False
    participants = evidence.get("participants_verified")
    return (
        evidence.get("state") == VERIFIED
        and evidence.get("mechanism") == MECHANISM
        and evidence.get("reason") is None
        and evidence.get("consistency_ready") is True
        and evidence.get("operator_acknowledged_unverified") is False
        and isinstance(participants, int)
        and not isinstance(participants, bool)
        and participants > 0
    )
