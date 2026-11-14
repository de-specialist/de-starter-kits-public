"""Value- and path-free budget result objects (card 320).

Claim kinds are never interchangeable:

* ``limit``: a configured cap with an explicit unit (from ``schema``). It refuses work; it
  never describes consumption.
* ``estimate``: a conservative upper bound computed before work starts (from ``estimate``).
  It can refuse work early; it is never reported as observed and never proves a fit.
* ``observed``: a counter value of units actually admitted (from ``counter``).
* ``probe``: a point-in-time environment reading (from ``disk``). It guarantees nothing
  later; probe races are inherent.
* ``unknown``: a missing input, an arithmetic overflow, or an unsupported or failed probe.
  It is refused and never coerced to zero or to "within budget".

A result carries only limit keys, units, integers, claim kinds and fixed outcome and status
codes. It never carries a path, hostname, username, row or column value, exception text or
repr. It never claims that anything was rolled back or cleaned up: the budget performs no
cleanup, and partial local output may remain after a refusal. It says nothing about
warehouse bytes billed, credits or currency, and it cannot satisfy or alter any human or
cloud gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_VALUE = 2**63 - 1
"""Largest integer any limit, estimate, observation or probe value may hold."""

UNITS = frozenset({"bytes", "rows", "files", "members", "depth", "seconds"})
"""Explicit base units. Values are plain integers; strings such as ``"MB"`` are refused."""

MAX_LIMIT_KEY_LENGTH = 64
LIMIT_KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,3}")

# Claim kinds.
LIMIT = "limit"
ESTIMATE = "estimate"
OBSERVED = "observed"
PROBE = "probe"
UNKNOWN = "unknown"
CLAIM_KINDS = frozenset({LIMIT, ESTIMATE, OBSERVED, PROBE, UNKNOWN})
_DIMENSION_CLAIM_KINDS = frozenset({ESTIMATE, OBSERVED, PROBE, UNKNOWN})

# Phases. ``pre_run`` results come from estimates and probes before any work; ``run``
# results come from counters of admitted units.
PRE_RUN = "pre_run"
RUN = "run"

# Outcome codes. There is deliberately no bare "ok" or "budget_ok".
ESTIMATE_WITHIN_LIMIT = "estimate_within_limit"
ESTIMATE_EXCEEDS_LIMIT = "estimate_exceeds_limit"
ESTIMATE_UNKNOWN = "estimate_unknown"
PROBE_HEADROOM_SUFFICIENT = "probe_headroom_sufficient"
INSUFFICIENT_HEADROOM = "insufficient_headroom"
PROBE_FAILED = "probe_failed"
WITHIN_OBSERVED_LIMITS = "within_observed_limits"
OBSERVED_EXHAUSTION = "observed_exhaustion"

_OUTCOME_PHASE = {
    ESTIMATE_WITHIN_LIMIT: PRE_RUN,
    ESTIMATE_EXCEEDS_LIMIT: PRE_RUN,
    ESTIMATE_UNKNOWN: PRE_RUN,
    PROBE_HEADROOM_SUFFICIENT: PRE_RUN,
    INSUFFICIENT_HEADROOM: PRE_RUN,
    PROBE_FAILED: PRE_RUN,
    WITHIN_OBSERVED_LIMITS: RUN,
    OBSERVED_EXHAUSTION: RUN,
}
OUTCOMES = frozenset(_OUTCOME_PHASE)
REFUSING_OUTCOMES = frozenset(
    {
        ESTIMATE_EXCEEDS_LIMIT,
        ESTIMATE_UNKNOWN,
        INSUFFICIENT_HEADROOM,
        PROBE_FAILED,
        OBSERVED_EXHAUSTION,
    }
)

# Per-dimension status codes.
WITHIN_LIMIT = "within_limit"
EXCEEDS_LIMIT = "exceeds_limit"
NEXT_UNIT_REFUSED = "next_unit_refused"
HEADROOM_SUFFICIENT_AT_PROBE = "headroom_sufficient_at_probe"
HEADROOM_INSUFFICIENT = "headroom_insufficient"
STATUS_UNKNOWN = "unknown"
_STATUS_CLAIM_KINDS = {
    WITHIN_LIMIT: frozenset({ESTIMATE, OBSERVED}),
    EXCEEDS_LIMIT: frozenset({ESTIMATE}),
    NEXT_UNIT_REFUSED: frozenset({OBSERVED}),
    HEADROOM_SUFFICIENT_AT_PROBE: frozenset({PROBE}),
    HEADROOM_INSUFFICIENT: frozenset({PROBE}),
    STATUS_UNKNOWN: frozenset({UNKNOWN}),
}
STATUSES = frozenset(_STATUS_CLAIM_KINDS)

CLEANUP_NOT_PERFORMED = "not_performed_by_budget"

EXIT_NOT_REFUSED = 0
EXIT_REFUSED = 3
"""Non-zero exit code a caller should use for any refusing outcome (F12)."""


class BudgetResultError(ValueError):
    """An internally inconsistent result was requested. Messages are fixed and value-free."""


def is_strict_int(value: object) -> bool:
    """True only for real ``int`` values; ``bool`` and ``int`` subclasses are refused."""
    return type(value) is int


def _member(value: object, allowed: frozenset[str]) -> bool:
    return type(value) is str and value in allowed


def is_valid_limit_key(key: object) -> bool:
    return (
        type(key) is str
        and len(key) <= MAX_LIMIT_KEY_LENGTH
        and LIMIT_KEY_PATTERN.fullmatch(key) is not None
    )


@dataclass(frozen=True)
class DimensionReport:
    """One budget dimension: which limit, what kind of claim, and how it compares."""

    limit_key: str
    unit: str
    limit: int
    claim_kind: str
    value: int | None
    status: str

    def __post_init__(self) -> None:
        if not is_valid_limit_key(self.limit_key):
            raise BudgetResultError("Dimension limit key is not a valid limit key.")
        if not _member(self.unit, UNITS):
            raise BudgetResultError("Dimension unit is not a supported unit.")
        if not is_strict_int(self.limit) or not 1 <= self.limit <= MAX_VALUE:
            raise BudgetResultError("Dimension limit is not a positive bounded integer.")
        if not _member(self.claim_kind, _DIMENSION_CLAIM_KINDS):
            raise BudgetResultError("Dimension claim kind is not supported.")
        if not _member(self.status, STATUSES):
            raise BudgetResultError("Dimension status is not supported.")
        if self.claim_kind not in _STATUS_CLAIM_KINDS[self.status]:
            raise BudgetResultError("Dimension status does not match its claim kind.")
        if self.claim_kind == UNKNOWN:
            if self.value is not None:
                raise BudgetResultError("An unknown dimension cannot carry a value.")
            return
        if not is_strict_int(self.value) or not 0 <= self.value <= MAX_VALUE:
            raise BudgetResultError("Dimension value is not a non-negative bounded integer.")
        if self.status == WITHIN_LIMIT and self.value > self.limit:
            raise BudgetResultError("A within-limit dimension cannot exceed its limit.")
        if self.status == EXCEEDS_LIMIT and self.value <= self.limit:
            raise BudgetResultError("An exceeding dimension must be above its limit.")
        if self.status == NEXT_UNIT_REFUSED and self.value > self.limit:
            raise BudgetResultError("An observed counter can never exceed its limit.")

    def to_dict(self) -> dict[str, object]:
        """Serialize; the value is keyed by its claim kind so claims stay distinguishable."""
        data: dict[str, object] = {
            "limit_key": self.limit_key,
            "unit": self.unit,
            "limit": self.limit,
            "claim_kind": self.claim_kind,
            "status": self.status,
        }
        if self.claim_kind != UNKNOWN:
            data[self.claim_kind] = self.value
        return data


_HINTS = {
    ESTIMATE_EXCEEDS_LIMIT: (
        "Reduce the planned local workload or raise {keys} in the supplied budget config, "
        "then rerun. No work was started by the budget check."
    ),
    ESTIMATE_UNKNOWN: (
        "Supply every count and width the estimate needs; unknown estimates are refused, "
        "never treated as zero. Limits involved: {keys}."
    ),
    INSUFFICIENT_HEADROOM: (
        "Free local disk space or reduce the planned workload ({keys}), then rerun. "
        "The budget did not delete or move anything."
    ),
    PROBE_FAILED: (
        "The local free-space probe was unsupported or failed, so space is unknown and the "
        "run was refused. Check the target directory exists locally, then rerun. "
        "Limits involved: {keys}."
    ),
    OBSERVED_EXHAUSTION: (
        "Raise {keys} in the supplied budget config or reduce the local workload, then rerun. "
        "Partial local output may be present; the budget did not remove it."
    ),
}


@dataclass(frozen=True)
class BudgetResult:
    """Outcome of one budget check. Fields are value-free by construction."""

    outcome: str
    dimensions: tuple[DimensionReport, ...]

    def __post_init__(self) -> None:
        if not _member(self.outcome, OUTCOMES):
            raise BudgetResultError("Result outcome is not supported.")
        if type(self.dimensions) is not tuple or not self.dimensions:
            raise BudgetResultError("A result needs a non-empty tuple of dimensions.")
        if any(type(item) is not DimensionReport for item in self.dimensions):
            raise BudgetResultError("Result dimensions must be dimension reports.")
        keys = [item.limit_key for item in self.dimensions]
        if len(set(keys)) != len(keys):
            raise BudgetResultError("Result dimensions must name distinct limits.")
        kinds = {item.claim_kind for item in self.dimensions}
        statuses = {item.status for item in self.dimensions}
        if self.phase == RUN and not kinds <= {OBSERVED}:
            raise BudgetResultError("Run-phase results carry only observed dimensions.")
        if self.phase == PRE_RUN and OBSERVED in kinds:
            raise BudgetResultError("Pre-run results cannot carry observed dimensions.")
        required = {
            ESTIMATE_WITHIN_LIMIT: lambda: statuses == {WITHIN_LIMIT} and kinds == {ESTIMATE},
            ESTIMATE_EXCEEDS_LIMIT: lambda: EXCEEDS_LIMIT in statuses,
            ESTIMATE_UNKNOWN: lambda: STATUS_UNKNOWN in statuses,
            PROBE_HEADROOM_SUFFICIENT: lambda: statuses
            == {WITHIN_LIMIT, HEADROOM_SUFFICIENT_AT_PROBE},
            INSUFFICIENT_HEADROOM: lambda: HEADROOM_INSUFFICIENT in statuses
            and statuses <= {WITHIN_LIMIT, HEADROOM_INSUFFICIENT},
            PROBE_FAILED: lambda: statuses == {WITHIN_LIMIT, STATUS_UNKNOWN}
            and PROBE not in kinds,
            WITHIN_OBSERVED_LIMITS: lambda: statuses == {WITHIN_LIMIT},
            OBSERVED_EXHAUSTION: lambda: NEXT_UNIT_REFUSED in statuses,
        }[self.outcome]
        if not required():
            raise BudgetResultError("Result outcome does not match its dimensions.")

    @property
    def phase(self) -> str:
        return _OUTCOME_PHASE[self.outcome]

    @property
    def refused(self) -> bool:
        return self.outcome in REFUSING_OUTCOMES

    @property
    def exit_code(self) -> int:
        return EXIT_REFUSED if self.refused else EXIT_NOT_REFUSED

    @property
    def partial_work_present(self) -> bool | None:
        """Run phase only: whether any unit was admitted before this result.

        ``None`` for pre-run results, where the budget has admitted nothing and cannot know
        what the caller did.
        """
        if self.phase != RUN:
            return None
        return any(item.value for item in self.dimensions)

    @property
    def cleanup(self) -> str:
        """The budget never deletes, moves or rolls back anything, on any outcome."""
        return CLEANUP_NOT_PERFORMED

    @property
    def refusing_limit_keys(self) -> tuple[str, ...]:
        refusing = {EXCEEDS_LIMIT, NEXT_UNIT_REFUSED, HEADROOM_INSUFFICIENT, STATUS_UNKNOWN}
        return tuple(item.limit_key for item in self.dimensions if item.status in refusing)

    @property
    def hint(self) -> str | None:
        """Actionable local hint for refusing outcomes; ``None`` otherwise."""
        template = _HINTS.get(self.outcome)
        if template is None:
            return None
        keys = self.refusing_limit_keys or tuple(item.limit_key for item in self.dimensions)
        return template.format(keys=", ".join(f"`{key}`" for key in keys))

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "phase": self.phase,
            "refused": self.refused,
            "exit_code": self.exit_code,
            "partial_work_present": self.partial_work_present,
            "cleanup": self.cleanup,
            "hint": self.hint,
            "dimensions": [item.to_dict() for item in self.dimensions],
        }


class BudgetExhausted(Exception):
    """Raised when a counter refuses the next unit. Its message is fixed and value-free."""

    def __init__(self, result: BudgetResult) -> None:
        if type(result) is not BudgetResult or result.outcome != OBSERVED_EXHAUSTION:
            raise BudgetResultError("BudgetExhausted needs an observed-exhaustion result.")
        super().__init__("Local resource budget refused the next unit.")
        self.result = result
