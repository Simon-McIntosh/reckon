"""Run one detached per-run supervisor as this module's main program.

Dispatch launches the supervisor with ``python -m reckon.crew.supervisor_main``
rather than ``python -m reckon.crew.dispatch``. The crew package imports its
concern modules when it loads, dispatch among them, so running dispatch as the
main module re-executes a module that import already ran and opens the
supervisor's stderr with runpy's duplicate-import warning before any supervisor
code runs. Nothing imports this module, so the launch stays quiet.

The body hands the supervisor role token and its arguments to the same command
dispatch exposes, and exits with that command's status.
"""

from __future__ import annotations

import sys

# Imported from the submodule directly: the crew facade re-exports a function
# named ``dispatch``, which shadows the submodule attribute of the same name.
from reckon.crew.dispatch import SUPERVISOR_ENTRY, _supervisor_command


def main(arguments: list[str] | None = None) -> int:
    """Run the supervisor the arguments describe and return its status."""
    argv = list(sys.argv[1:] if arguments is None else arguments)
    if argv and argv[0] == SUPERVISOR_ENTRY:
        argv = argv[1:]
    return _supervisor_command(argv)


if __name__ == "__main__":
    raise SystemExit(main())
