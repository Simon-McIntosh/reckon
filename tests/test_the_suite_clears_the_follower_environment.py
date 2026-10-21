"""The suite clears the follower environment it inherits.

A crew-host session exports four variables into every process it starts, this
suite included. They tell follower and session code that the process runs under
a live host — whether a pane is hosted, which process owns a follower, which
commit and source tree the running image carries. A test that drives a follower
then inherits a state it never set and reads the host that ran the suite as its
own session.

``tests/conftest.py`` removes the four for every test. This case proves it by
running the affected files as a subprocess with the four set exactly as a crew
host sets them, and requiring them green: the files are red under that
environment without the fixture and green with it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The four variables a crew-host session stamps into its children.
FOLLOWER_SESSION_ENV = {
    "RECKON_SESSION_HOST_SETTLE": "1",
    "RECKON_FOLLOWER_OWNER": json.dumps({"pid": 4242, "start_time": "999.0"}),
    "RECKON_FOLLOWER_IMAGE_COMMIT": "0" * 40,
    "RECKON_FOLLOWER_SOURCE_ROOT": str(REPO_ROOT),
}

# The files that fail in bulk when the suite runs from a crew-host session.
AFFECTED_FILES = (
    "tests/test_crew_dispatch_guard.py",
    "tests/test_crew_session_delivery.py",
    "tests/test_a_reload_runs_the_committed_head.py",
)

# The inner run gets its own worker pool, basetemp and timeout so it never
# shares pytest's three-slot basetemp with the outer run. The three files carry
# several tests that sleep tens of seconds apiece, so the pool is wide enough to
# overlap them: at eight workers the inner run takes about eighty seconds on an
# idle node, which leaves room for the slowdown of running it inside a busy
# outer suite and still finish inside the outer 300-second per-test bound.
_INNER_WORKERS = 8
_INNER_TIMEOUT_S = 275
_SUBPROCESS_TIMEOUT_S = 285


def _crew_host_environment() -> dict[str, str]:
    """The ambient environment with the four variables set as a host sets them."""
    environment = dict(os.environ)
    for name in FOLLOWER_SESSION_ENV:
        environment.pop(name, None)
    environment.update(FOLLOWER_SESSION_ENV)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    return environment


def _run_affected_files(basetemp: Path) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        "-rfE",
        f"-n{_INNER_WORKERS}",
        f"--timeout={_INNER_TIMEOUT_S}",
        f"--basetemp={basetemp}",
        *AFFECTED_FILES,
    ]
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=_crew_host_environment(),
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
        check=False,
    )


def test_the_follower_session_is_absent_in_this_process():
    """The autouse fixture clears the four before any test body runs.

    Read directly from the running process: run from a crew-host session the
    variables are present until the fixture removes them, so this case is red
    without the fixture and green with it.
    """
    leaked = [name for name in FOLLOWER_SESSION_ENV if name in os.environ]
    assert leaked == [], f"the follower session leaked into the suite: {leaked}"


def test_a_test_that_sets_one_keeps_its_value(monkeypatch):
    """The fixture removes the ambient value without fighting a test that wants one."""
    monkeypatch.setenv("RECKON_FOLLOWER_OWNER", "a-test-owner")
    assert os.environ["RECKON_FOLLOWER_OWNER"] == "a-test-owner"


def test_the_affected_files_pass_with_a_crew_host_session_set(tmp_path):
    """The files that fail under a crew-host session pass once it is cleared."""
    result = _run_affected_files(tmp_path / "bt")
    assert result.returncode == 0, (
        "the affected files failed under a crew-host environment:\n"
        f"--- stdout ---\n{result.stdout[-6000:]}\n"
        f"--- stderr ---\n{result.stderr[-2000:]}"
    )
