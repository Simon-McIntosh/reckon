"""A job's fence start waits out a mount point that vanishes under it.

A submitted job starts its worker's fence on the job's own node, and the fence
binds that node's whole tree with its submounts. On some partitions the node's
automounter makes short-lived mount points under the node tmp while a job
starts; one that is listed and then gone fails the fence's setup with "Can't
bind mount ... No such file or directory", and the job ended in its first
second with its task never started. Two nodes of one partition did this on 20
November, so a fenced worker could not use that partition at all.

The fence program here is a stand-in that fails its first calls the way the
real one did and otherwise runs the command it is handed, so the cases read how
often the task bootstrap tries and that the task itself starts exactly once.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from reckon import nested_launch

STAND_IN = """#!/bin/sh
count=$(cat "$COUNT" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$COUNT"
if [ "$count" -le "$FAILURES" ]; then
  echo "$MESSAGE" >&2
  exit 1
fi
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
shift
exec "$@"
"""

VANISHED = (
    "bwrap: Can't bind mount /oldroot/tmp/auto0vL3R8 on /newroot/tmp/auto0vL3R8: "
    "No such file or directory"
)


def _bootstrap(tries: int) -> str:
    """The task bootstrap with a short settle pause, so a case costs no seconds."""
    return (
        nested_launch._TASK_REENTRY_TEMPLATE.replace(
            "_FENCE_PROGRAM_STATUS", str(nested_launch.FENCE_PROGRAM_STATUS)
        )
        .replace("_MOUNT_SETTLE_TRIES", str(tries))
        .replace("_MOUNT_SETTLE_SECONDS", "0.01")
    )


@pytest.fixture()
def start(tmp_path: Path):
    program = tmp_path / "fence-stand-in"
    program.write_text(STAND_IN, encoding="utf-8")
    program.chmod(0o755)
    count = tmp_path / "count"
    marker = tmp_path / "task-started"

    def run(*, failures: int, message: str, tries: int = 5):
        recipe = {
            "argv": [str(program)],
            "program": str(program),
            "scratch": str(tmp_path),
        }
        spec = {
            "fence": recipe,
            "command": ["/bin/sh", "-c", f'echo started >> "{marker}"'],
            "env": nested_launch.WORKER_FENCE_ENV,
        }
        encoded = base64.urlsafe_b64encode(json.dumps(spec).encode()).decode()
        script = _bootstrap(tries)
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, script, encoded],
            env={
                "PATH": "/usr/bin:/bin",
                "COUNT": str(count),
                "FAILURES": str(failures),
                "MESSAGE": message,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        calls = int(count.read_text()) if count.exists() else 0
        starts = len(marker.read_text().splitlines()) if marker.exists() else 0
        return result, calls, starts

    return run


def test_a_mount_that_vanishes_twice_is_waited_out(start) -> None:
    result, calls, starts = start(failures=2, message=VANISHED)

    assert result.returncode == 0, result.stderr
    assert starts == 1
    # Two failed tries, the try that held, and the task's own start.
    assert calls == 4


def test_a_node_whose_mounts_hold_costs_one_extra_start(start) -> None:
    result, calls, starts = start(failures=0, message=VANISHED)

    assert result.returncode == 0, result.stderr
    assert starts == 1
    assert calls == 2


def test_another_fence_failure_is_not_retried(start) -> None:
    result, calls, starts = start(
        failures=99, message="bwrap: setting up uid map: Permission denied"
    )

    assert result.returncode != 0
    assert starts == 0
    # One try that failed for another reason, then the real start that reports it.
    assert calls == 2
    assert "Permission denied" in result.stderr


def test_a_mount_that_never_holds_is_reported_after_the_bounded_tries(start) -> None:
    result, calls, starts = start(failures=99, message=VANISHED, tries=3)

    assert result.returncode != 0
    assert starts == 0
    assert calls == 4
    assert "Can't bind mount" in result.stderr


def test_the_shipped_bootstrap_carries_the_settle_bound() -> None:
    assert "_MOUNT_SETTLE" not in nested_launch._TASK_REENTRY
    assert f"range({nested_launch.MOUNT_SETTLE_TRIES})" in nested_launch._TASK_REENTRY
