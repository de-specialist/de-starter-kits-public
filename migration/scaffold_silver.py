#!/usr/bin/env python3
"""Launcher for the offline, plan-only Silver starter-model scaffolder (migration/scaffold/).

Plan-only by design: it never creates, modifies, or removes files. See
docs/scaffolder-contract-proposal.md and docs/scaffolder-input-contract.md.
"""

import sys

# Plan-only runs must not leave bytecode caches behind.
sys.dont_write_bytecode = True

from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scaffold.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
