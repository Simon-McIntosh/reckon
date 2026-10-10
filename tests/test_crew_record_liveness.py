"""A run's liveness is read from its record, through one accessor.

A worker's pid answers only on the host that issued it, so once a run is
placed as a job the pid primitive is the wrong question and the run's own
record is the right one. These cases hold the shape that makes that change
possible: the accessor answers exactly what the primitive answers for the
record's pid today, and no module outside :mod:`reckon.crew.process_liveness`
reaches for a record's pid by itself.
"""

from __future__ import annotations

import ast
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import recovery, runs

REPO_ROOT = Path(__file__).resolve().parents[1]
CREW = REPO_ROOT / "reckon" / "crew"

# Modules that decide a run's liveness from its record. process_liveness.py
# owns the pid primitive and accessor, so it is not in this list.
LIVENESS_MODULES = (
    "runs.py",
    "watch_unit.py",
    "follower_registration.py",
    "recovery.py",
    "recovery_classification.py",
    "recovery_liveness.py",
    "recovery_memo.py",
    "recovery_repair_dispatch.py",
    "recovery_review_acceptance.py",
    "recovery_review_delivery.py",
    "recovery_review_dispatch.py",
    "recovery_review_subject.py",
    "recovery_stream.py",
    "recovery_vocabulary.py",
    "recovery_wait.py",
    "recovery_watch.py",
    "dispatch.py",
    "resumption.py",
    "promotion.py",
    "routing.py",
    "claims.py",
    "node.py",
    "directory.py",
    "query.py",
)

# The shapes by which a pid is taken from a record. A call spelled any of
# these decides liveness from a record without passing through the accessor.
PID_FROM_RECORD = ('["pid"]', "['pid']", '.get("pid")', ".get('pid')")


def _reaped_pid() -> int:
    """A pid the kernel has already collected, so the primitive says False."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_the_accessor_agrees_with_the_pid_primitive() -> None:
    live = os.getpid()
    # Positive control: the probe sees a process known to be present, so the
    # False readings below are measurements rather than a blind instrument.
    assert runs.process_alive(live) is True
    assert runs.record_process_alive({"pid": live}) is True

    dead = _reaped_pid()
    assert runs.process_alive(dead) is False
    assert runs.record_process_alive({"pid": dead}) is False

    assert runs.record_process_alive({"pid": None}) is None
    assert runs.record_process_alive({}) is None
    assert runs.record_process_alive(None) is None

    for record in ({"pid": live}, {"pid": dead}, {"pid": None}, {}):
        assert runs.record_process_alive(record) == runs.process_alive(
            record.get("pid")
        )


def _scope_owners(tree: ast.AST) -> dict[int, ast.AST]:
    """Map every node to the function (or module) whose names it reads.

    A name bound from a record and the probe that consumes it are compared only
    within the scope that owns both. A module-wide name map would flag a probe
    that takes a pid which merely shares a name with a pid read elsewhere — a
    supervisor pid this dispatch has just spawned, say — and that call may probe
    the process the module itself started.
    """
    owner: dict[int, ast.AST] = {}

    def walk(node: ast.AST, scope: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
                )
                else scope
            )
            owner[id(child)] = inner
            walk(child, inner)

    owner[id(tree)] = tree
    walk(tree, tree)
    return owner


def _process_alive_calls(
    source: str, filename: str = "<source>"
) -> list[dict[str, Any]]:
    """Every call to the pid primitive, with whether its pid came from a record.

    A pid read from a record is one bound by an assignment in the same scope, so
    a probe is judged against the names that scope itself read from a record.
    """
    tree = ast.parse(source, filename=filename)
    owners = _scope_owners(tree)
    record_pid_names: dict[int, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if value is not None and any(
            marker in ast.unparse(value) for marker in PID_FROM_RECORD
        ):
            record_pid_names.setdefault(id(owners[id(node)]), set()).update(
                t.id for t in targets if isinstance(t, ast.Name)
            )

    found: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if called != "process_alive" or not node.args:
            continue
        arg = node.args[0]
        argument = ast.unparse(arg)
        scope_names = record_pid_names.get(id(owners[id(node)]), set())
        from_record = any(marker in argument for marker in PID_FROM_RECORD) or (
            isinstance(arg, ast.Name) and arg.id in scope_names
        )
        found.append(
            {"line": node.lineno, "argument": argument, "from_record": from_record}
        )
    return found


def test_the_scan_recognises_the_forbidden_shape() -> None:
    """Positive control for the scan, so an empty result means compliance."""
    direct = _process_alive_calls("alive = process_alive(record['pid'])")
    assert [call["from_record"] for call in direct] == [True]

    indirect = _process_alive_calls(
        "pid = pointer.get('pid')\nalive = process_alive(pid)\n"
    )
    assert [call["from_record"] for call in indirect] == [True]

    other_pid = _process_alive_calls("alive = process_alive(record['parent_pid'])")
    assert [call["from_record"] for call in other_pid] == [False]
    assert (
        _process_alive_calls("alive = process_alive(os.getppid())")[0]["from_record"]
        is False
    )


def test_no_module_decides_record_liveness_with_a_bare_pid() -> None:
    offenders: dict[str, list[dict[str, Any]]] = {}
    for name in LIVENESS_MODULES:
        calls = _process_alive_calls(
            (CREW / name).read_text(), filename=str(CREW / name)
        )
        flagged = [call for call in calls if call["from_record"]]
        if flagged:
            offenders[name] = flagged
    assert offenders == {}


def test_the_registration_module_still_calls_the_primitive() -> None:
    """The scan is not blind on a file it is known to match."""
    assert _process_alive_calls((CREW / "follower_registration.py").read_text())


# --- the recorded start time is re-derived on every read ---------------------


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the crew tree at a temp home, leaving the real one untouched."""
    live_directory = runs.live_dir()
    before = (
        sorted(item.name for item in live_directory.iterdir())
        if (live_directory.is_dir())
        else []
    )
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    yield config_home
    after = (
        sorted(item.name for item in live_directory.iterdir())
        if (live_directory.is_dir())
        else []
    )
    assert after == before


def _write_pointer(home: Path, run_id: str, **fields: Any) -> None:
    record = {
        "run_id": run_id,
        "project": "liveness-fixture",
        "created_at": runs._utc_now(),
        **fields,
    }
    runs._write_json(runs.pointer_path(run_id), record)


def test_a_pointer_whose_process_is_gone_stops_working(home: Path) -> None:
    """The read model, not the accessor: a dead pid stops reading as running.

    ``list_live`` returns pointers as they are stored and never writes a probe
    into ``process_alive``: a value there would be read back as this host's own
    answer by a reader that never issued the pid. Liveness is derived where it
    is read, through the classifier, so the dead pid is asserted there, on a
    pointer whose launching host is this one.
    """
    pid = _reaped_pid()
    # A positive control so the False below is a measurement: the primitive
    # reads a process known to be present, then the pointer is re-read.
    assert runs.process_alive(os.getpid()) is True
    _write_pointer(
        home,
        "r-gone",
        phase="working",
        pid=pid,
        pid_start_time="12345",
        launcher_host=socket.gethostname(),
    )
    reloaded = [row for row in runs.list_live() if row["run_id"] == "r-gone"]
    assert len(reloaded) == 1
    assert recovery.classify_pointer(reloaded[0])["process_alive"] is False


def test_a_reused_pid_is_not_read_as_a_survivor() -> None:
    """A live pid under a start tick that disagrees is the previous process."""
    live = os.getpid()
    actual = runs._process_start_time(live)
    assert actual is not None, "the check needs a readable start tick to compare"
    # The pid is genuinely alive, so a False can only come from the start time.
    assert runs.process_alive(live) is True
    stale = str(int(actual) + 1)
    assert runs.record_process_alive({"pid": live, "pid_start_time": stale}) is False


def test_a_matching_start_time_reads_alive() -> None:
    live = os.getpid()
    actual = runs._process_start_time(live)
    assert actual is not None
    assert runs.record_process_alive({"pid": live, "pid_start_time": actual}) is True


def test_a_record_without_a_start_tick_is_unchanged() -> None:
    """An absent tick is not a mismatch: the primitive's answer stands."""
    live = os.getpid()
    assert runs.record_process_alive({"pid": live}) is True
    dead = _reaped_pid()
    assert runs.record_process_alive({"pid": dead}) is False


def test_the_seat_guard_keeps_a_running_holder_with_a_stale_tick() -> None:
    """The one caller that asks "is the process running", not "is it the one".

    A seat guard must not refuse to see a running holder, so it opts out of the
    reuse check rather than depending on the check being absent.
    """
    live = os.getpid()
    actual = runs._process_start_time(live)
    assert actual is not None
    stale = str(int(actual) + 1)
    assert (
        runs.record_process_alive(
            {"pid": live, "pid_start_time": stale}, match_start_time=False
        )
        is True
    )
