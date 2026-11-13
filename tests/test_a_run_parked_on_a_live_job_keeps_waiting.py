"""A run parked on a job the scheduler still lists is never producer-gone.

A wait over a long scheduler job answers the same state for hours: a run
re-parked on ``sacct -j 1307334 -o State`` reported RUNNING on every sweep,
which is what a healthy wait looks like. The stillness bound alone therefore
cannot separate a working wait from a dead producer, and the scheduler is the
only witness that can. On 6 November a follower read that wait as halted with
"with no scheduler job alive" while ``sacct`` reported the job RUNNING.

The classifier asked the scheduler only when the probe's own head was
``squeue``, so a ``sacct`` vector and an interpreter's code string named no job
to it and the scheduler was never asked at all. The job id is now read by the
shared probe reader, which sees through a ``sacct`` vector and through an
interpreter's code string alike, so every shape a wait is written in names its
job.

Every case stubs the scheduler query itself rather than the reader that reports
its answer, so the job id the classifier derives is the one the query carries:
a stub that answered without being asked the right question would pass while
the defect stood.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, recovery_classification
from reckon.crew.runs import _write_json, pointer_path, runs_dir

PROJECT = "parked-on-a-live-job-fixture"
SESSION = "fixture-session"
HOST = socket.gethostname()
STALE_AFTER_SECONDS = 900
NOW_SECONDS = 1_788_853_920.0
BOUND = recovery_classification.WAIT_PRODUCER_GONE_SECONDS

# The probe the run was re-parked with, verbatim: a ``sacct`` query whose head
# is not ``squeue``, so the retired head-only reader named no job for it.
SACCT_PROBE = ["sacct", "-n", "-X", "-P", "-j", "1307334", "-o", "State"]
SACCT_JOB = "1307334"

# The probe that sat idle for eight hours, verbatim: a ``python3 -c`` code
# string that reaches ``squeue`` for job 1306915.
INTERPRETER_PROBE = [
    "python3",
    "-c",
    (
        "import subprocess; p=subprocess.run(['squeue','-h','-j','1306915']); "
        "print('PENDING' if p.stdout.strip() else 'TERMINAL')"
    ),
]
INTERPRETER_JOB = "1306915"

RUN_IDS = (
    "r-parked-job-still-running",
    "r-parked-job-left-the-queue",
    "r-parked-interpreter-job-running",
)


@pytest.fixture(autouse=True)
def _the_real_pointer_directory_is_untouched() -> None:
    """No case may reach the workstation's own live-pointer directory."""
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    mine = [real_live / f"{run_id}.json" for run_id in RUN_IDS]

    assert not any(path.exists() for path in mine)
    yield
    assert not any(path.exists() for path in mine)


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip()) + 4096


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _history(*entries: tuple[float, str, bool]) -> list[dict[str, object]]:
    """A recorded probe history: (seconds before now, observed, answered)."""
    return [
        {"at": _stamp(NOW_SECONDS - ago), "observed": observed, "answered": answered}
        for ago, observed, answered in entries
    ]


def _waiting_run(run_id: str, *, probe: list[str]) -> dict:
    """One parked run with the manifest and pointer a declared wait needs.

    The value the probe answers has stood still for longer than the bound in
    every case, which is the healthy steady state of a wait on a long job; the
    scheduler alone decides whether that stillness means anything.
    """
    directory = runs_dir() / run_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(
        "\n".join(
            (
                f"node: {run_id}",
                "status: waiting",
                "wait_condition: the scheduler job writes its final rows",
                f"wait_probe: {json.dumps(probe)}",
                f"wait_terminal: {json.dumps(['COMPLETED', 'FAILED'])}",
                "resume_brief: collect the rows and finish the report",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    stamp = NOW_SECONDS - 10.0
    os.utime(manifest, (stamp, stamp))
    _write_json(directory / "attempt.json", {"attempt": 1, "at": _stamp(NOW_SECONDS)})
    tree = runs_dir() / f"{run_id}-tree"
    tree.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(runs_dir()),
        "worktree": str(tree),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "backend": "codex",
        "role": "implement",
        "session": SESSION,
        "session_id": SESSION,
        "created_at": _stamp(NOW_SECONDS - 4 * 3600),
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "manifest_path": str(manifest),
        "phase": "waiting",
        "attempt": 1,
        "process_alive": False,
        "pid": _absent_pid(),
        "launcher_host": HOST,
        "wait_probe_history": _history(
            (BOUND + 300, "RUNNING", True),
            (BOUND + 60, "RUNNING", True),
            (60, "RUNNING", True),
        ),
        "node": {
            "id": run_id,
            "plan": "plan-a",
            "section": "a-broken-wait-reaches-its-coordinator",
            "time_budget": "30m",
            "write_paths": ["reckon/crew/recovery_classification.py"],
        },
    }
    _write_json(pointer_path(run_id), record)
    return record


def _scheduler_stub(
    monkeypatch: pytest.MonkeyPatch, listed: frozenset[str]
) -> list[list[str]]:
    """Stub the scheduler query, and report what it was asked.

    Only ``squeue`` is answered: it lists the job when it is in ``listed`` and
    prints nothing when it is not, which is what the real query does. Every
    other command is delegated to the real runner, because the classifier
    reaches git for the same run. The argv of each scheduler query is recorded
    so a case can assert which job was asked about.
    """
    real_run = subprocess.run
    asked: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        if Path(str(argv[0])).name == "squeue":
            asked.append([str(item) for item in argv])
            job_id = str(argv[argv.index("-j") + 1])
            row = f"{job_id} RUNNING fixture\n" if job_id in listed else ""
            return subprocess.CompletedProcess(argv, 0, stdout=row, stderr="")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return asked


def _asked_jobs(asked: list[list[str]]) -> list[str]:
    """The job ids the stub was asked about, in the order it was asked."""
    return [query[query.index("-j") + 1] for query in asked]


def _row(run: dict) -> dict:
    return recovery.classify_pointer(
        run,
        now_seconds=NOW_SECONDS,
        stale_after_seconds=STALE_AFTER_SECONDS,
        condition_test=lambda _pointer, _wait: {
            "state": "unknown",
            "observed": "RUNNING",
            "answered": True,
            "detail": "the scheduler job is still in the queue",
        },
    )


def test_a_sacct_wait_over_a_job_still_running_keeps_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The value stood still, but the job it reports on is the scheduler's.

    A run with no process is normal while the job it started is running, and a
    long job answers RUNNING on every sweep. The scheduler is asked about the
    job the probe names, whatever command the probe's own head is.
    """
    asked = _scheduler_stub(monkeypatch, frozenset({SACCT_JOB}))
    run = _waiting_run(RUN_IDS[0], probe=SACCT_PROBE)

    row = _row(run)

    assert _asked_jobs(asked) == [SACCT_JOB]
    assert row["classification"] == "waiting"
    assert row["recovery_classification"] == "waiting"
    assert row["external_wait"].get("producer_gone") is None


def test_a_sacct_wait_whose_job_left_the_queue_is_producer_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scheduler answered, and it does not list the job.

    With no process, a value that has not moved past the bound and no job in
    the queue, nothing is left that could lift the wait.
    """
    _scheduler_stub(monkeypatch, frozenset())
    run = _waiting_run(RUN_IDS[1], probe=SACCT_PROBE)

    row = _row(run)

    assert row["classification"] == recovery_classification.PRODUCER_GONE_STATUS
    assert (
        row["recovery_classification"] == recovery_classification.PRODUCER_GONE_STATUS
    )
    assert row["external_wait"]["producer_gone"]["still_seconds"] >= BOUND
    assert "the run's own process is gone" in row["detail"]


def test_an_interpreter_wait_over_a_job_still_running_keeps_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that reaches the scheduler through a code string names its job.

    This is the shape that sat idle for eight hours. Its job id lives inside an
    interpreter's code string, so a reader that takes the vector's head at face
    value finds no job and never asks.
    """
    asked = _scheduler_stub(monkeypatch, frozenset({INTERPRETER_JOB}))
    run = _waiting_run(RUN_IDS[2], probe=INTERPRETER_PROBE)

    row = _row(run)

    assert _asked_jobs(asked) == [INTERPRETER_JOB]
    assert row["classification"] == "waiting"
    assert row["recovery_classification"] == "waiting"
    assert row["external_wait"].get("producer_gone") is None


def test_the_classifier_holds_no_job_id_reader_of_its_own() -> None:
    """One reader answers which job a probe names, so there is no second one.

    A job-id reader beside the shared one reads a narrower set of shapes and
    silently answers None for the rest, and None is the answer that skips the
    scheduler entirely. The name the classifier calls is the shared reader, and
    the classifier's own source declares nothing under it.
    """
    from reckon.crew import recovery_wait

    assert (
        recovery_classification._wait_probe_job_id is recovery_wait._wait_probe_job_id
    )
    source = Path(recovery_classification.__file__).read_text(encoding="utf-8")
    assert "def _wait_probe_job_id(" not in source
