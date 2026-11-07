"""A refusal of a second worker names the attempt whose process it found.

A run that delivered is reviewed, the review raises a blocking finding, and the
reflex resumes the run to answer it. A coordinator who then types ``crew
resume`` meets a refusal it cannot act on: the sentence names no attempt, so it
reads as a stale process and the coordinator stops whatever is running, cutting
short a repair round that would have delivered by itself.

So all three doors that refuse a second worker — the launcher's
:func:`resume_plan`, the sweep's :func:`_launcher_refusal`, and the
stream-reading gate — compose their message through one function, and that
message names the attempt from the run's own records: its number, pid and
launch time, its kind, whether a resume is a repair round the reflex opened, and
the age of its newest stream record. A detail the records do not hold is left
out rather than guessed, and a process no record attributes keeps the sentence
the guard has always given.

The declared mutation, applied verbatim in a scratch copy: composing the
message at one site only fails the parity case.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon.crew import resumption, runs
from reckon.crew.dispatch import resume_plan
from reckon.crew.node import CrewError
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "fixture-project"
HOST = socket.gethostname()
FOREIGN_HOST = "a-different-node"
LAUNCHED_AT = "2026-11-07T06:25:56Z"
CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "claude",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
        },
    },
    "roles": {"implement": {}},
    "budget": {
        "utilisation_ceiling_pct": 100,
        "resume_reserve_pct": 5,
        "exhausted_statuses": [],
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

REPAIR_ADVICE = "Repair round: round-1-abc\nanswer the blocking finding\n"

_REAL_HOME = Path(os.path.expanduser("~")) / ".config" / "reckon" / "crew"
_RUN_IDS = (
    "r-refused-repair-round",
    "r-refused-coordinator-resume",
    "r-refused-session-walk",
    "r-refused-on-another-host",
    "r-refused-late-round",
    "r-refused-borrowed-stream",
)


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip()) + 4096


def _start_time(pid: int) -> str | None:
    """The kernel start tick a worker record carries.

    Read from the ledger's own reader rather than restated, so a live pid is not
    read as a recycled number by the reuse check the liveness owner applies.
    """
    from reckon.crew.recovery import _process_start_time

    return _process_start_time(pid)


@contextmanager
def _live_worker(*extra_argv: str):
    """A real, running child process, reaped by pid on the way out.

    The process sleeps on its own clock, so it ends even if this test's teardown
    never runs. ``extra_argv`` is what lands in its command line, which is how a
    worker is found by the lane's session mark rather than by a pid.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)", *extra_argv],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield child.pid
    finally:
        child.kill()
        child.wait()


def _case_artifacts(crew_home: Path) -> list[Path]:
    """The paths this file's cases leave under ``crew_home``.

    Named for the run ids above, so the same list describes the real crew home
    and a stand-in: the assertion in the fixture is the receipt that this file
    wrote nothing into a live fleet's home while it ran.
    """
    return [
        *(crew_home / "runs" / run_id for run_id in _RUN_IDS),
        *(crew_home / "live" / f"{run_id}.json" for run_id in _RUN_IDS),
    ]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run against a temporary crew home, and prove the real one untouched."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield config_home
    landed = [path for path in _case_artifacts(_REAL_HOME) if path.exists()]
    assert not landed, f"the real crew home carries this file's run paths: {landed}"


def _stream(path: Path, *, assistant: bool) -> None:
    """Write a stream, optionally carrying the assistant turn a worker leaves."""
    lines = [json.dumps({"type": "thread.started"})]
    if assistant:
        lines.append(json.dumps({"type": "assistant", "text": "working"}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pointer(
    tmp_path: Path,
    run_id: str,
    *,
    pid: int | None,
    attempt: int,
    attempt_kind: str,
    launcher_host: str = HOST,
    repair_round: bool = False,
    session_id: str = "",
) -> dict:
    """A run whose attempts are on disk, differing only in what they record."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / f"{run_id}-tree"
    worktree.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / f"{run_id}.md"
    manifest.write_text("node: refused-resume\nstatus: working\n", encoding="utf-8")
    stream = directory / "resume-1.jsonl"
    _stream(stream, assistant=launcher_host != HOST)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "backend": "alpha",
        "launch": "cli",
        "argv": ["claude", "exec"],
        "role": "implement",
        "session_id": session_id,
        "phase": "working",
        "attempt": attempt,
        "attempt_kind": attempt_kind,
        "created_at": "2026-11-07T05:24:38+00:00",
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(directory / "resume-1.stderr.log"),
        "worktree": str(worktree),
        "process_alive": None,
        "pid": pid,
        "pid_start_time": _start_time(pid) if pid is not None else None,
        "launcher_host": launcher_host,
        "node": {
            "id": run_id,
            "role": "implement",
            "time_budget": "20m",
            "write_paths": [],
        },
    }
    if repair_round:
        record["repair_rounds"] = {"round_id": "round-1-abc", "count": 1, "attempts": 1}
    _write_json(pointer_path(run_id), record)
    return record


def _attempt_records(
    run_id: str,
    attempt: int,
    *,
    pid: int,
    attempt_kind: str,
) -> None:
    """The attempt's own receipts: its worker record and its identity record."""
    directory = runs.run_dir(run_id)
    (directory / f"attempt-{attempt}-worker.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "attempt": attempt,
                "pid": pid,
                "pid_start_time": _start_time(pid),
                "launched_at": LAUNCHED_AT,
                "backend": "alpha",
                "argv": ["claude", "exec"],
            }
        ),
        encoding="utf-8",
    )
    (directory / "attempt.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "attempt": attempt,
                "attempt_kind": attempt_kind,
                "attempt_started_at": LAUNCHED_AT,
            }
        ),
        encoding="utf-8",
    )


def _both_doors(run_id: str, advice: str = REPAIR_ADVICE) -> tuple[str, str]:
    """The message each of the two launch doors gives for one run."""
    with pytest.raises(CrewError) as launcher:
        resume_plan(run_id, advice, config=CONFIG)
    mirror = resumption._launcher_refusal(runs.read_pointer(run_id), config=None)
    assert mirror is not None, "the sweep's door admitted what the launcher refused"
    return str(launcher.value), str(mirror)


def test_a_repair_round_attempt_is_named(home: Path, tmp_path: Path) -> None:
    """The live attempt is the reflex's repair round, and the message says so.

    This is the case the lead reported: the run delivered, a review raised a
    blocking finding, and the reflex resumed it as attempt 2. A coordinator's
    resume is refused over that attempt, so the refusal must name it — and say
    that the round delivers by itself, because the coordinator's next move is to
    wait for it rather than stop it.
    """
    run_id = "r-refused-repair-round"
    with _live_worker() as pid:
        _pointer(
            tmp_path,
            run_id,
            pid=pid,
            attempt=2,
            attempt_kind="resume",
            repair_round=True,
        )
        _attempt_records(run_id, 2, pid=pid, attempt_kind="resume")

        launcher_text, sweep_text = _both_doors(run_id)

    for refusal in (launcher_text, sweep_text):
        assert "still has a live process" in refusal, refusal
        assert "before resuming" in refusal, refusal
        assert "attempt 2" in refusal, refusal
        assert f"pid {pid}" in refusal, refusal
        assert f"launched {LAUNCHED_AT}" in refusal, refusal
        assert "a resume" in refusal, refusal
        assert "repair round" in refusal, refusal
        assert "delivers by itself" in refusal, refusal


def test_a_coordinator_resume_is_named_without_the_repair_wording(
    home: Path, tmp_path: Path
) -> None:
    """An attempt no round opened is named, and is not called a repair round.

    The same shape with no opened round on the run: naming it is still what the
    coordinator needs, and claiming a round opened that did not would send them
    to wait for a delivery nothing is going to make.
    """
    run_id = "r-refused-coordinator-resume"
    with _live_worker() as pid:
        _pointer(tmp_path, run_id, pid=pid, attempt=2, attempt_kind="resume")
        _attempt_records(run_id, 2, pid=pid, attempt_kind="resume")

        launcher_text, sweep_text = _both_doors(run_id, advice="continue the task")

    for refusal in (launcher_text, sweep_text):
        assert "attempt 2" in refusal, refusal
        assert f"pid {pid}" in refusal, refusal
        assert "a resume" in refusal, refusal
        assert "repair round" not in refusal, refusal


def test_a_live_process_found_by_its_session_invents_no_attempt(
    home: Path, tmp_path: Path
) -> None:
    """A worker no receipt records keeps the sentence alone.

    The session walk finds a live process carrying the run's session id when
    every recorded pid is dead. Nothing in the records attributes that process
    to an attempt, so the refusal names none rather than picking the nearest
    receipt and calling it the live one.
    """
    run_id = "r-refused-session-walk"
    with _live_worker("fixture-session") as pid:
        _pointer(
            tmp_path,
            run_id,
            pid=_absent_pid(),
            attempt=2,
            attempt_kind="resume",
            session_id="fixture-session",
        )

        launcher_text, sweep_text = _both_doors(run_id)

    for refusal in (launcher_text, sweep_text):
        assert "still has a live process" in refusal, refusal
        assert "attempt" not in refusal, refusal
        assert str(pid) not in refusal, refusal


def test_both_launch_doors_give_the_same_message(home: Path, tmp_path: Path) -> None:
    """Parity: one composer, so the two doors cannot word the refusal apart."""
    run_id = "r-refused-repair-round"
    with _live_worker() as pid:
        _pointer(
            tmp_path,
            run_id,
            pid=pid,
            attempt=2,
            attempt_kind="resume",
            repair_round=True,
        )
        _attempt_records(run_id, 2, pid=pid, attempt_kind="resume")

        launcher_text, sweep_text = _both_doors(run_id)

    assert launcher_text == sweep_text


def test_an_unknown_liveness_stream_names_the_moving_attempt(
    home: Path, tmp_path: Path
) -> None:
    """A worker this host cannot look up is named by the stream it is writing.

    The run was launched elsewhere, so no pid answers here and no exit was
    recorded. Its newest stream carries an assistant turn written now, which is
    a worker producing output: the refusal must name that attempt, give the age
    of its newest record, and say its liveness could not be read on this host —
    it must not call the attempt live, and must not say its process was found
    gone.
    """
    run_id = "r-refused-on-another-host"
    _pointer(
        tmp_path,
        run_id,
        pid=_absent_pid(),
        attempt=2,
        attempt_kind="resume",
        launcher_host=FOREIGN_HOST,
        session_id="fixture-session",
    )
    _attempt_records(run_id, 2, pid=_absent_pid(), attempt_kind="resume")

    refusal = resumption._observed_end_refusal(runs.read_pointer(run_id))

    assert refusal is not None, "a worker writing now licenses no resume"
    text = str(refusal)
    assert "liveness unknown" in text, text
    assert "Attempt 2" in text, text
    assert "a resume" in text, text
    assert "newest stream record is" in text, text
    assert "could not be read on this host" in text, text
    assert "live process" not in text, text
    assert "process gone" not in text, text

    # The same refusal through the launcher and its prediction, which share the
    # composer rather than restating it.
    with pytest.raises(CrewError) as launcher:
        resume_plan(run_id, "continue the task", config=CONFIG)
    assert str(launcher.value) == text
    mirror = resumption._launcher_refusal(runs.read_pointer(run_id), config=None)
    assert mirror is not None and str(mirror) == text


def test_a_round_opened_after_the_pointer_read_is_still_named(
    home: Path, tmp_path: Path
) -> None:
    """The round record is read afresh, so a round opened since the read counts.

    A sweep holds a pointer it read before the review reflex opened the round.
    The refusal must still say the round delivers by itself, because telling the
    coordinator to stop is wrong in exactly the case the section was written
    about. The record handed in here carries no round, and the live pointer on
    disk does, which is the difference the owner's fresh read makes.
    """
    run_id = "r-refused-late-round"
    with _live_worker() as pid:
        _pointer(
            tmp_path,
            run_id,
            pid=pid,
            attempt=2,
            attempt_kind="resume",
            repair_round=True,
        )
        _attempt_records(run_id, 2, pid=pid, attempt_kind="resume")
        stale = dict(runs.read_pointer(run_id))
        stale.pop("repair_rounds", None)

        text = str(resumption.live_process_refusal(stale, reading="alive"))

    assert "attempt 2" in text, text
    assert "repair round" in text, text


def test_an_unidentifiable_stream_claims_no_age(home: Path, tmp_path: Path) -> None:
    """An attempt whose own stream cannot be found is dated by nothing.

    The run's newest stream is the first attempt's, but the attempt being named
    is the second, so calling that age this attempt's newest record would date
    it by a neighbour. The message leaves the age out instead of guessing it.
    """
    run_id = "r-refused-borrowed-stream"
    with _live_worker() as pid:
        _pointer(tmp_path, run_id, pid=pid, attempt=2, attempt_kind="resume")
        _attempt_records(run_id, 2, pid=pid, attempt_kind="resume")
        _stream(runs.run_dir(run_id) / "stream.jsonl", assistant=True)
        record = dict(runs.read_pointer(run_id))
        record["log_path"] = ""

        text = str(resumption.live_process_refusal(record, reading="alive"))

    assert "attempt 2" in text, text
    assert "newest stream record" not in text, text
