"""A re-exec reaps the children it inherits instead of stranding them."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import dispatch_launch, queue_admission, recovery_watch

REPO_ROOT = Path(__file__).resolve().parents[1]

# Runs after the image replacement. It adopts the children the previous image
# left, then watches the inherited child's /proc entry: a reaped child has no
# entry, while an unreaped one sits there as a zombie.
_POST_EXEC_SCRIPT = """\
import json
import os
import sys
import time

sys.path.insert(0, {repo!r})
from reckon.crew.dispatch_launch import _adopt_launched_workers_from_reexec

_adopt_launched_workers_from_reexec()
child = int(os.environ["INHERITED_CHILD_PID"])
started = time.monotonic()
present = True
state = ""
zombie_seen = False
gone_after = None
deadline = started + 10.0
while time.monotonic() < deadline:
    try:
        stat = open(f"/proc/{{child}}/stat").read()
    except OSError:
        present = False
        gone_after = time.monotonic() - started
        break
    state = stat.rsplit(") ", 1)[-1][:1]
    if state == "Z":
        zombie_seen = True
    time.sleep(0.02)
with open(os.environ["REEXEC_RESULT_PATH"], "w") as handle:
    json.dump({{"present": present, "state": state,
               "zombie_seen": zombie_seen, "gone_after": gone_after}}, handle)
"""

# Starts a child the image replacement will strand, then replaces its own image
# without waiting on that child. The Popen object dies with the image, so the
# child is inherited by the replacement with nothing referring to it.
_PRE_EXEC_SCRIPT = """\
import os
import subprocess
import sys

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(1.5)"]
)
with open(os.environ["INHERITED_CHILD_PID_PATH"], "w") as handle:
    handle.write(str(child.pid))
environment = dict(os.environ)
environment["INHERITED_CHILD_PID"] = str(child.pid)
os.execve(
    sys.executable,
    [sys.executable, os.environ["POST_EXEC_SCRIPT"]],
    environment,
)
"""


def _write_pointer(home: Path, run_id: str, node: str, *, phase: str) -> None:
    stream = home / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n')
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
            "phase": phase,
            "created_at": datetime.now(tz=UTC).isoformat(),
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(stream),
            "process_alive": None,
        },
    )


def _run_reexec(tmp_path: Path) -> dict:
    """Replace an image that owns an unregistered child, then adopt and watch."""
    post_exec = tmp_path / "post_exec.py"
    post_exec.write_text(_POST_EXEC_SCRIPT.format(repo=str(REPO_ROOT)))
    pre_exec = tmp_path / "pre_exec.py"
    pre_exec.write_text(_PRE_EXEC_SCRIPT)
    child_pid_path = tmp_path / "child.pid"
    result_path = tmp_path / "result.json"
    environment = dict(os.environ)
    environment["POST_EXEC_SCRIPT"] = str(post_exec)
    environment["INHERITED_CHILD_PID_PATH"] = str(child_pid_path)
    environment["REEXEC_RESULT_PATH"] = str(result_path)
    completed = subprocess.run(
        [sys.executable, str(pre_exec)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    child_pid = int(child_pid_path.read_text())
    result = json.loads(result_path.read_text()) if result_path.exists() else {}
    result["child_pid"] = child_pid
    result["exit_status"] = completed.returncode
    result["stderr"] = completed.stderr
    return result


def _no_child_left(pid: int) -> bool:
    return not Path(f"/proc/{pid}").exists()


def test_an_inherited_child_is_reaped_after_the_image_replacement(
    tmp_path: Path,
) -> None:
    result = _run_reexec(tmp_path)

    assert result.get("exit_status") == 0, result
    assert result["present"] is False, (
        f"the inherited child still holds a process-table entry after the "
        f"replacement (state {result.get('state')!r}): {result}"
    )
    # The reaper polls every 0.1 s while a registered child is outstanding, so
    # the entry is gone well inside a poll or two of the child exiting.
    assert result["gone_after"] is not None
    assert result["gone_after"] < 2.0, result


def test_a_child_the_new_image_starts_keeps_its_own_exit_status() -> None:
    dispatch_launch._adopt_launched_workers_from_reexec()

    child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"])
    assert child.pid not in dispatch_launch._LAUNCHED_WORKERS
    assert child.wait(timeout=60) == 7


def test_a_handed_over_pid_keeps_its_run_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    record = {"run_id": "r-handover", "run_directory": "/nonexistent/run"}
    monkeypatch.setenv(
        dispatch_launch._LAUNCHED_WORKERS_HANDOVER_ENV,
        json.dumps({"pids": [child.pid], "runs": {str(child.pid): record}}),
    )
    try:
        dispatch_launch._adopt_launched_workers_from_reexec()
        assert child.pid in dispatch_launch._LAUNCHED_WORKERS
        assert dispatch_launch._LAUNCHED_WORKER_RUNS[child.pid] == record
    finally:
        with contextlib.suppress(ProcessLookupError):
            child.kill()
        with contextlib.suppress(ChildProcessError):
            os.waitpid(child.pid, 0)
        # The reaper may already have reaped it; either way the Popen must not
        # poll a pid that no longer names a child.
        child.returncode = -9
        with dispatch_launch._LAUNCHED_WORKERS_LOCK:
            dispatch_launch._LAUNCHED_WORKERS.discard(child.pid)
            dispatch_launch._LAUNCHED_WORKER_RUNS.pop(child.pid, None)
    assert _no_child_left(child.pid)


def test_admission_reaches_the_adoption_once_at_start_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        dispatch_launch,
        "_adopt_launched_workers_from_reexec",
        lambda: calls.append(1),
    )

    queue_admission.admit_until_stopped(once=True)

    assert calls == [1]


def test_the_producer_reaches_the_adoption_once_at_start_up(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_pointer(home, "r-producer", "producer-node", phase="working")
    calls: list[int] = []
    monkeypatch.setattr(
        dispatch_launch,
        "_adopt_launched_workers_from_reexec",
        lambda: calls.append(1),
    )
    stream = recovery_watch.watch_ticker("proj", stall_window="1h", poll_interval=0)
    try:
        next(stream)
    finally:
        stream.close()

    assert calls == [1]