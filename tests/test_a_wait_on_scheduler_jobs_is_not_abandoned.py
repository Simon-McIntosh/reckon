"""A wait on a batch of scheduler jobs is never read as abandoned while one lives.

On 12-13 November a run declared a wait whose probe ran
``python …/wait_jobs.py 1312389,1312390,1312391,1312392,1312393,1312394,1312395``
— seven jobs on ``betelgeuse``, one completed, one running and five pending on
priority. The wait was healthy. The obligations view read it as
``run-producer-gone`` and said no scheduler job was alive, because a job id was
read only from a probe that ran a scheduler command or passed a job flag. A
script handed a comma-separated batch names neither, so the scheduler was never
asked and the row asserted an absence it had never looked for.

Three readings are pinned here. A wait naming a batch is a wait on those jobs,
and the run is not abandoned while the scheduler lists any of them. A wait whose
jobs the scheduler does not list reads as nothing can lift it, and its row says
the scheduler was asked and listed none. A wait naming no job reads as before,
and its row says the scheduler was not consulted rather than that no job is
alive — the two are different facts and only one of them was measured.

Every case stubs the scheduler query itself rather than the reader that reports
its answer, so the ids the query carries are the ones the classifier derived.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, recovery_classification, recovery_wait
from reckon.crew.runs import _write_json, pointer_path, runs_dir

PROJECT = "waiting-on-a-job-batch-fixture"
SESSION = "fixture-session"
HOST = socket.gethostname()
STALE_AFTER_SECONDS = 900
NOW_SECONDS = 1_788_853_920.0
BOUND = recovery_classification.WAIT_PRODUCER_GONE_SECONDS

# The probe the run declared, in the shape that sat idle: an interpreter handed
# a script and the batch as one comma-separated operand. Its digits are not a
# job to a reader that looks only for a scheduler command or a job flag.
BATCH_JOBS = ("1312389", "1312390", "1312391")
BATCH_PROBE = ["python", "{script}", ",".join(BATCH_JOBS)]

# A probe that names no job at all: a file wait whose reference is a path.
NO_JOB_PROBE = ["cat", "{state}"]

RUN_IDS = (
    "r-batch-one-job-running",
    "r-batch-no-job-listed",
    "r-no-job-named",
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

    No process of the run is alive, which is the ordinary state of a run while
    the job it started is still running. The value the probe answers has stood
    still longer than the bound, which is what a healthy wait on a long job
    looks like, so the scheduler alone decides whether the stillness means the
    work has ended.
    """
    directory = runs_dir() / run_id
    directory.mkdir(parents=True, exist_ok=True)
    state = directory / "state.txt"
    state.write_text("RUNNING\n", encoding="utf-8")
    # The script the probe runs is a real path on this host, because a probe
    # that resolves no reference at all is refused as one that cannot fail
    # before any of this reading is reached.
    script = directory / "wait_jobs.py"
    script.write_text("import sys\nprint('PENDING')\n", encoding="utf-8")
    resolved = [part.format(state=state, script=script) for part in probe]
    manifest = directory / "manifest.md"
    manifest.write_text(
        "\n".join(
            (
                f"node: {run_id}",
                "status: waiting",
                "wait_condition: the scheduler jobs write their final rows",
                f"wait_probe: {json.dumps(resolved)}",
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
            "section": "a-declared-wait-names-the-jobs-it-rests-on",
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

    Only ``squeue`` is answered: it prints a row for each job of the batch it
    was asked about that is in ``listed``, and prints nothing when none are,
    which is what the real query does. Every other command is delegated to the
    real runner, because the classifier reaches git for the same run. Each
    query's argv is recorded so a case can assert which jobs were asked about.
    """
    real_run = subprocess.run
    asked: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        if Path(str(argv[0])).name == "squeue":
            asked.append([str(item) for item in argv])
            batch = str(argv[argv.index("-j") + 1]).split(",")
            rows = [f"{job} RUNNING fixture\n" for job in batch if job in listed]
            return subprocess.CompletedProcess(argv, 0, stdout="".join(rows), stderr="")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return asked


def _asked_jobs(asked: list[list[str]]) -> list[str]:
    """Every job id the stub was asked about, in the order it was asked."""
    return [
        job for query in asked for job in str(query[query.index("-j") + 1]).split(",")
    ]


def _row(run: dict) -> dict:
    return recovery.classify_pointer(
        run,
        now_seconds=NOW_SECONDS,
        stale_after_seconds=STALE_AFTER_SECONDS,
        condition_test=lambda _pointer, _wait: {
            "state": "unknown",
            "observed": "RUNNING",
            "answered": True,
            "detail": "the scheduler jobs are still in the queue",
        },
    )


def _row_prose(row: dict) -> str:
    """The text a reader is shown for a row, lowercased.

    The row's classification identifier and the evidence key it carries are a
    machine vocabulary shared with the duty derivation and asserted as literals
    by tests outside this node's write scope, so the word this reading is about
    is the one the row's own sentences use. A row that says the run's process
    is gone cannot be read as the project's watch producer having stopped.
    """
    return " ".join(
        str(row.get(key) or "") for key in ("detail", "next_action")
    ).lower()


def test_a_batch_wait_with_one_job_running_is_not_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One live job in the batch is what keeps this run's wait liftable.

    The probe names its jobs through a script's operand rather than through a
    scheduler command or a job flag, which is the shape that was read as naming
    nothing. The scheduler is asked, it lists one of the three, and the run is
    not called abandoned.
    """
    asked = _scheduler_stub(monkeypatch, frozenset({BATCH_JOBS[1]}))
    run = _waiting_run(RUN_IDS[0], probe=BATCH_PROBE)

    row = _row(run)

    assert _asked_jobs(asked) == list(BATCH_JOBS)
    assert row["classification"] == "waiting"
    assert row["recovery_classification"] == "waiting"
    assert row["external_wait"].get("producer_gone") is None


def test_a_batch_wait_whose_jobs_all_left_the_queue_is_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scheduler answered, and it lists none of the three.

    With no process, a value that has not moved past the bound and no job in
    the queue, nothing is left that could lift the wait — and the row says the
    scheduler was asked and listed none, which is a fact it measured.
    """
    _scheduler_stub(monkeypatch, frozenset())
    run = _waiting_run(RUN_IDS[1], probe=BATCH_PROBE)

    row = _row(run)

    assert row["classification"] == recovery_classification.PRODUCER_GONE_STATUS
    assert (
        row["recovery_classification"] == recovery_classification.PRODUCER_GONE_STATUS
    )
    assert row["external_wait"]["producer_gone"]["still_seconds"] >= BOUND
    assert row["external_wait"]["producer_gone"]["scheduler_jobs"] == list(BATCH_JOBS)
    assert "the scheduler listed none of the 3 jobs the wait names" in row["detail"]
    assert "the scheduler was not consulted" not in row["detail"]


def test_a_wait_naming_no_job_says_the_scheduler_was_not_consulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wait that names no job reads as before, and says why the silence is empty.

    The run is still read as having nothing left to lift it, because a file
    wait's probe answers the same value for as long as the wait is healthy. Its
    row must not borrow the scheduler's authority for a question the scheduler
    was never asked.
    """
    asked = _scheduler_stub(monkeypatch, frozenset())
    run = _waiting_run(RUN_IDS[2], probe=NO_JOB_PROBE)

    row = _row(run)

    assert asked == []
    assert row["classification"] == recovery_classification.PRODUCER_GONE_STATUS
    assert row["external_wait"]["producer_gone"]["scheduler_jobs"] == []
    assert "the scheduler was not consulted" in row["detail"]
    assert "no scheduler job alive" not in row["detail"]


def test_no_row_names_the_watch_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three readings name the run's own process, never the watch producer.

    The word the incident turned on is the one a row uses for what is gone.
    Naming the run's own process keeps the reading from being acted on as a
    stopped project watch producer, which is what happened for a day.
    """
    rows = []
    _scheduler_stub(monkeypatch, frozenset({BATCH_JOBS[1]}))
    rows.append(_row(_waiting_run(RUN_IDS[0], probe=BATCH_PROBE)))
    _scheduler_stub(monkeypatch, frozenset())
    rows.append(_row(_waiting_run(RUN_IDS[1], probe=BATCH_PROBE)))
    rows.append(_row(_waiting_run(RUN_IDS[2], probe=NO_JOB_PROBE)))

    for row in rows:
        assert "producer" not in _row_prose(row), row["detail"]


def test_the_batch_reader_is_the_one_the_classifier_calls() -> None:
    """One reader answers which jobs a probe names, so there is no second one.

    A batch reader beside the shared one would read a narrower set of shapes
    and silently answer nothing for the rest, and nothing is the answer that
    skips the scheduler entirely.
    """
    assert recovery_classification._wait_probe_job_ids is (
        recovery_wait._wait_probe_job_ids
    )
    source = Path(recovery_classification.__file__).read_text(encoding="utf-8")
    assert "def _wait_probe_job_ids(" not in source
