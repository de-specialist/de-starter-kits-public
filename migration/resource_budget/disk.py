"""Injectable free-disk probe with reserved headroom (card 319).

``check_headroom`` runs before any write. It needs two supplied limits: the projected
workload's own byte limit (for example ``install.scratch_bytes``) and
``disk.reserved_headroom_bytes``, the space that must stay free after the projected work.

Order of checks, each of which refuses before the next runs:

1. Projected estimate unknown, or projected + headroom overflows: ``estimate_unknown``
   (refused by default, owner decision D3). The probe is not called.
2. Projected estimate above its own limit: ``estimate_exceeds_limit``. The probe is not
   called.
3. Probe unsupported (``probe=None``), raising, or returning anything other than a
   non-negative bounded ``int``: ``probe_failed``. Never treated as "enough space" (F8).
4. Free bytes below projected + headroom: ``insufficient_headroom`` (F7).
5. Otherwise ``probe_headroom_sufficient``: a point-in-time reading, not a guarantee.

The probe dimension reports the bytes that would remain free after the projected work
(clamped at zero), compared against the reserved headroom. The directory is passed only to
the probe and never appears in a result. Nothing here creates, scans, deletes or moves
files, or retries a probe.
"""

from __future__ import annotations

import os
import shutil
from typing import Callable, Union

from . import result as r
from .estimate import Estimate, EstimateInputError, estimate_report
from .schema import BudgetEnvelope

HEADROOM_KEY = "disk.reserved_headroom_bytes"

PathArg = Union[str, "os.PathLike[str]"]
DiskProbe = Callable[[PathArg], int]


def shutil_free_bytes(directory: PathArg) -> int:
    """Default probe: free bytes for the filesystem holding ``directory``.

    Reads filesystem statistics for the one supplied directory only. It does not create the
    directory, list it, or walk anything beneath it.
    """
    return shutil.disk_usage(directory).free


def check_headroom(
    envelope: BudgetEnvelope,
    *,
    projected_key: str,
    projected: Estimate,
    directory: PathArg,
    probe: DiskProbe | None = shutil_free_bytes,
) -> r.BudgetResult:
    if type(envelope) is not BudgetEnvelope:
        raise EstimateInputError("check_headroom needs a BudgetEnvelope.")
    if type(projected) is not Estimate or projected.unit != "bytes":
        raise EstimateInputError("check_headroom needs a projected estimate in bytes.")
    if not isinstance(directory, (str, os.PathLike)):
        raise EstimateInputError("check_headroom needs a directory path.")
    if probe is not None and not callable(probe):
        raise EstimateInputError("check_headroom probe must be callable or None.")
    if projected_key == HEADROOM_KEY:
        raise EstimateInputError("The projected limit must differ from the headroom limit.")

    projected_limit = envelope.limit(projected_key, "bytes")
    headroom = envelope.limit(HEADROOM_KEY, "bytes")
    estimate_dim = estimate_report(
        projected_limit.key, projected_limit.unit, projected_limit.value, projected
    )
    if not projected.known:
        return r.BudgetResult(r.ESTIMATE_UNKNOWN, (estimate_dim,))
    if estimate_dim.status == r.EXCEEDS_LIMIT:
        return r.BudgetResult(r.ESTIMATE_EXCEEDS_LIMIT, (estimate_dim,))
    required = projected.value + headroom.value
    if required > r.MAX_VALUE:
        overflow_dim = r.DimensionReport(
            projected_limit.key,
            projected_limit.unit,
            projected_limit.value,
            r.UNKNOWN,
            None,
            r.STATUS_UNKNOWN,
        )
        return r.BudgetResult(r.ESTIMATE_UNKNOWN, (overflow_dim,))

    free = _read_probe(probe, directory)
    if free is None:
        probe_dim = r.DimensionReport(
            headroom.key, headroom.unit, headroom.value, r.UNKNOWN, None, r.STATUS_UNKNOWN
        )
        return r.BudgetResult(r.PROBE_FAILED, (estimate_dim, probe_dim))
    remaining = max(free - projected.value, 0)
    if free < required:
        status, outcome = r.HEADROOM_INSUFFICIENT, r.INSUFFICIENT_HEADROOM
    else:
        status, outcome = r.HEADROOM_SUFFICIENT_AT_PROBE, r.PROBE_HEADROOM_SUFFICIENT
    probe_dim = r.DimensionReport(
        headroom.key, headroom.unit, headroom.value, r.PROBE, remaining, status
    )
    return r.BudgetResult(outcome, (estimate_dim, probe_dim))


def _read_probe(probe: DiskProbe | None, directory: PathArg) -> int | None:
    """Call the probe once. Any failure or malformed reading is unknown, never "enough"."""
    if probe is None:
        return None
    try:
        free = probe(directory)
    except Exception:  # noqa: BLE001 - any probe failure is reported as unknown, value-free
        return None
    if not r.is_strict_int(free) or not 0 <= free <= r.MAX_VALUE:
        return None
    return free
