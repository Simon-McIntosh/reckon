"""Gate: a stop delivered before the worker exists means no worker is spawned.

A per-run supervisor takes a boundary tree snapshot before it spawns its
worker, and that snapshot walks every registered worktree of the repository, so
it can hold the supervisor for minutes on a large host. A ``crew stop`` — a
SIGTERM to the run's process group, which the supervisor leads — can arrive
inside that window. The supervisor's stop handler records the request and the
pre-spawn read abandons the launch when it is set, so a stop delivered inside
that window means no worker is ever spawned. The read is taken again with
SIGTERM and SIGHUP blocked across it and the fork, so a stop delivered between
the read and the fork cannot be missed.

This gate holds a supervisor inside the pre-spawn window — first inside a
stubbed snapshot, then at the blocked read just before the fork — delivers the
stop, and requires that no worker is ever spawned for the run. A third case
delivers a group stop after the worker has spawned and requires the supervisor
to record that stop's exit.

Instrument: the boundary snapshot's ``git status`` call is a child process, so
a ``git`` shim first on the child processes' ``PATH`` stubs the snapshot's
timing — the first charged call it makes under the worktree root signals a
marker and is held there until the case releases it. Awaiting that marker
proves the supervisor is inside the pre-spawn snapshot rather than merely slow.
The blocked-read window is held by a ``sitecustomize`` that pauses the
supervisor the moment it blocks the stop signals.

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
from reckon.shim_lookup import real_executable

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


# Runs inside the supervisor interpreter at startup. It pauses the supervisor
# the moment it blocks the stop signals, so a case can deliver a stop at the
# one point between the stop read and the fork — a window otherwise too short
# to hit. It is inert unless the two window paths are set.
SITECUSTOMIZE_SOURCE = """\
import os
import signal
import time

_SIGNAL = os.environ.get("RECKON_STOP_WINDOW_SIGNAL")
_RELEASE = os.environ.get("RECKON_STOP_WINDOW_RELEASE")

if _SIGNAL and _RELEASE:
    _real = signal.pthread_sigmask

    def pthread_sigmask(how, signals):
        result = _real(how, signals)
        try:
            blocked = set(signals)
        except TypeError:
            blocked = set()
        if (
            how == signal.SIG_BLOCK
            and signal.SIGTERM in blocked
            and signal.SIGHUP in blocked
        ):
            with open(_SIGNAL, "w") as created:
                created.write(str(os.getpid()))
            deadline = time.monotonic() + 120
            while not os.path.exists(_RELEASE) and time.monotonic() < deadline:
                time.sleep(0.02)
        return result

    signal.pthread_sigmask = pthread_sigmask
"""


def _real_git() -> str:
    """The real ``git``, never a reckon shim.

    A worker runs with reckon's ``git`` shim first on ``PATH``, so
    ``shutil.which`` would return that shim, and this case's own shim would
    then chain to it and be refused for nesting rather than reach the tool.
    The first ``git`` on ``PATH`` that is not a shim is the one to forward to.
    """
    found = real_executable(
        "git", os.environ.get("PATH", ""), skipped=[_shim_directory()]
    )
    assert found, "git is not on PATH, so the worktree cannot be built"
    return found


def _shim_directory() -> str:
    """The directory reckon's worker shims live in, derived from this package."""
    return str(Path(reckon.__file__).resolve().parent / "worker_shims")


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


def _real_live_entries() -> set[str]:
    return set(_snapshot(REAL_LIVE_DIR)["entries"])


def _own_pointer_names(home: Path) -> set[str]:
    """The pointers this case wrote into its own throwaway live directory."""
    live = home / "crew" / "live"
    if not live.is_dir():
        return set()
    return {item.name for item in live.glob("*.json")}


def _leaked_into_real_live(home: Path, before: set[str]) -> set[str]:
    """Real-live entries that appeared and are pointers this case itself wrote.

    The real live-pointer directory is shared by every session on the host, so a
    peer's concurrent dispatch adds entries this case must not be blamed for —
    the same monitor-of-the-workstation defect the sibling dispatch case fixed.
    Only a pointer this case wrote in its own home is its own leak.
    """
    appeared = _real_live_entries() - before
    return appeared & _own_pointer_names(home)


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
    pysite = tmp_path / "pysite"
    pysite.mkdir()
    (pysite / "sitecustomize.py").write_text(SITECUSTOMIZE_SOURCE, encoding="utf-8")
    return {
        "base": tmp_path,
        "repo": repo,
        "bin_dir": bin_dir,
        "pysite": pysite,
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
        "RECKON_SHIM_SCAN_ROOT": str(host["base"] / ".reckon-worktrees"),
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

    real_before = _real_live_entries()
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
        leaked = _leaked_into_real_live(home, real_before)
        outcome["real_live_dir_leaked"] = sorted(leaked)
        assert not leaked, (
            "this case wrote its own pointers into the real fleet's live "
            f"directory {REAL_LIVE_DIR}: {sorted(leaked)}"
        )
    print("STOP-GATE-OUTCOME", json.dumps(outcome))


def _launch_stub_dispatch(
    host: dict[str, Any],
    *,
    stub_source: str,
    node: str,
    write_path: str,
    done_when: str,
    env_extra: dict[str, str],
) -> tuple[subprocess.Popen[bytes], Path, Path]:
    """Start a dispatch through the stub backend and return its handles.

    Every path the dispatch touches lives under the test's own temporary tree:
    the repository, the crew home, the stub backend and the driver's output.
    """
    home = host["base"] / "home"
    home.mkdir()
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(host["repo"] / "docs")}), encoding="utf-8"
    )
    marker_dir = host["base"] / "markers"
    marker_dir.mkdir()
    stub_dir = host["base"] / "stub-bin"
    stub_dir.mkdir()
    stub = stub_dir / STUB_COMMAND
    stub.write_text(stub_source.format(marker_dir=marker_dir), encoding="utf-8")
    stub.chmod(0o755)
    environment = {
        **os.environ,
        "RECKON_HOME": str(home),
        "PATH": f"{stub_dir}{os.pathsep}{host['bin_dir']}{os.pathsep}"
        + os.environ.get("PATH", ""),
        "PYTHONPATH": f"{host['pysite']}{os.pathsep}{PACKAGE_ROOT}",
        "RECKON_SHIM_GIT": host["real_git"],
        "RECKON_SHIM_SCAN_ROOT": str(host["base"] / "no-such-root"),
        "RECKON_SHIM_SCAN_SIGNAL": str(home / "unused-signal"),
        "RECKON_SHIM_SCAN_RELEASE": str(home / "unused-release"),
        "RECKON_SHIM_SCAN_WAITS": "0",
        "RECKON_WATCH_ARMING": "off",
        **env_extra,
    }
    payload = {
        "repo": str(host["repo"]),
        "config": str(home),
        "project": PROJECT,
        "session": node,
        "bin_dir": str(stub_dir),
        "config_data": CONFIG,
        "node": node,
        "write_path": write_path,
        "done_when": done_when,
        "manifest": str(home / "manifest.md"),
        "out_path": str(home / "driver.json"),
    }
    driver = host["base"] / "driver.py"
    driver.write_text(DRIVER_SOURCE, encoding="utf-8")
    payload_path = home / "payload.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
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
    return process, home, marker_dir


def _await_supervisor(home: Path) -> tuple[str, int, Path]:
    """Return the run id, supervisor pid and run directory for a launched run."""
    output = _load_json(home / "driver.json") or {}
    assert output, "the dispatch process reported no run"
    run_id = output.get("run_id")
    assert run_id, "the dispatch process reported no run id"
    assert str(output.get("reckon_file", "")).startswith(str(PACKAGE_ROOT)), (
        f"the driver imported {output.get('reckon_file')!r}, not the package "
        f"under test at {PACKAGE_ROOT}, so this case measured the wrong tree"
    )
    pointer_path = home / "crew" / "live" / f"{run_id}.json"
    pointer = _wait_for(lambda: _load_json(pointer_path), timeout=DISPATCH_BOUND)
    assert pointer, "the dispatch process published no pointer"
    return run_id, int(pointer["pid"]), home / "crew" / "runs" / run_id


def test_a_stop_between_the_check_and_the_spawn_spawns_no_worker(
    host: dict[str, Any], tmp_path: Path
) -> None:
    window = host["base"] / "stop-window"
    window.mkdir()
    signal_path = window / "entered"
    release_path = window / "release"
    real_before = _real_live_entries()
    process, home, marker_dir = _launch_stub_dispatch(
        host,
        stub_source=(
            "#!/bin/sh\n"
            'echo $$ > "{marker_dir}/worker.pid"\n'
            ': > "{marker_dir}/marker"\n'
            "sleep 30\n"
        ),
        node="stop-window",
        write_path="src/stop-window.txt",
        done_when=(
            "pytest tests/test_stop_before_spawn_spawns_nothing.py exits 0; the "
            "run directory holds no worker.json within 45 s of the stop"
        ),
        env_extra={
            "RECKON_STOP_WINDOW_SIGNAL": str(signal_path),
            "RECKON_STOP_WINDOW_RELEASE": str(release_path),
        },
    )
    supervisor_pid: int | None = None
    try:
        process.wait(timeout=DISPATCH_BOUND)
        assert process.returncode == 0, (
            "the dispatch process failed before returning: "
            f"{_tail(home / 'driver.err')!r}"
        )
        _run_id, supervisor_pid, run_directory = _await_supervisor(home)

        # The supervisor holds the read-to-fork window open at the blocked stop
        # read. Awaiting its signal proves the stop lands inside that window
        # rather than racing it.
        entered = _wait_for(signal_path.exists, timeout=SCAN_BOUND)
        assert entered, (
            f"the supervisor blocked no stop signal within {SCAN_BOUND} s, so no "
            "stop was delivered inside the read-to-fork window this case "
            f"measures. supervisor stderr: "
            f"{_tail(run_directory / 'supervisor.stderr.log')!r}"
        )
        assert not (run_directory / "worker.json").exists(), (
            "a worker was spawned before the stop was delivered, so the window "
            "was not holding the launch"
        )

        # Exactly what crew stop sends — SIGTERM to the run's process group.
        os.killpg(os.getpgid(supervisor_pid), signal.SIGTERM)
        release_path.touch()
        _wait_for(
            lambda: not _process_alive(supervisor_pid), timeout=TERMINAL_BOUND
        )

        # The declared negative control — removing the re-check — fails here.
        assert not (run_directory / "worker.json").exists(), (
            "a worker was spawned after a stop delivered between the stop read "
            "and the fork. A stop delivered inside that window must mean no "
            "worker is ever spawned for the run."
        )
        assert not (marker_dir / "worker.pid").exists(), (
            "the stub backend recorded a worker pid, so a worker process was "
            "spawned despite the stop"
        )
        assert not _process_alive(supervisor_pid), (
            f"the supervisor {supervisor_pid} is still alive after the stop, so "
            "the launch was neither abandoned nor left to finish"
        )
        exit_record = _load_json(run_directory / "exit.json")
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
        _reap_processes(home)
        leaked = _leaked_into_real_live(home, real_before)
        assert not leaked, (
            "this case wrote its own pointers into the real fleet's live "
            f"directory {REAL_LIVE_DIR}: {sorted(leaked)}"
        )


def test_a_group_stop_after_the_spawn_is_recorded_as_that_stops_exit(
    host: dict[str, Any], tmp_path: Path
) -> None:
    real_before = _real_live_entries()
    process, home, marker_dir = _launch_stub_dispatch(
        host,
        stub_source=(
            "#!/bin/sh\n"
            'echo $$ > "{marker_dir}/worker.pid"\n'
            ': > "{marker_dir}/marker"\n'
            "sleep 30\n"
        ),
        node="post-spawn-stop",
        write_path="src/post-spawn-stop.txt",
        done_when=(
            "pytest tests/test_stop_before_spawn_spawns_nothing.py exits 0; the "
            "run directory's exit.json names SIGTERM within 45 s of the stop"
        ),
        env_extra={},
    )
    supervisor_pid: int | None = None
    try:
        process.wait(timeout=DISPATCH_BOUND)
        assert process.returncode == 0, (
            "the dispatch process failed before returning: "
            f"{_tail(home / 'driver.err')!r}"
        )
        _run_id, supervisor_pid, run_directory = _await_supervisor(home)
        worker_record = _wait_for(
            lambda: _load_json(run_directory / "worker.json"), timeout=SCAN_BOUND
        )
        assert worker_record, "the supervisor spawned no worker for the stop"
        worker_pid = int(worker_record["pid"])
        started = _wait_for((marker_dir / "marker").exists, timeout=SCAN_BOUND)
        assert started, (
            "the stub worker never ran, so the group stop would find nothing to "
            "end and the recorded exit would not be this stop's"
        )

        # Exactly what crew stop sends: SIGTERM to the run's process group,
        # which reaches the worker and the supervisor that leads it.
        os.killpg(os.getpgid(supervisor_pid), signal.SIGTERM)

        def _stopped_exit() -> dict[str, Any] | None:
            record = _load_json(run_directory / "exit.json")
            if isinstance(record, dict) and record.get("worker_pid") == worker_pid:
                return record
            return None

        exit_record = _wait_for(_stopped_exit, timeout=TERMINAL_BOUND)
        assert isinstance(exit_record, dict), (
            f"the supervisor recorded no exit for the stopped worker {worker_pid} "
            f"within {TERMINAL_BOUND} s, so it exited instead of recording the "
            "stop's exit"
        )
        # The declared negative control — recording a plain exit instead of the
        # stop's — fails here.
        assert exit_record.get("signal_name") == "SIGTERM", (
            f"the exit record names signal {exit_record.get('signal_name')!r} "
            "rather than SIGTERM, the group stop this case delivered"
        )
        assert exit_record.get("exit_code") is None, (
            "a signal-ended worker must record no exit code, but the record "
            f"carries {exit_record.get('exit_code')!r}"
        )
        assert exit_record.get("worker_pid") == worker_pid, (
            f"the record names worker {exit_record.get('worker_pid')!r} rather "
            f"than the stopped worker {worker_pid}"
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
        if supervisor_pid is not None:
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(supervisor_pid), signal.SIGKILL)
        _reap_processes(home)
        leaked = _leaked_into_real_live(home, real_before)
        assert not leaked, (
            "this case wrote its own pointers into the real fleet's live "
            f"directory {REAL_LIVE_DIR}: {sorted(leaked)}"
        )
