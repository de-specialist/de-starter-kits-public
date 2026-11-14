"""Offline, plan-only Silver starter-model scaffolder (backlog 145).

Modules:
- `reasons`: fixed refusal reason codes.
- `inputs`: input contract (mapping, dry_run, run_manifest admission, fields, bindings).
- `plan`: frozen validated plan dataclasses and the edition renderer registry hook.
- `workspace`: read-only client-workspace and collision checks.
- `cli`: command-line entry point (`python migration/scaffold_silver.py ...`).

The shared migration modules (`_private_json`, `schema_drift`, `evidence_index`,
`mapping_contract`, `destination_type_profiles`) use flat imports, so the `migration/`
directory is placed on `sys.path` here. None of them import dlt, dbt, or warehouse clients.
"""

from __future__ import annotations

import sys
from pathlib import Path

_MIGRATION_DIR = str(Path(__file__).resolve().parent.parent)
if _MIGRATION_DIR not in sys.path:
    sys.path.insert(0, _MIGRATION_DIR)
