"""Local, offline resource budgets (backlog cycle 19, cards 316-320).

The package models caps on local resources only: bytes, rows, files, archive members,
directory depth and wall-clock seconds on the user's machine. It never says anything about
warehouse bytes billed, credits or currency, and no result from it can satisfy or alter a
human or cloud gate.

Modules:

* ``schema``: strict envelope of explicit, positive, bounded integer limits (card 316).
* ``estimate``: conservative pre-run upper bounds from supplied counts and widths (317).
* ``counter``: injectable counters that refuse the next unit before a cap is exceeded (318).
* ``disk``: injectable free-space probe with reserved headroom (319).
* ``result``: value- and path-free result objects and outcome codes (320).

Everything is stdlib only. Nothing here opens a network connection, starts a subprocess,
reads a source, discovers credentials, deletes files, cleans up, or retries. The package is
not wired into ``run_migration.py``; its ``ExtractionBudget`` is unchanged (card 315 owns that
integration decision).

Import it as ``resource_budget.<module>`` with ``migration/`` on ``sys.path``. Never put
``migration/resource_budget`` itself on ``sys.path``.
"""
