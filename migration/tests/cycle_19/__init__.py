"""Cycle 19 local resource budget tests (cards 321-325).

This file makes ``unittest discover -s migration/tests`` collect the package.
"""

import sys
from pathlib import Path

MIGRATION_ROOT = Path(__file__).resolve().parents[2]
if str(MIGRATION_ROOT) not in sys.path:
    sys.path.insert(0, str(MIGRATION_ROOT))
