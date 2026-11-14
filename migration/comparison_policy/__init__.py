"""Offline aggregate comparison policy (backlog cycle 17, cards 266-270).

This package decides how two already-computed aggregate metric values relate under an
explicit, versioned policy. It is stdlib-only: it never imports dlt, duckdb, dbt, a loader or
a profile, never reads files, environment variables or the network, and is not wired into
``reconcile_bronze.py``, ``acceptance_report.py`` or any run path.

Modules:

* ``schema`` - outcome enum, precedence, closed reason codes and dimensions, strict policy
  parsing, and the typed evidence input structure.
* ``exact`` - value validation and exact Decimal comparison where absent is not zero.
* ``tolerance`` - explicit absolute/relative Decimal tolerances (no defaults).
* ``compatibility`` - declared-dimension comparability between source and target.
* ``decision`` - deterministic, value-free per-metric and report-level decisions.

Converting an aggregate-reconciliation report (backlog item 224) into
``schema.ComparisonEvidence`` is a documented follow-up and is intentionally not here.
Nothing in this package derives or changes cutover status; every report carries the constant
``cutover_ready: false``.
"""
