"""A served case's discovery walk-reuse window does not outlive its test.

``serve.main`` assigns ``reckon.serve._SIGNATURE_TTL_S`` — the walk-reuse window
the served process reads its discovery walks under — on whichever thread runs
the server, and does not restore it. So a test that starts the served process
leaves a live discovery memo behind, and a later test sharing its pytest process
reads a memoised walk it never armed. ``tests/test_server_listens_before_it_watches.py``
refuses at setup when it finds that memo live.

This runs the file that starts the served process and the entry-point file in
one child pytest process, in that order, and asserts the entry-point cases pass.
The suite-wide restore in ``tests/conftest.py`` is what makes the assignment not
survive; the negative-control run removes it (by environment) and this test then
fails.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent

# The file whose served-process tests assign the discovery memo live and leave
# it, and the entry-point file that refuses at setup once a memo is already live.
_LEAKING_FILE = _TESTS_DIR / "test_served_code_drift.py"
_ENTRY_POINT_FILE = _TESTS_DIR / "test_server_listens_before_it_watches.py"

# The entry-point setup refusal this run must not see: it names the live memo
# when a served case's assignment survived into a later test in the process.
_LIVE_MEMO_MESSAGE = "left the discovery memo live"

# Environment the parent's own fixtures set for its test process, which must not
# decide the child's homes or records: the child's own conftest fixtures create
# them. The memo-restore switch is deliberately NOT dropped, because the
# negative-control run arms it here and the child has to see it.
_INHERITED_ENV_TO_DROP = (
    "RECKON_HOME",
    "RECKON_STATE_ROOT",
    "RECKON_MOUNTS_PATH",
    "RECKON_SCHEDULER_REACH_LOG",
    "RECKON_RUN_ID",
    "RECKON_MANIFEST",
    "RECKON_ATTEMPT_STARTED_AT",
    "RECKON_DISCOVERY_REUSE_S",
)


def _revision() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in _INHERITED_ENV_TO_DROP:
        env.pop(name, None)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    return env


def test_a_served_case_leaves_no_live_discovery_memo_for_a_later_test(
    tmp_path: Path,
) -> None:
    command = [
        sys.executable,
        "-m",
        "pytest",
        str(_LEAKING_FILE),
        str(_ENTRY_POINT_FILE),
        "-p",
        "no:cacheprovider",
        "-p",
        "no:xdist",
        "-q",
    ]
    completed = subprocess.run(
        command,
        cwd=_REPO_ROOT,
        env=_child_env(),
        capture_output=True,
        timeout=240,
        text=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    log = tmp_path / "child-pytest.log"
    log.write_text(
        f"# revision: {_revision()}\n"
        f"# tree: {_REPO_ROOT}\n"
        f"# command: {' '.join(command)}\n\n"
        f"{output}\nEXIT={completed.returncode}\n",
        encoding="utf-8",
    )
    assert _LIVE_MEMO_MESSAGE not in output, (
        "a served case left the discovery memo live for a later test in the "
        f"same process; the child pytest log is {log}:\n{output}"
    )
    assert completed.returncode == 0, (
        "the entry-point cases did not pass after the served-process file ran "
        f"first in one process; the child pytest log is {log}:\n{output}"
    )
