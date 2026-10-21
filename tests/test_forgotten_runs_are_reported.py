"""Terminal, unpromoted runs nobody is coming back to are reported.

A session may keep its runs as long as it needs them, so the cap that once
bounded them retired in favour of a report. What the report must not do is
either of the two easy wrong things: name a run whose session is still being
followed -- a coordinator that stepped away for an hour is not gone -- or miss
the run whose session stopped existing, which is exactly the one no reader will
otherwise ever see.

Three pointers are built in a temporary home: the first is complete and
followed, the
second is complete and forgotten, and the third is running under that same
forgotten session. Exactly the second is listed, so the first is held out by
its follower and the third by its classification -- and the second's own
session is read whole, not one pointer at a time.

The temporary RECKON_HOME makes every pointer and follower registration local
to the case, so no real crew directory is read or written.
"""

from __future__ import annotations

import fcntl
import os
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import forgotten_runs as forgotten
from reckon.crew.follower_registration import (
    _write_watch_record,
    follower_lock_path,
)
from reckon.crew.recovery_vocabulary import RECOVERY_VERBS
from reckon.crew.runs import _write_json, pointer_path, run_dir

PROJECT = "sample"
ATTENDED_SESSION = "session-attended"
FORGOTTEN_SESSION = "session-forgotten"
ATTENDED_RUN = "run-attended-complete"
FORGOTTEN_RUN = "run-forgotten-complete"
RUNNING_RUN = "run-forgotten-running"
SEVEN_HOURS = 7 * 60 * 60.0
EIGHT_HOURS = 8 * 60 * 60.0


def _absent_pid() -> int:
    """A pid the kernel will never allocate: ran higher than pid_max."""

    return int(Path("/proc/sys/kernel/pid_max").read_text().strip()) + 4096


def _aged(moment: float) -> dict[str, Any]:
    """The stamp a run carries for the instant its last act happened."""

    return {"created_at": datetime.fromtimestamp(moment, tz=UTC).isoformat()}


def _pointer(
    run_id: str,
    *,
    session: str,
    status: str,
    age_seconds: float,
    pid: int,
    phase: str = "working",
) -> dict[str, Any]:
    """One live pointer, with its whole evidence aged to one instant.

    Stream, manifest and pointer are all moved into the past together, so the
    one clock the session's attended age is read from -- stream, then pointer,
    then creation -- answers the same age whichever branch it takes.
    """

    moment = time.time() - age_seconds
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"
    stream.write_text('{"type":"assistant"}\n', encoding="utf-8")
    manifest = directory / "manifest.md"
    manifest.write_text(f"node: {run_id}\nstatus: {status}\n", encoding="utf-8")
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "30m"},
        "session": session,
        "phase": phase,
        "attempt": 1,
        "pid": pid,
        "launcher_host": socket.gethostname(),
        "log_path": str(stream),
        "manifest_path": str(manifest),
        "worktree": str(directory / "tree" / run_id),
        "base_sha": "",
        **_aged(moment),
    }
    (directory / "tree" / run_id).mkdir(parents=True, exist_ok=True)
    path = pointer_path(run_id)
    _write_json(path, record)
    for touched in (stream, manifest, path, directory):
        stamp = time.time() - age_seconds
        os.utime(touched, (stamp, stamp))
    return record


def _hold_a_live_registration(project: str, session: str, *, age_seconds: float):
    """Hold a follower registration the way ``follower_registration`` judges it.

    The lock is taken here and held for the life of the case, because liveness
    is a held lock and nothing else -- the registration file's own stamp is
    then aged into the past, so every age beside a live follower says the
    opposite of its liveness. That opposition is what the case measures.
    """

    path = follower_lock_path(project, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    _write_watch_record(
        handle,
        {
            "project": project,
            "session": session,
            "pid": os.getpid(),
            "parent_pid": os.getppid(),
            "delivery": "stream",
            "started_at": datetime.fromtimestamp(
                time.time() - age_seconds, tz=UTC
            ).isoformat(),
        },
    )
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return handle


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Three pointers and a follower the derivation must tell apart."""

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "reckon-home"))
    for name in (
        "RECKON_SESSION_HOST_SETTLE",
        "RECKON_FOLLOWER_OWNER",
        "RECKON_FOLLOWER_IMAGE_COMMIT",
        "RECKON_FOLLOWER_SOURCE_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)

    attended = _pointer(
        ATTENDED_RUN,
        session=ATTENDED_SESSION,
        status="complete",
        age_seconds=SEVEN_HOURS,
        pid=_absent_pid(),
    )
    forgotten_pointer = _pointer(
        FORGOTTEN_RUN,
        session=FORGOTTEN_SESSION,
        status="complete",
        age_seconds=SEVEN_HOURS,
        pid=_absent_pid(),
    )
    running = _pointer(
        RUNNING_RUN,
        session=FORGOTTEN_SESSION,
        status="in-progress",
        age_seconds=SEVEN_HOURS,
        pid=os.getpid(),
    )
    handle = _hold_a_live_registration(
        PROJECT, ATTENDED_SESSION, age_seconds=SEVEN_HOURS
    )
    try:
        yield {
            "attended": attended,
            "forgotten": forgotten_pointer,
            "running": running,
        }
    finally:
        handle.close()


def test_the_view_lists_exactly_the_run_nobody_is_coming_back_to(
    fleet: dict[str, dict[str, Any]],
) -> None:
    """The followed run and the running one are left out; the third is listed."""

    view = forgotten.forgotten_runs_view(PROJECT)

    assert [row["run_id"] for row in view["runs"]] == [FORGOTTEN_RUN], (
        "the forgotten view must name exactly the terminal, unpromoted run "
        "whose session has had no follower and no dispatch; "
        f"it named {[row['run_id'] for row in view['runs']]}"
    )
    row = view["runs"][0]
    assert row["session"] == FORGOTTEN_SESSION
    assert row["node"] == FORGOTTEN_RUN
    assert row["classification"] in {"promotable", "scoring", "complete"}
    assert row["age_seconds"] == pytest.approx(SEVEN_HOURS, abs=120)
    assert row["unattended_seconds"] == pytest.approx(SEVEN_HOURS, abs=120)
    # The recover action the classifier already offers for this run travels on
    # the row, so a reader acts on the view rather than on a second read.
    assert row["recover"] in set(RECOVERY_VERBS.values())
    assert isinstance(row["next_action"], str) and row["next_action"]


def test_a_threshold_older_than_the_session_lists_nothing(
    fleet: dict[str, dict[str, Any]],
) -> None:
    """The threshold is the derivation's parameter, not a constant of the row."""

    assert forgotten.forgotten_runs(PROJECT, threshold_seconds=EIGHT_HOURS) == []


def test_a_pointer_with_no_session_is_forgotten_once_past_the_threshold(
    fleet: dict[str, dict[str, Any]],
) -> None:
    """Absence of a session is not ownership: nothing follows it either way."""

    pointer = _pointer(
        "run-unowned",
        session="",
        status="complete",
        age_seconds=SEVEN_HOURS,
        pid=_absent_pid(),
    )
    assert pointer["session"] == ""
    rows = forgotten.forgotten_runs(PROJECT)
    assert "run-unowned" in [row["run_id"] for row in rows]
    assert forgotten.forgotten_runs(PROJECT, threshold_seconds=EIGHT_HOURS) == []


def _modules_naming(needle: str, *, roots: list[Path] | None = None) -> list[str]:
    """Every dispatch module naming ``needle``, and whatever module does.

    The dispatch modules are read as text rather than imported: an import would
    run their module bodies, and what is being measured is whether a refusal
    path says the word at all.
    """

    crew_dir = Path(forgotten.__file__).resolve().parent
    if roots is None:
        roots = sorted(crew_dir.glob("dispatch*.py"))
    return [path.name for path in roots if needle in path.read_text(encoding="utf-8")]


def test_no_dispatch_module_consults_the_forgotten_list() -> None:
    """The report is read by a reader; nothing that refuses consults it."""

    assert _modules_naming("forgotten_runs") == []
    # The positive control: the same instrument, pointed at the module that
    # does name it. Without it the absence above would be a failed search.
    crew_dir = Path(forgotten.__file__).resolve().parent
    assert _modules_naming(
        "forgotten_runs", roots=[crew_dir / "forgotten_runs.py"]
    ) == ["forgotten_runs.py"]
