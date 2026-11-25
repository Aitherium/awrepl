"""`python -m awrepl` — a way into the REPL that does not need the console script.

A blocked console-script shim (Smart App Control judges each unsigned exe on its
own reputation) must leave another way in. The daemon imports awrepl lazily for
repl_run, and a session with no console script had no CLI at all.
"""

import sys

from awrepl.cli import main

if __name__ == "__main__":
    sys.exit(main())
