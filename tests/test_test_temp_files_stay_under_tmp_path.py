"""Files that reach for the system temp directory, run with none to reach.

Each file in the population runs in its own subprocess whose ``TMPDIR`` points
at a fresh directory, and the assertion is that the directory is still empty
afterwards — the whole of it, not just the prefixes known to leak, so a new
writer in any of these files fails here too. The nested session is given its
own ``--basetemp`` under this test's tree, so pytest's own temporary
directories never count against the empty directory being asserted: only a
helper that reaches for the system temp directory can put an entry there.

``-n 4`` keeps the three nested sessions inside the host's time budget; where a
helper writes is a property of the helper, not of how the run is scheduled.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

# The nested session's own per-test timeout is 300 s from pyproject; a whole
# file is larger than that on a loaded host, so this test's bound is set above
# the nested run's expected cost and the subprocess call just under it.
NESTED_RUN_TIMEOUT_SECONDS = 900
GATE_TIMEOUT_SECONDS = 960

# The files measured writing into the system temp directory: a configuration
# home per declaring test, and a manifest directory named for a process that
# found no test's own temporary tree to write under.
TEMP_WRITING_FILES = (
    "test_budget_group_declaration.py",
    "test_budget.py",
    "test_crew.py",
)


@pytest.mark.timeout(GATE_TIMEOUT_SECONDS)
@pytest.mark.parametrize("filename", TEMP_WRITING_FILES)
def test_the_file_leaves_the_system_temp_directory_empty(filename, tmp_path):
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    basetemp = tmp_path / f"basetemp-{filename.removesuffix('.py')}"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "-n",
            "4",
            "--basetemp",
            str(basetemp),
            str(TESTS_DIR / filename),
        ],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "TMPDIR": str(system_temp),
            "PYTHONPATH": str(REPO_ROOT),
        },
        capture_output=True,
        text=True,
        timeout=NESTED_RUN_TIMEOUT_SECONDS,
        check=False,
    )

    entries = sorted(path.name for path in system_temp.iterdir())
    assert completed.returncode == 0, (
        f"the nested session for {filename} did not pass:\n"
        f"{completed.stdout[-4000:]}\n{completed.stderr[-2000:]}"
    )
    assert entries == [], (
        f"{filename} left entries in the system temp directory it was given: "
        f"{entries}. A test's temporary files belong under its own tmp_path or "
        "the session basetemp, where pytest's retention removes them."
    )
