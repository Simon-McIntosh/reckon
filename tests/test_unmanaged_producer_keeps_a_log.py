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
import os
import subprocess
import sys
import textwrap
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
    under test is the real one — the fallback, its naming of the log, and the
    watcher's adoption of it — while no real watcher, console script or backend
    has to start. The producer hands the child the environment the arming built,
    including the log path it named.
    """
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    script = tmp_path / "producer_driver.py"
    script.write_text(DRIVER.format(body=textwrap.indent(body, "    ")))
    root = Path(__file__).resolve().parents[1]
    children: list[subprocess.CompletedProcess] = []

    def fake_producer(name: str) -> dict:
        environment = dict(os.environ)
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
