"""The resume sweep lifts a run only once something observed the worker end.

The row a coordinator reads promises that the sweep lifts a run once its process
is observed to have ended. The sweep's own guard refused a proven-live process
and nothing else, so a run whose liveness nothing established — a worker on
another machine, or a pointer that recorded no process at all — fell through and
was resumed as though it were dead. That is the one recovery mistake which costs
more than it recovers: two workers writing one run is the collision the promise
exists to prevent.

An observation is one of exactly two things, and the sweep reads them through the
same three-way vocabulary the row states rather than a second one — a pid checked
on this host and found dead, or the supervisor's exit record, which outlives a
pointer nobody updated and stays readable on a machine that never launched the
worker. Either licenses a lift; a stored answer carried from elsewhere does not,
and neither does no answer at all.

The sweep below runs once over three parked runs, and every park is genuinely
parked: the condition each declares is met by the fixture's own file, so the lift
stage is reached by the condition rather than by a stub, and the three differ
only in what is known about the worker's end. A fourth case holds a proven-live
local process and is not lifted, as before. The unknown-liveness case is the one
that fails against the old guard, where its run was lifted.

The declared mutation removes the observed-end gate from the lift, restoring the
guard that refuses only a proven-live process; the unknown-liveness case's run is
then lifted and its assertion fails. The gate that runs it logs that mutation
verbatim as the red log's first line.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, resumption, runs
from tests import test_a_live_run_never_reads_dead as liveness

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "restore the guard that refuses only a proven-live process in a scratch "
    "copy; the unknown-liveness case must be lifted and fail"
)

PROJECT = "fixture-project"
HOST = socket.gethostname()
FOREIGN_HOST = "a-launcher-host-that-is-not-this-one"

# The moment the sweep is asked, and the older one the manifests are stamped
# with, so a park's age is a fixture fact rather than the wall clock.
NOW_SECONDS = datetime(2026, 9, 29, 12, 0, tzinfo=UTC).timestamp()
MANIFEST_NS = int(datetime(2026, 9, 29, 10, 0, tzinfo=UTC).timestamp()) * 1_000_000_000

CONDITION = "the scheduler job has finished"

# One parked run per answer to the question the lift rests on.
EXIT_RUN = "r-observed-exit"
DEAD_PID_RUN = "r-observed-dead-pid"
UNKNOWN_RUN = "r-unobserved-liveness"
LIVE_RUN = "r-proven-live"


def _home_fingerprint(home: Path) -> list[tuple[str, int]]:
    """The real config home's own entries, by name and mtime.

    One directory level only: the point is to catch a write that landed in the
    reader's own home, and a recursive walk of a live fleet's home on GPFS is the
    crawl this check must not itself become.
    """
    if not home.is_dir():
        return []
    return sorted((entry.name, entry.stat().st_mtime_ns) for entry in home.iterdir())


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run against a temporary config home, and prove the real one untouched.

    The fixture is the receipt for its own isolation: a pointer or a run
    directory written to the real home would both escape the test and collide
    with a live fleet, so the fingerprint is taken before the environment moves
    and the same home is re-read after the case ends.
    """
    real_home = Path(os.path.expanduser("~")) / ".config" / "reckon"
    before = _home_fingerprint(real_home)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield
    assert _home_fingerprint(real_home) == before


def _waiting_manifest(awaited: Path) -> str:
    return (
        "status: waiting\n"
        f"wait_condition: {CONDITION}\n"
        f"wait_file: {json.dumps([str(awaited)])}\n"
        "resume_brief: read the job's output and continue\n"
    )


def _write_exit_record(run_id: str) -> None:
    """The supervisor's account of the end, written where its reader looks."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / recovery.EXIT_RECORD_NAME).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "worker_pid": None,
                "launched_at": "2026-09-29T10:00:00Z",
                "exited_at": "2026-09-29T10:30:00Z",
                "exit_code": 0,
                "stream_records_seen": 4,
            }
        ),
        encoding="utf-8",
    )


def _parked_pointer(
    tmp_path: Path,
    run_id: str,
    *,
    pid: int | None,
    launcher_host: str | None,
    exit_record: bool = False,
) -> dict:
    """One run parked on a condition its own file has already reported met.

    The awaited path exists before the sweep reads the declaration, so the
    condition is met by the fixture rather than by a stubbed probe and the
    default vector is the one that carries the lift. Everything but the evidence
    about the worker's end is identical across the three runs, so the sweep's
    decision differs only in that.
    """
    worktree = tmp_path / f"{run_id}-tree"
    worktree.mkdir(parents=True, exist_ok=True)
    awaited = tmp_path / f"{run_id}-condition-met"
    awaited.write_text("done\n", encoding="utf-8")
    manifest = tmp_path / f"{run_id}.md"
    manifest.write_text(_waiting_manifest(awaited), encoding="utf-8")
    os.utime(manifest, ns=(MANIFEST_NS, MANIFEST_NS))
    if exit_record:
        _write_exit_record(run_id)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "backend": "fixture-lane",
        "launch": "cli",
        "argv": ["fixture-agent", "exec"],
        "session_id": "fixture-session",
        "phase": "working",
        "attempt": 1,
        "created_at": "2026-09-29T09:59:00+00:00",
        "manifest_path": str(manifest),
        "log_path": str(tmp_path / f"{run_id}.jsonl"),
        "stderr_path": str(tmp_path / f"{run_id}.stderr.log"),
        "worktree": str(worktree),
        "process_alive": None,
        "pid": pid,
        "pid_start_time": recovery._process_start_time(pid) if pid else None,
        "node": {
            "id": run_id,
            "role": "implement",
            "time_budget": "20m",
            "write_paths": [],
        },
    }
    if launcher_host is not None:
        record["launcher_host"] = launcher_host
    return record


def _sweep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pointers: list[dict]
) -> dict:
    """One sweep over the given fleet, entering where the follower enters."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(resumption, "list_live", lambda **_kwargs: list(pointers))
    monkeypatch.setattr(resumption, "_claimed_write_paths", lambda _pointer: [])
    return resumption.sweep(
        PROJECT,
        dry_run=True,
        now=datetime.fromtimestamp(NOW_SECONDS, tz=UTC),
    )


def _declared_wait(pointer: dict) -> dict:
    """The park the sweep will read, asserted into the arm under test."""
    wait = recovery.external_wait(pointer, now_seconds=NOW_SECONDS)
    assert wait is not None, pointer["run_id"]
    assert wait["valid"] is True, wait.get("error")
    assert "exit:0" in wait["terminal"], wait["terminal"]
    return wait


def test_the_sweep_lifts_only_the_runs_whose_end_it_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three parked runs, one sweep, and only two observed ends.

    The supervisor's record and the dead pid each license a lift. The run whose
    worker lives on another machine and left no record does not, because the
    lift would rest on an observation nobody took; its refusal names the reading
    it is holding, so a coordinator sees why the run was left alone rather than
    concluding the sweep passed it over.
    """
    pointers = [
        _parked_pointer(
            tmp_path,
            EXIT_RUN,
            pid=liveness._absent_pid(),
            launcher_host=FOREIGN_HOST,
            exit_record=True,
        ),
        _parked_pointer(
            tmp_path, DEAD_PID_RUN, pid=liveness._absent_pid(), launcher_host=HOST
        ),
        _parked_pointer(
            tmp_path,
            UNKNOWN_RUN,
            pid=liveness._absent_pid(),
            launcher_host=FOREIGN_HOST,
        ),
    ]
    for pointer in pointers:
        _declared_wait(pointer)

    report = _sweep(monkeypatch, tmp_path, pointers)

    assert report["checked"] == len(pointers)
    assert [row["run_id"] for row in report["resumed"]] == [EXIT_RUN, DEAD_PID_RUN]
    skipped = {row["run_id"]: row for row in report["skipped"]}
    assert sorted(skipped) == [UNKNOWN_RUN]
    detail = skipped[UNKNOWN_RUN]["detail"]
    assert "liveness unknown" in detail, detail
    assert "nothing observed the end" in detail, detail
    assert skipped[UNKNOWN_RUN]["reason"] == "resume-refused", skipped[UNKNOWN_RUN]


def test_a_proven_live_process_is_not_lifted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case that was already right, and stays right.

    A process on this host that the process table answers for is refused before
    anything about observation is asked, with the message the guard has always
    given, and the refusal is the launcher's own rather than a second wording of
    it. The child is real and is reaped on the way out.
    """
    with liveness._live_child() as pid:
        pointer = _parked_pointer(tmp_path, LIVE_RUN, pid=pid, launcher_host=HOST)
        _declared_wait(pointer)

        report = _sweep(monkeypatch, tmp_path, [pointer])

    assert report["checked"] == 1
    assert report["resumed"] == []
    skipped = {row["run_id"]: row for row in report["skipped"]}
    assert sorted(skipped) == [LIVE_RUN]
    detail = skipped[LIVE_RUN]["detail"]
    assert "still has a live process" in detail, detail
    assert "before resuming" in detail, detail
