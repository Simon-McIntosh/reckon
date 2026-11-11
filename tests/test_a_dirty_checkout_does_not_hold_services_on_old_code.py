"""A dirty shared checkout does not hold a declared service on old code.

A shared checkout's working tree routinely carries peer sessions' uncommitted
edits while main moves. The trust test a reload takes refuses such a tree, and
until this behaviour was fixed the refusal covered the restart decision too: the
admit service and the station kept running the code they started on for about
ten hours while landings sat on main, because the supervisor would not restart
them onto a tree it would not itself load.

The cases here drive ``DeclaredServices.reload`` against a real temporary
checkout and a stubbed process table. Nothing signals a real process: the
supervisor's signal path lands in the table, so a case says what a copy does
with SIGTERM and reads back what was sent. The configuration home, the runtime
directory, the state directory and the reload-image cache all point at the
temporary tree, so no case reads or writes the machine's own fleet state.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import checkout_trust
from reckon.crew import fleet_supervisor

SERVICE = "demo"
STOP_GRACE_SECONDS = 0.2


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _stamp(repo: Path) -> str:
    """The committed head's code identity, read the way the supervisor reads it."""
    return checkout_trust.source_code_identity(repo / "reckon", source_root=repo)


def _make_checkout(root: Path) -> Path:
    repo = root / "checkout"
    (repo / "reckon").mkdir(parents=True)
    (repo / "reckon" / "__init__.py").write_text("", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "worker")
    (repo / "reckon" / "demo.py").write_text("VERSION = 'one'\n", encoding="utf-8")
    _git(repo, "add", "reckon/__init__.py", "reckon/demo.py")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


def _move_main(repo: Path) -> str:
    """Commit a change to a module the service imports, and return its stamp."""
    (repo / "reckon" / "demo.py").write_text("VERSION = 'two'\n", encoding="utf-8")
    _git(repo, "add", "reckon/demo.py")
    _git(repo, "commit", "-q", "-m", "move main")
    return _stamp(repo)


class FakeProcesses:
    """A process table the supervisor is driven against, with no real pids."""

    def __init__(self) -> None:
        self.alive: set[int] = set()
        self.lock_holder: int | None = None
        self.signals: list[tuple[int, int, float]] = []
        self.started: list[tuple[int, list[str], dict[str, str]]] = []
        self.alive_by_name: dict[int, str] = {}
        self._next_pid = 90000

    def add_copy(self, stamp: str) -> int:
        self._next_pid += 1
        pid = self._next_pid
        self.alive.add(pid)
        self.alive_by_name[pid] = stamp
        self.lock_holder = pid
        return pid

    def is_alive(self, pid: int) -> bool:
        return pid in self.alive

    def lock_held(self) -> bool:
        return self.lock_holder is not None and self.is_alive(self.lock_holder)

    def signal(self, pid: int, signum: int, reason: str = "") -> None:
        self.signals.append((pid, signum, time.monotonic()))
        self.alive.discard(pid)
        if self.lock_holder == pid:
            self.lock_holder = None

    def start(self, argv, environ) -> int:
        self._next_pid += 1
        pid = self._next_pid
        self.alive.add(pid)
        self.lock_holder = pid
        self.started.append((pid, list(argv), dict(environ)))
        return pid

    def sigterms(self) -> list[int]:
        return [pid for pid, signum, _ in self.signals if signum == signal.SIGTERM]


class FakePopen:
    """The one thing ``_start_locked`` needs from a spawned service."""

    def __init__(self, table: FakeProcesses) -> None:
        self._table = table

    def __call__(self, argv, **kwargs) -> SimpleNamespace:
        return SimpleNamespace(pid=self._table.start(argv, kwargs.get("env") or {}))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A fleet home, runtime root and config in a temporary tree."""
    config_home = tmp_path / "config-home"
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    run_root = tmp_path / "run"
    for directory in (config_home / "fleet", runtime, state, run_root):
        directory.mkdir(parents=True, exist_ok=True)
    (config_home / "fleet" / "services.json").write_text(
        json.dumps({SERVICE: ["python", "-m", "demo"]}), encoding="utf-8"
    )
    environ = {
        "FLEET_RUNTIME_DIR": str(runtime),
        "FLEET_STATE_DIR": str(state),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_RUNTIME_DIR": str(run_root),
        "TMPDIR": str(run_root),
    }
    # The reload-image cache resolves from the node's runtime root, so it must
    # be pointed at the temporary tree too: a case that published under the
    # machine's own runtime directory would write outside the repository.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run_root))
    monkeypatch.setenv("TMPDIR", str(run_root))
    return SimpleNamespace(
        tmp_path=tmp_path,
        config_home=config_home,
        runtime=runtime,
        state=state,
        env=environ,
    )


def _seed_record(home, pid: int, stamp: str) -> None:
    """Record a running copy as an earlier supervisor would have left it."""
    fleet_supervisor.service_pid_path(home.runtime, SERVICE).write_text(
        f"{pid} - {stamp}\n", encoding="utf-8"
    )


def _install(monkeypatch, repo: Path) -> FakeProcesses:
    """Point the supervisor's process, signal and checkout paths at fakes."""
    table = FakeProcesses()
    monkeypatch.setattr(fleet_supervisor, "signal_worker", table.signal)
    monkeypatch.setattr(
        fleet_supervisor,
        "process_runs_service",
        lambda pid, name, argv: table.is_alive(pid),
    )
    monkeypatch.setattr(
        fleet_supervisor, "lock_held", lambda name, runtime: table.lock_held()
    )
    monkeypatch.setattr(
        fleet_supervisor,
        "subprocess",
        SimpleNamespace(Popen=FakePopen(table), DEVNULL=-3),
    )
    monkeypatch.setattr(fleet_supervisor, "_checkout_root", lambda: repo)
    return table


def _manager(home, repo: Path, *, code_stamp=None) -> fleet_supervisor.DeclaredServices:
    return fleet_supervisor.DeclaredServices(
        home.runtime,
        home.env,
        clock=lambda: 0.0,
        code_stamp=code_stamp or (lambda: _stamp(repo)),
        checkout_trust=lambda: checkout_trust.moved_checkout_is_trustworthy(repo),
        stop_grace=STOP_GRACE_SECONDS,
    )


def test_a_dirty_checkout_restarts_a_service_onto_the_committed_head(
    home, monkeypatch, capsys
) -> None:
    """Main moves under a dirty working tree and the service still follows it.

    The copy is replaced within one regular check, the replacement records the
    committed head's stamp, and the code it is started from carries none of the
    working tree's uncommitted edit.
    """
    repo = _make_checkout(home.tmp_path)
    first = _stamp(repo)
    table = _install(monkeypatch, repo)
    old_pid = table.add_copy(first)
    _seed_record(home, old_pid, first)

    second = _move_main(repo)
    assert second != first, "the committed code did not move"
    (repo / "reckon" / "demo.py").write_text(
        "VERSION = 'uncommitted-edit'\n", encoding="utf-8"
    )
    dirty, _detail = checkout_trust.tracked_reckon_tree_is_dirty(repo)
    assert dirty, "the fixture's working tree is not dirty, so the case proves nothing"

    _manager(home, repo).reload()

    assert table.sigterms() == [old_pid], "the stale copy was not stopped"
    assert not table.is_alive(old_pid), "the stale copy survived the restart"
    assert len(table.started) == 1, "no replacement copy was started"
    new_pid, _argv, child_environ = table.started[0]
    assert fleet_supervisor.recorded_service_pid(home.runtime, SERVICE) == new_pid, (
        "the replacement is not the copy the record names"
    )
    assert fleet_supervisor.recorded_service_stamp(home.runtime, SERVICE) == second, (
        "the replacement does not record the committed head's stamp"
    )

    image = Path(child_environ["PYTHONPATH"].split(os.pathsep)[0])
    assert image.name == second, "the replacement does not import the committed head"
    carried = (image / "reckon" / "demo.py").read_text(encoding="utf-8")
    assert "uncommitted-edit" not in carried, (
        "the replacement runs the working tree's uncommitted edit"
    )
    assert "two" in carried, "the replacement does not carry the committed head's code"

    output = capsys.readouterr().out
    assert first in output and second in output, (
        "the log does not name both stamps, so a reader cannot tell what moved"
    )
    assert "uncommitted edits" in output, (
        "the log does not say the working tree's edits were left behind"
    )


def test_a_stamp_the_supervisor_cannot_compare_is_reported_not_adopted(
    home, monkeypatch, capsys
) -> None:
    """An unreadable identity leaves the copy running and says it is uncompared."""
    repo = _make_checkout(home.tmp_path)
    first = _stamp(repo)
    table = _install(monkeypatch, repo)
    pid = table.add_copy(first)
    _seed_record(home, pid, first)

    manager = _manager(home, repo, code_stamp=lambda: checkout_trust.IDENTITY_UNKNOWN)
    manager.reload()

    assert table.signals == [], "a copy was stopped on an unreadable identity"
    assert table.started == [], "a copy was started on an unreadable identity"
    assert table.is_alive(pid), "the running copy did not survive"
    assert manager._states[SERVICE].pid == pid
    output = capsys.readouterr().out
    assert "uncompared" in output, (
        "the supervisor did not report the copy as uncompared"
    )
    assert "already running as pid" not in output, (
        "the supervisor reported an uncompared copy as adopted"
    )
