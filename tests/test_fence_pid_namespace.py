"""A fenced worker can signal only the processes it started.

The fence runs each worker in a pid namespace of its own with a procfs mounted
for that namespace. kill(2) resolves a pid in the caller's namespace, so a
process outside the worker's tree cannot be reached whatever the worker selects:
`ps -e` with `--ppid` (ps unions its selectors) lists the worker's own tree and
nothing else, and a kill aimed at a host pid finds no such process.

The real-bubblewrap tests compose the fence through the production path over a
synthetic node tmp and home, start a sibling process outside it, and check that
the sibling is unreachable from inside. Each has a control showing the same
command reaches the sibling when the pid-namespace flags are removed, so a pass
is owed to those flags rather than to the command failing for another reason.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from reckon import _worker_fence

RUN_ID = "r-20990101T000000000000-fence-pid-namespace"


def _compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> list[str]:
    """Compose a fence over a synthetic node tmp and home, as a dispatch does."""
    node_tmp = tmp_path / "node-tmp"
    scratch_root = node_tmp / "reckon-crew-scratch"
    own = scratch_root / RUN_ID
    own.mkdir(parents=True)
    fleet = node_tmp / "fleet-runtime"
    fleet.mkdir()
    (node_tmp / ".codex").mkdir()
    home = tmp_path / "home"
    run = home / ".config" / "reckon" / "crew" / "runs" / RUN_ID
    run.mkdir(parents=True)
    monkeypatch.setattr(_worker_fence, "NODE_TMP_ROOT", node_tmp)
    monkeypatch.setattr(_worker_fence, "shared_store_seal_roots", list)
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(scratch_root))
    monkeypatch.setenv("FLEET_RUNTIME_DIR", str(fleet))
    return _worker_fence.fence_argv(
        command,
        writable_directories=[str(own)],
        manifest_path=str(run / "manifest.md"),
        home=str(home),
    )


def _without_pid_namespace(argv: list[str]) -> list[str]:
    """The same fence with the pid-namespace flags removed: the control."""
    flags = list(_worker_fence.PID_NAMESPACE_ARGS)
    start = argv.index(flags[0])
    assert argv[start : start + len(flags)] == flags
    return argv[:start] + argv[start + len(flags) :]


def test_the_fence_puts_the_worker_in_its_own_pid_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = _compose(tmp_path, monkeypatch, ["/bin/true"])
    separator = argv.index("--")
    head = argv[:separator]
    assert "--unshare-pid" in head
    proc = head.index("--proc")
    assert head[proc + 1] == "/proc"
    # The procfs mount overlays the root bind, so it must come after it.
    assert head.index("--dev-bind") < proc


def _fence_runs() -> str | None:
    if shutil.which(_worker_fence.FENCE_BINARY) is None:
        return "bubblewrap is not on PATH"
    problem = _worker_fence.fence_capability_problem()
    if problem is not None:
        return problem[1]
    return None


_SKIP = _fence_runs()

_PROBE = """
import os, subprocess, sys
target = int(sys.argv[1])
try:
    os.kill(target, 0)
    reached = "reached"
except ProcessLookupError:
    reached = "absent"
except PermissionError:
    reached = "denied"
listed = subprocess.run(
    ["ps", "-eo", "pid=", "--ppid", "1"], capture_output=True, text=True, check=True
).stdout.split()
print(reached)
print(" ".join(listed))
print(os.getpid(), os.path.realpath("/proc/self"))
"""


def _probe(argv: list[str], sibling: int) -> tuple[str, list[int], str]:
    result = subprocess.run(
        [*argv, str(sibling)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    reached, listed, identity = result.stdout.strip().splitlines()
    return reached, [int(pid) for pid in listed.split()], identity


@pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")
def test_a_fenced_worker_cannot_reach_a_process_outside_its_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(_PROBE)
    argv = _compose(tmp_path, monkeypatch, [sys.executable, str(probe)])
    sibling = subprocess.Popen(["sleep", "60"])
    try:
        reached, listed, identity = _probe(argv, sibling.pid)
        assert reached == "absent"
        # `ps -e --ppid 1` selects every process the worker can see, the
        # union that made a node-wide kill out of a cleanup command; inside
        # the fence it is the worker's own tree and never the sibling.
        assert sibling.pid not in listed
        assert os.getpid() not in listed
        own_pid, proc_self = identity.split()
        assert proc_self == f"/proc/{own_pid}"

        reached, listed, _ = _probe(_without_pid_namespace(argv), sibling.pid)
        assert reached == "reached"
        assert sibling.pid in listed
    finally:
        sibling.kill()
        sibling.wait()
