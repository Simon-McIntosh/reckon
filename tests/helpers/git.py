"""Git subprocess helpers shared across the test suite."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(repository: Path, *arguments: str) -> str:
    """Run ``git <arguments>`` in ``repository`` and return stripped stdout."""
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
