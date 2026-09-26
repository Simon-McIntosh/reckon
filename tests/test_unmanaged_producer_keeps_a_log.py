"""A watcher armed without a service manager keeps a log.

A host with no systemd user manager — a fleet compute node, where lingering is
refused outright — arms the project watcher as a plain background process. That
process is nothing's child, so when it stops there is no unit state and no
journal to read: the seat empties, the follower goes quiet, and the cause of the
death cannot be established. Measured 2026-09-26 on 98dci4-clu-2058: the seat
read empty at 06:44:46Z after the stream stopped at 06:28:42Z and nothing
recorded why.

The three checks below pin the remedy: the unmanaged arming names a log, the
watcher it starts writes that log and its seat record names the path, a watcher
that raises leaves its traceback there, and none of it reaches the real logs
directory when the arming runs against a synthetic home.

The child is a real process rather than a stub because the whole point is where
a process's own output ends up — a fake stdout cannot show that a library or the
interpreter reaches the same file the arming named.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from reckon import service
from reckon.crew import runs
from tests.test_crew_watch_ensure import FakeWatchService, _backend_bin, _config

BUS_REFUSED = (
    "systemctl --user daemon-reload failed: Failed to connect to bus: "
    "Connection refused"
)

# A project name no real watcher runs under, so the only writer of this
# node's log is the child the test starts.
PROJECT = "unmanaged-log-sample"


class UnreachableWatchService(FakeWatchService):
    """A manager whose bus calls fail the way a restarted client's do."""

    def active(self, project: str) -> bool:
        raise service.ServiceError(BUS_REFUSED)

    def start(self, project: str, *, restart: bool) -> None:
        raise service.ServiceError(BUS_REFUSED)


DRIVER = (
    "import sys\n"
    "\n"
    "from reckon.crew import runs\n"
    "\n"
    "project = sys.argv[1]\n"
    'with runs._project_watch_claim(project, "30s") as (acquired, watcher):\n'
    "    if not acquired:\n"
    "        raise SystemExit(3)\n"
    '    print(f"imported reckon from {{runs.__file__}}")\n'
    "{body}\n"
)


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway configuration home, so no real log or seat is reached."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


@pytest.fixture()
def worktree_root() -> Path:
    """The tree under test, which the child must import instead of the install."""
    return Path(__file__).resolve().parents[1]


def _arm_with_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: str,
    project: str = PROJECT,
) -> tuple[dict, subprocess.CompletedProcess]:
    """Drive an unmanaged arming whose child runs ``body`` after taking its seat.

    The child is spawned from a patched dispatch producer, so the arming path
    under test is the real one — the fallback, the watcher's adoption of the log
    the spawn names, and the seat record — while no real watcher, console script
    or backend has to start. The fake stands in for the spawn, so it hands the
    child the environment that spawn builds, including the log path it names;
    the test below that drives the real ``_ensure_watch_producer`` proves the
    spawn itself names it.
    """
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    script = tmp_path / "producer_driver.py"
    script.write_text(DRIVER.format(body=textwrap.indent(body, "    ")))
    root = Path(__file__).resolve().parents[1]
    children: list[subprocess.CompletedProcess] = []

    def fake_producer(name: str) -> dict:
        environment = dict(os.environ)
        environment[runs.WATCH_LOG_ENV] = str(runs.watch_log_path(name))
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(root), environment.get("PYTHONPATH", "")]
        )
        completed = subprocess.run(
            [sys.executable, str(script), name],
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        children.append(completed)
        return {"project": name, "watcher_live": True}

    monkeypatch.setattr(dispatch_module, "_ensure_watch_producer", fake_producer)
    result = runs.ensure_watcher_service(
        project,
        manager=UnreachableWatchService(),
        config=_config(_backend_bin(tmp_path)),
    )
    assert children, "the fallback armed nothing"
    return result, children[-1]


def _seat_record(project: str) -> dict:
    """Read the project's seat record without taking its lock."""
    path = runs.watch_lock_path(project)
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return runs._read_watch_record(handle)


def test_an_unmanaged_arming_writes_its_log_and_the_seat_names_it(
    config_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The arming falls back, and the watcher it started leaves its words behind."""
    result, child = _arm_with_child(
        tmp_path, monkeypatch, body='print("watcher took the seat", file=sys.stderr)'
    )

    assert result["path"] == "fallback"
    assert child.returncode == 0

    log_path = runs.watch_log_path(PROJECT)
    assert log_path.parent == config_home / "logs"
    assert log_path.is_file(), "the unmanaged arming left no log at all"
    text = log_path.read_text(encoding="utf-8")
    # The child imported the tree under test, so this asserts the code that
    # wrote the log is the code the node declares.
    assert str(Path(__file__).resolve().parents[1]) in text
    assert "watcher took the seat" in text
    # Every adopted line carries the moment it arrived, so a reader can date
    # the last line rather than only see it.
    assert "[2" in text

    record = _seat_record(PROJECT)
    assert record.get("log_path") == str(log_path)


def test_a_watcher_that_raises_leaves_its_traceback_in_the_log(
    config_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A producer's death has a cause in the log, which is the whole point."""
    result, child = _arm_with_child(
        tmp_path,
        monkeypatch,
        body='raise RuntimeError("the producer fell over")',
    )

    assert result["path"] == "fallback"
    assert child.returncode == 1

    text = runs.watch_log_path(PROJECT).read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in text
    assert "the producer fell over" in text


def test_the_real_logs_directory_is_untouched_by_the_arming(
    config_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A synthetic home keeps the arming off the account's own logs.

    The positive half matters as much as the negative one: the synthetic log
    must exist, or "the real one is untouched" would also be true of an arming
    that wrote nothing anywhere.
    """
    real_log = (
        Path.home()
        / ".config"
        / "reckon"
        / "logs"
        / (f"watch-{runs.watch_unit_name(PROJECT)}.log")
    )
    before = (
        real_log.exists(),
        real_log.stat().st_mtime if real_log.exists() else None,
    )

    _arm_with_child(tmp_path, monkeypatch, body='print("probe")')

    assert runs.watch_log_path(PROJECT).is_file(), "the synthetic home got no log"
    after = (real_log.exists(), real_log.stat().st_mtime if real_log.exists() else None)
    assert after == before, f"the arming wrote the real log {real_log}"


def test_the_seat_names_the_log_the_service_path_already_writes(
    config_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both arming routes name one file, so a reader looks in one place.

    The unit the service path writes appends to ``watch_log_path``; the seat
    record a watcher writes names that same path. A reader who finds a dead
    seat therefore finds the file whichever route started the producer.
    """
    unit = runs.render_watch_unit(
        PROJECT, environment={"PATH": "/usr/bin"}, executable="/usr/bin/reckon"
    )
    assert f"StandardOutput=append:{runs.watch_log_path(PROJECT)}" in unit


# The driver a patched console entry point runs in place of ``reckon crew
# watch``. It does what a watcher does at the one point that matters here: it
# takes the seat — which is where the log is adopted — and then writes a line
# to its own stderr, one the parent can look for in the log the arming named.
ADMISSION_DRIVER = (
    "#!{python}\n"
    "import sys, time\n"
    "sys.path.insert(0, {root!r})\n"
    "\n"
    "from reckon.crew import runs\n"
    "\n"
    "project = {project!r}\n"
    'with runs._project_watch_claim(project, "30s") as (acquired, watcher):\n'
    "    if not acquired:\n"
    "        raise SystemExit(3)\n"
    '    print("the watcher took the seat", file=sys.stderr)\n'
    "    time.sleep({hold})\n"
)


def _write_watch_driver(tmp_path: Path, *, hold: str = "0.8") -> Path:
    """Write an executable stand-in for the console script the spawn runs."""
    script = tmp_path / "watch_driver.py"
    script.write_text(
        ADMISSION_DRIVER.format(
            python=sys.executable,
            root=str(Path(__file__).resolve().parents[1]),
            project=PROJECT,
            hold=hold,
        )
    )
    script.chmod(0o755)
    return script


def _wait_for_marker(path: Path, marker: str, *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and marker in path.read_text(
            encoding="utf-8", errors="replace"
        ):
            return True
        time.sleep(0.05)
    return False


def test_the_dispatch_admission_route_writes_the_log_its_seat_names(
    config_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route the dispatch guard uses names a log a worker's absence can be read from.

    The fallback is not the only unmanaged arming: ``_ensure_watch_producer`` is
    the admission path's own, and it started a producer whose output went to
    /dev/null. This drives the real function — only the console entry point is
    replaced, so the spawn, its argv and the log it names are the ones under
    test.
    """
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    driver = _write_watch_driver(tmp_path)
    monkeypatch.setattr(dispatch_module, "_watch_executable", lambda: str(driver))
    monkeypatch.setenv(dispatch_module.WATCH_ARMING_ENV, "on")

    state = dispatch_module._ensure_watch_producer(PROJECT)
    assert state["watcher_live"] is True, state

    log_path = runs.watch_log_path(PROJECT)
    assert _wait_for_marker(log_path, "the watcher took the seat"), (
        "the dispatch admission route armed a producer that kept no log"
    )
    # A seat that names a log nothing writes is worse than no seat at all: the
    # reader is sent to a file that is not there.
    assert _seat_record(PROJECT).get("log_path") == str(log_path)


def test_the_fleet_delegated_spawn_carries_the_log_without_the_arming_environment(
    config_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fleet route spawns from the batch step's env, so the argv must carry the path.

    ``_spawn_through_fleet`` hands the argv to the allocation's batch step,
    which starts it from its own environment. An arming that named the log only
    in its own process environment would therefore reach the watcher on the
    direct route and miss it on the fleet route. This captures the argv the
    fleet route delegates and runs it with the log variable removed, so only
    the argv can carry it.
    """
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    driver = _write_watch_driver(tmp_path, hold="0")
    monkeypatch.setattr(dispatch_module, "_watch_executable", lambda: str(driver))
    monkeypatch.setenv(dispatch_module.WATCH_ARMING_ENV, "on")

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    record = tmp_path / "fleet.json"
    record.write_text(
        json.dumps({"runtime_dir": str(runtime), "job_id": ""}), encoding="utf-8"
    )
    monkeypatch.setenv(dispatch_module.FLEET_RECORD_PATH_ENV, str(record))
    monkeypatch.setenv(dispatch_module.FLEET_SPAWN_ENV, "on")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    # The fake spawn starts nothing, so bound the poll that waits for a seat.
    monkeypatch.setattr(dispatch_module, "WATCHER_LOAD_BOUND_SECONDS", 0.3)

    captured: dict = {}

    def fake_spawn(runtime_dir, request_id, spec_path, ack_path) -> int:
        captured["spec"] = json.loads(Path(spec_path).read_text(encoding="utf-8"))
        return 4321

    monkeypatch.setattr(dispatch_module, "_spawn_through_fleet", fake_spawn)
    dispatch_module._ensure_watch_producer(PROJECT)
    assert captured, "the fleet route was not taken"

    environment = dict(os.environ)
    environment.pop(runs.WATCH_LOG_ENV, None)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1]), environment.get("PYTHONPATH", "")]
    )
    subprocess.run(
        captured["spec"]["argv"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )

    log_path = runs.watch_log_path(PROJECT)
    assert log_path.is_file(), (
        "the fleet-delegated argv did not carry the log the seat names"
    )
    assert "the watcher took the seat" in log_path.read_text(encoding="utf-8")


# A watcher that keeps writing after its log has rotated. The first writes go
# through the Python stream, which rotates; the last two go straight to the
# process's own descriptors 1 and 2, which is how a library writing unbuffered
# output and the interpreter printing an uncaught traceback reach the log.
ROTATION_DRIVER = (
    "import os, sys\n"
    "sys.path.insert(0, {root!r})\n"
    "from reckon.crew import runs\n"
    "log = runs._adopt_watch_log()\n"
    "assert log is not None\n"
    'chunk = "R" * 4096 + "\\n"\n'
    "for _ in range(1100):\n"
    "    sys.stdout.write(chunk)\n"
    'os.write(1, b"FD-ONE-AFTER-ROTATION\\n")\n'
    'os.write(2, b"FD-TWO-AFTER-ROTATION\\n")\n'
    'sys.stderr.write("STREAM-AFTER-ROTATION\\n")\n'
)


def test_a_descriptor_write_after_two_rotations_lands_in_the_current_log(
    config_home: Path, tmp_path: Path
) -> None:
    """A descriptor pointed at the log once must be re-pointed when the log rotates.

    Rotation renames the file away, so the descriptor keeps writing an inode a
    reader no longer opens — the traceback that explains a producer's death
    lands in a file nothing reads, or in one rotation discards. Two rotations
    are forced, then the descriptors are written to, and the current log is the
    only file the test will accept the words in.
    """
    log_path = runs.watch_log_path(PROJECT)
    script = tmp_path / "rotation_driver.py"
    script.write_text(
        ROTATION_DRIVER.format(root=str(Path(__file__).resolve().parents[1])),
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1]), environment.get("PYTHONPATH", "")]
    )
    environment[runs.WATCH_LOG_ENV] = str(log_path)
    subprocess.run(
        [sys.executable, str(script)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )

    # Two rotations must have happened, or the descriptor writes would have
    # nothing to survive and the check would pass without testing anything.
    assert log_path.with_name(f"{log_path.name}.1").is_file(), "no rotation happened"
    current = log_path.read_text(encoding="utf-8")
    assert "FD-ONE-AFTER-ROTATION" in current, "a write through fd 1 was lost"
    assert "FD-TWO-AFTER-ROTATION" in current, "a write through fd 2 was lost"
    assert "STREAM-AFTER-ROTATION" in current
