"""Allow `python -m scaffold` from the migration directory."""

import sys

sys.dont_write_bytecode = True

from scaffold.cli import main  # noqa: E402

sys.exit(main())
