"""`python3 -m dashboard_checker`: continuous mode by default, or one scan with
CHECKER_MODE=once. See server.py."""

import sys

from .server import main

sys.exit(main())
