"""Git subprocess helpers shared across the test suite."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from reckon.worker_git_shim import RUN_ID_ENV


def git(repository: Path, *arguments: str) -> str:
    """Run ``git <arguments>`` in ``repository`` and return stripped stdout.

    The run identity a crew worker exports is left out of the child's
    environment. The git shim refuses a mutating verb whose run's live pointer
    it cannot read, and that is the state a test is in whenever it has named a
    temporary configuration home — so without this a test's own ``git init``
    would be refused the moment the suite ran inside a worker. A test's
    repository is a throwaway of its own, so the verb it runs changes nothing
    the shim exists to protect.
    """
    environment = {key: value for key, value in os.environ.items() if key != RUN_ID_ENV}
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()
