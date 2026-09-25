"""Gate: a stop delivered before the worker exists means no worker is spawned.

A per-run supervisor takes a boundary tree snapshot before it spawns its
worker, and that snapshot walks every registered worktree of the repository, so
it can hold the supervisor for minutes on a large host. A ``crew stop`` — a
SIGTERM to the run's process group, which the supervisor leads — can arrive
inside that window. It used to be ignored: the supervisor survived the signal,
finished its snapshot, and spawned the worker anyway, so the stop was reported
as done while a fresh worker started behind it. This gate holds a supervisor
inside a stubbed snapshot, delivers the stop, releases the snapshot, and
requires that no worker is ever spawned for the run.

Instrument: the boundary snapshot's ``git status`` call is a child process, so
a ``git`` shim first on the child processes' ``PATH`` stubs the snapshot's
timing — the first charged call it makes under the worktree root signals a
marker and is held there until this case releases it. Awaiting that marker
proves the supervisor is inside the pre-spawn snapshot rather than merely slow,
which is the window the defect lives in.

The repository is throwaway, the configuration home is throwaway, and the real
(non-temporary) live-pointer directory is asserted untouched, so a passing run
leaves the host as it found it.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import reckon
from reckon._store import _config_home

PACKAGE_ROOT = Path(reckon.__file__).resolve().parents[1]

# Captured before the suite's autouse home fixture redirects ``RECKON_HOME``,
# so the guard names the directory a real dispatch writes to.
REAL_LIVE_DIR = _config_home() / "crew" / "live"

PYTHON = Path(sys.executable)

PROJECT = "sample"
STUB_COMMAND = "claude"

# Dispatch's own return, and the run id it must have reported.
DISPATCH_BOUND = 60.0
# The supervisor must reach the stubbed snapshot, which is the window this case
# delivers its stop inside.
SCAN_BOUND = 60.0
# After the snapshot is released, either the supervisor ends the run without a
# worker (the fix) or it spawns one (the control).
TERMINAL_BOUND = 45.0

CONFIG: dict[str, Any] = {
    "default_backend": "stub",
    "backends": {
        "stub": {
            "launch": "cli",
            "command": STUB_COMMAND,
            "model": "local-stub",
            "effort": "low",
            "sandbox": "worktree-full",
            "session_reuse": False,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


DRIVER_SOURCE = """\
import json
import os
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
os.environ["RECKON_HOME"] = payload["config"]
os.environ["PATH"] = payload["bin_dir"] + os.pathsep + os.environ.get("PATH", "")

import reckon
from reckon import crew
from reckon.crew.node import TaskNode

node = TaskNode(
    id=payload["node"],
    goal="record one stub-backed dispatch",
    plan="fixture",
    section="supervisor",
    role="implement",
    spec_level="guided",
    done_when=payload["done_when"],
    write_paths=[payload["write_path"]],
    time_budget="25m",
    manifest_path=payload["manifest"],
)
record = crew.dispatch(
    node=node,
    project=payload["project"],
    repo=payload["repo"],
    config=payload["config_data"],
    session=payload["session"],
    watch_required=False,
)
Path(payload["out_path"]).write_text(
    json.dumps(
        {
            "run_id": record.get("run_id"),
            "pid": record.get("pid"),
            "reckon_file": reckon.__file__,
        }
    ),
    encoding="utf-8",
)
"""


SHIM_SOURCE = """#!/bin/sh
charge=0
prev=""
for a in "$@"; do
  case "$prev" in
    -C|--git-dir|--work-tree|-c) prev="$a"; continue ;;
  esac
  if [ "$a" = "status" ]; then charge=1; fi
  prev="$a"
done
if [ "$charge" = "1" ]; then
  here=$(pwd)
  case "$here" in
    "$RECKON_SHIM_SCAN_ROOT"/*)
      : > "$RECKON_SHIM_SCAN_SIGNAL"
      waited=0
      while [ ! -e "$RECKON_SHIM_SCAN_RELEASE" ]; do
        [ "$waited" -ge "$RECKON_SHIM_SCAN_WAITS" ] && break
        sleep 0.02
        waited=$((waited + 1))
      done
      ;;
  esac
fi
exec "$RECKON_SHIM_GIT" "$@"
"""


def _real_git() -> str:
    found = shutil.which("git")
    assert found, "git is not on PATH, so the worktree cannot be built"
    return found


def _run_git(repo: Path, *arguments: str) -> None:
    subprocess.run([_real_git(), *arguments], cwd=repo, check=True, capture_output=True)


def _build_repository(repo: Path, worktree_root: Path) -> None:
    """Create a minimal dispatchable repository whose scan this case can hold."""
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="supervisor">A stop before the spawn means no worker</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    fleet = PACKAGE_ROOT / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py"
    if fleet.is_file():
        destination = repo / "skills" / "reckon-build" / "scripts"
        destination.mkdir(parents=True)
        shutil.copy(fleet, destination / "worktree_fleet.py")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("add", "seed.txt", "docs/plans/fixture.html"),
        (
            "-c",
            "user.email=worker@example.invalid",
            "-c",
            "user.name=Worker",
            "commit",
            "-q",
            "-m",
            "chore: seed",
        ),
    ):
        _run_git(repo, *arguments)
    worktree_root.mkdir(parents=True, exist_ok=True)
    _run_git(
        repo,
        "worktree",
        "add",
        "--detach",
        "--no-checkout",
        str(worktree_root / "registered-00"),
        "HEAD",
    )


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _tail(path: Path, limit: int = 2000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _wait_for(
    predicate: Callable[[], Any], *, timeout: float, interval: float = 0.05
) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def _process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def _reap_processes(home: Path) -> list[int]:
    """Kill every process whose environment names this case's home."""
    needle = str(home).encode()
    killed: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle not in environ:
            continue
        pid = int(entry.name)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue
        killed.append(pid)
    return sorted(killed)


def _snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "entries": []}
    return {"exists": True, "entries": sorted(item.name for item in path.iterdir())}


@pytest.fixture
def host(tmp_path: Path) -> dict[str, Any]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _build_repository(repo, tmp_path / "worktrees")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "git"
    shim.write_text(SHIM_SOURCE, encoding="utf-8")
    shim.chmod(0o755)
    return {
        "base": tmp_path,
        "repo": repo,
        "bin_dir": bin_dir,
        "real_git": _real_git(),
    }


def test_a_stop_during_the_pre_spawn_snapshot_spawns_no_worker(
    host: dict[str, Any], tmp_path: Path
) -> None:
    home = host["base"] / "home"
    home.mkdir()
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(host["repo"] / "docs")}), encoding="utf-8"
    )
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    stub_dir = host["base"] / "stub-bin"
    stub_dir.mkdir()
    stub = stub_dir / STUB_COMMAND
    stub.write_text(
        "#!/bin/sh\n"
        f'echo $$ > "{marker_dir / "worker.pid"}"\n'
        f': > "{marker_dir / "marker"}"\n'
        "sleep 30\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    gate = home / "scan-gate"
    gate.mkdir()
    environment = {
        **os.environ,
        "RECKON_HOME": str(home),
        "PATH": f"{stub_dir}{os.pathsep}{host['bin_dir']}{os.pathsep}"
        + os.environ.get("PATH", ""),
        "PYTHONPATH": f"{host['bin_dir']}{os.pathsep}{PACKAGE_ROOT}",
        "RECKON_SHIM_GIT": host["real_git"],
        "RECKON_SHIM_SCAN_ROOT": str(host["base"] / "worktrees"),
        "RECKON_SHIM_SCAN_SIGNAL": str(gate / "scanning"),
        "RECKON_SHIM_SCAN_RELEASE": str(gate / "release"),
        "RECKON_SHIM_SCAN_WAITS": "1500",
        "RECKON_WATCH_ARMING": "off",
    }
    payload = {
        "repo": str(host["repo"]),
        "config": str(home),
        "project": PROJECT,
        "session": "stop-before-spawn",
        "bin_dir": str(stub_dir),
        "config_data": CONFIG,
        "node": "stop-before-spawn",
        "write_path": "src/stop-before-spawn.txt",
        "done_when": (
            "pytest tests/test_stop_before_spawn_spawns_nothing.py exits 0; the "
            "run directory holds no worker.json within 45 s of the stop"
        ),
        "manifest": str(home / "manifest.md"),
        "out_path": str(home / "driver.json"),
    }
    driver = host["base"] / "driver.py"
    driver.write_text(DRIVER_SOURCE, encoding="utf-8")
    payload_path = home / "payload.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    real_before = _snapshot(REAL_LIVE_DIR)
    outcome: dict[str, Any] = {"case": "a stop during the snapshot spawns nothing"}
    driver_out = (home / "driver.out").open("w")
    driver_err = (home / "driver.err").open("w")
    try:
        process = subprocess.Popen(
            [str(PYTHON), str(driver), str(payload_path)],
            cwd=str(host["repo"]),
            env=environment,
            stdout=driver_out,
            stderr=driver_err,
            stdin=subprocess.DEVNULL,
        )
    finally:
        driver_out.close()
        driver_err.close()
    supervisor_pid: int | None = None
    try:
        process.wait(timeout=DISPATCH_BOUND)
        output = _load_json(home / "driver.json") or {}
        outcome["driver_output"] = output
        assert process.returncode == 0, (
            "the dispatch process failed before returning: "
            f"{_tail(home / 'driver.err')!r}"
        )
        run_id = output.get("run_id")
        assert run_id, "the dispatch process reported no run id"
        assert str(output.get("reckon_file", "")).startswith(str(PACKAGE_ROOT)), (
            f"the driver imported {output.get('reckon_file')!r}, not the package "
            f"under test at {PACKAGE_ROOT}, so this case measured the wrong tree"
        )
        pointer_path = home / "crew" / "live" / f"{run_id}.json"
        pointer = _wait_for(lambda: _load_json(pointer_path), timeout=DISPATCH_BOUND)
        assert pointer, "the dispatch process published no pointer"
        supervisor_pid = int(pointer["pid"])
        outcome["supervisor_pid"] = supervisor_pid
        run_directory = home / "crew" / "runs" / run_id
        outcome["run_directory"] = str(run_directory)

        # The stubbed snapshot is held open: awaiting its marker proves the
        # supervisor is inside the pre-spawn snapshot, which is the window the
        # stop must act in.
        signalled = _wait_for((gate / "scanning").exists, timeout=SCAN_BOUND)
        outcome["snapshot_held"] = bool(signalled)
        assert signalled, (
            f"the stubbed snapshot signalled no hold within {SCAN_BOUND} s, so "
            "the stop was never delivered inside the window this case measures. "
            f"supervisor stderr: {_tail(run_directory / 'supervisor.stderr.log')!r}"
        )
        assert not (run_directory / "worker.json").exists(), (
            "a worker was recorded before the stop was delivered, so the "
            "snapshot was not holding the launch"
        )

        # The stop: exactly what crew stop sends — SIGTERM to the run's process
        # group, which the supervisor leads until it spawns a worker.
        os.killpg(os.getpgid(supervisor_pid), signal.SIGTERM)
        outcome["stop_sent"] = True
        (gate / "release").touch()

        # Wait for whichever outcome arrives: the supervisor ends the run
        # without a worker, or the control spawns one.
        _wait_for(
            lambda: (
                (run_directory / "worker.json").exists()
                or not _process_alive(supervisor_pid)
            ),
            timeout=TERMINAL_BOUND,
        )

        # The no-worker assertion. The declared negative control — the
        # supervisor ignoring SIGTERM inside the stop window — fails here.
        assert not (run_directory / "worker.json").exists(), (
            "a worker was spawned after a stop delivered before the spawn. A "
            "stop delivered before the worker exists must mean no worker is "
            "ever spawned for the run."
        )
        assert not (marker_dir / "worker.pid").exists(), (
            "the stub backend recorded a worker pid, so a worker process was "
            "spawned despite the stop"
        )
        assert not _process_alive(supervisor_pid), (
            f"the supervisor {supervisor_pid} is still alive after the stop and "
            "the released snapshot, so the run was neither ended nor left to "
            "finish"
        )
        exit_record = _load_json(run_directory / "exit.json")
        outcome["exit_record"] = exit_record
        assert isinstance(exit_record, dict), (
            "the supervisor wrote no exit.json for a launch a stop ended before "
            "the worker existed, so the one launch-failure record is missing"
        )
        assert exit_record.get("worker_pid") is None, (
            "the launch-failure record names a worker pid "
            f"{exit_record.get('worker_pid')!r}"
        )
        assert exit_record.get("ended_during") == "launch", (
            f"the record reads ended_during {exit_record.get('ended_during')!r}, "
            "which must be 'launch' for a worker that never started"
        )
        assert "before the worker was spawned" in str(
            exit_record.get("detail") or ""
        ), (
            f"the record's detail {exit_record.get('detail')!r} does not name the "
            "pre-spawn stop this case delivered"
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
        if supervisor_pid is not None:
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(supervisor_pid), signal.SIGKILL)
        outcome["reaped_processes"] = _reap_processes(home)
        real_after = _snapshot(REAL_LIVE_DIR)
        outcome["real_live_dir_unchanged"] = real_before == real_after
        assert real_after == real_before, (
            "this case wrote the real fleet's live directory "
            f"{REAL_LIVE_DIR}: {real_after!r} != {real_before!r}"
        )
    print("STOP-GATE-OUTCOME", json.dumps(outcome))
