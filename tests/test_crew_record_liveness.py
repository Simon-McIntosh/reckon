"""A run's liveness is read from its record, through one accessor.

A worker's pid answers only on the host that issued it, so once a run is
placed as a job the pid primitive is the wrong question and the run's own
record is the right one. These cases hold the shape that makes that change
possible: the accessor answers exactly what the primitive answers for the
record's pid today, and no module outside :mod:`reckon.crew.runs` reaches for
a record's pid by itself.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import runs

REPO_ROOT = Path(__file__).resolve().parents[1]
CREW = REPO_ROOT / "reckon" / "crew"

# Modules that decide a run's liveness from its record. runs.py owns both the
# pid primitive and the accessor, so it is not in this list.
LIVENESS_MODULES = (
    "recovery.py",
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


def _record_pid_names(tree: ast.AST) -> set[str]:
    """Names bound to an expression that reads a pid out of a record."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if value is None:
            continue
        source = ast.unparse(value)
        if not any(marker in source for marker in PID_FROM_RECORD):
            continue
        names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _process_alive_calls(
    source: str, filename: str = "<source>"
) -> list[dict[str, Any]]:
    """Every call to the pid primitive, with whether its pid came from a record."""
    tree = ast.parse(source, filename=filename)
    record_pid_names = _record_pid_names(tree)
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
        from_record = any(marker in argument for marker in PID_FROM_RECORD) or (
            isinstance(arg, ast.Name) and arg.id in record_pid_names
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


def test_the_module_owning_the_primitive_still_calls_it() -> None:
    """The scan is not blind on a file it is known to match."""
    assert _process_alive_calls((CREW / "runs.py").read_text())


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
    """The read model, not the accessor: a dead pid stops reading as running."""
    pid = _reaped_pid()
    # A positive control so the False below is a measurement: the primitive
    # reads a process known to be present, then the pointer is re-read.
    assert runs.process_alive(os.getpid()) is True
    _write_pointer(home, "r-gone", phase="working", pid=pid, pid_start_time="12345")
    reloaded = [row for row in runs.list_live() if row["run_id"] == "r-gone"]
    assert len(reloaded) == 1
    assert reloaded[0]["process_alive"] is False


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
