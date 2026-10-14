"""A resume of a run whose worker still lives is refused, naming that process.

A live worker holds the run's session, so a second attempt started over it
collides with the worker already writing and can reclassify the run from
working to blocked on its way out. Both doors that can start a worker — a
hand-typed ``crew resume`` and the automatic resume sweep — therefore refuse
while the run's process is still there, and the refusal names the pid so the
coordinator reading it can check the process table rather than take the
refusal on faith.

The worker is not always the pid the pointer names. A supervised launch
records the supervisor on the pointer and the worker it spawned in the run
directory's own worker record, and a supervisor that exits ahead of its worker
takes the pointer's pid with it while the worker goes on committing. The
reading here is the same host-gated composition every other surface reads, and
the refusal reports which pid supplied it.

The supervisor's exit record does not license a resume while a worker still
answers for itself: a launcher that ends early can leave a record beside a run
that is still being worked, and the record is an observation of the end only
where nothing is still running.

The declared mutation, applied verbatim in a scratch copy: with the liveness
check removed from the manual resume, these cases launch a second attempt and
fail.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, resumption, runs
from reckon.crew.dispatch import resume_plan
from reckon.crew.dispatch_launch import _prepare_attempt_records
from reckon.crew.node import CrewError
from reckon.crew.runs import _write_json, pointer_path

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "With the liveness check removed from the manual resume, "
    "tests/test_resume_refuses_a_live_worker.py launches a second attempt and fails."
)

PROJECT = "fixture-project"
HOST = socket.gethostname()
NOW_SECONDS = datetime(2026, 9, 29, 12, 0, tzinfo=UTC).timestamp()
MANIFEST_NS = int(datetime(2026, 9, 29, 10, 0, tzinfo=UTC).timestamp()) * 1_000_000_000
CONDITION = "the scheduler job has finished"

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "fixture-agent",
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


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    ceiling = int(Path("/proc/sys/kernel/pid_max").read_text().strip())
    return ceiling + 4096


@contextmanager
def _live_worker():
    """A real, running child process, bounded by its own timeout and reaped by pid.

    The process sleeps on its own clock, so it ends even if this test's
    teardown never runs, and it is killed and waited on by its pid on the way
    out so no child is left behind for the next case to see.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield child.pid
    finally:
        child.kill()
        child.wait()


_REAL_HOME = Path(os.path.expanduser("~")) / ".config" / "reckon" / "crew"
_RUN_IDS = (
    "r-manual-live-worker",
    "r-sweep-live-worker",
    "r-live-under-record",
    "r-launch-in-flight",
    "r-launch-over-live",
)


def _case_artifacts(crew_home: Path) -> list[Path]:
    """The paths this file's cases leave under ``crew_home``.

    Named for the run ids above, so the same list describes the real crew home
    and a stand-in: the assertion below is the receipt that this file wrote
    nothing into a live fleet's home while it ran.
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


def _waiting_manifest(path: Path, awaited: Path) -> None:
    path.write_text(
        "status: waiting\n"
        f"wait_condition: {CONDITION}\n"
        f"wait_file: {json.dumps([str(awaited)])}\n"
        "resume_brief: read the job's output and continue\n",
        encoding="utf-8",
    )
    os.utime(path, ns=(MANIFEST_NS, MANIFEST_NS))


def _pointer(
    tmp_path: Path,
    run_id: str,
    *,
    pid: int | None,
    worker_pid: int | None = None,
    exit_record: bool = False,
    parked: bool = False,
) -> dict:
    """A stopped run, differing only in which process the evidence names."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / f"{run_id}-tree"
    worktree.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / f"{run_id}.md"
    if parked:
        awaited = tmp_path / f"{run_id}-condition-met"
        awaited.write_text("done\n", encoding="utf-8")
        _waiting_manifest(manifest, awaited)
    else:
        manifest.write_text(
            "node: resume-refuses-a-live-worker\nstatus: waiting\n", encoding="utf-8"
        )
    if worker_pid is not None:
        (directory / recovery.WORKER_RECORD_NAME).write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "attempt": 1,
                    "pid": worker_pid,
                    "pid_start_time": recovery._process_start_time(worker_pid),
                    "launched_at": "2026-09-29T09:59:30Z",
                    "backend": "alpha",
                    "argv": ["fixture-agent", "exec"],
                }
            ),
            encoding="utf-8",
        )
    if exit_record:
        # A record written while a launcher ended early, as the supervisor's
        # signal short-circuit leaves it: the run's worker is still running.
        (directory / recovery.EXIT_RECORD_NAME).write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "attempt": 1,
                    "recorded_by": "supervisor",
                    "worker_pid": worker_pid,
                    "launched_at": "2026-09-29T09:59:30Z",
                    "exited_at": "2026-09-29T09:59:33Z",
                    "exit_code": None,
                    "signal": 15,
                    "signal_name": "SIGTERM",
                    "stream_records_seen": 4,
                    "last_record_type": "thread.started",
                    "ended_during": "working",
                }
            ),
            encoding="utf-8",
        )
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "backend": "alpha",
        "launch": "cli",
        "argv": ["fixture-agent", "exec"],
        "role": "implement",
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
        "launcher_host": HOST,
        "node": {
            "id": run_id,
            "role": "implement",
            "time_budget": "20m",
            "write_paths": [],
        },
    }
    _write_json(pointer_path(run_id), record)
    return record


def _classification(run_id: str) -> str:
    return str(
        recovery.classify_pointer(runs.read_pointer(run_id)).get("classification")
    )


# --- The manual door: a hand-typed resume ---------------------------------


def test_a_manual_resume_of_a_live_worker_is_refused_naming_its_pid(home) -> None:
    """The worker the run's own record names is alive, so nothing may start."""
    with _live_worker() as worker_pid:
        _pointer(home, "r-manual-live-worker", pid=_absent_pid(), worker_pid=worker_pid)

        with pytest.raises(CrewError) as raised:
            resume_plan("r-manual-live-worker", "continue", config=CONFIG)

        refusal = str(raised.value)
        assert str(worker_pid) in refusal, refusal
        assert "alive" in refusal, refusal
        # The refusal is a refusal and not a stop: the run is still the live
        # worker's, and it must keep reading that way to every later reader.
        assert _classification("r-manual-live-worker") == "running"


def test_a_manual_resume_of_a_live_pointer_process_is_refused(home) -> None:
    """The pointer's own pid answering alive is the case the guard always held."""
    with _live_worker() as pid:
        _pointer(home, "r-live-under-record", pid=pid)

        with pytest.raises(CrewError) as raised:
            resume_plan("r-live-under-record", "continue", config=CONFIG)

        refusal = str(raised.value)
        assert "still has a live process" in refusal, refusal
        assert "before resuming" in refusal, refusal
        assert _classification("r-live-under-record") == "running"


# --- The automatic door: the resume sweep ----------------------------------


def _sweep_launching(monkeypatch: pytest.MonkeyPatch, pointers: list[dict]):
    """Run one real sweep, with a recorded launcher standing in for the spawn."""
    launches: list[dict] = []

    def launcher(plan, **kwargs):
        launches.append({"plan": plan, **kwargs})
        return 424242

    monkeypatch.setattr(resumption, "list_live", lambda **_kwargs: list(pointers))
    monkeypatch.setattr(resumption, "_claimed_write_paths", lambda _pointer: [])
    monkeypatch.setattr(resumption, "dispatch_awaiting_reviews", lambda **_kwargs: {})
    report = resumption.sweep(
        PROJECT,
        config=CONFIG,
        dry_run=False,
        launcher=launcher,
        now=datetime.fromtimestamp(NOW_SECONDS, tz=UTC),
    )
    return report, launches


def test_the_sweep_refuses_a_live_pointer_process_before_any_spawn(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parked run whose supervisor still lives is refused before any spawn.

    The message is the launcher's own, held word for word by the prediction
    parity test; what this case pins is that the sweep meets it, starts
    nothing, and leaves the run reading as it did.
    """
    with _live_worker() as pid:
        pointer = _pointer(tmp_path, "r-sweep-live-under-record", pid=pid, parked=True)
        before = _classification("r-sweep-live-under-record")
        report, launches = _sweep_launching(monkeypatch, [pointer])

        assert launches == []
        assert report["resumed"] == []
        skipped = {row["run_id"]: row for row in report["skipped"]}
        entry = skipped["r-sweep-live-under-record"]
        assert entry["reason"] == "resume-refused", entry
        assert "still has a live process" in entry["detail"], entry
        # The refusal starts nothing and stops nothing: the run reads exactly
        # as it did before the sweep touched it, while its worker still lives.
        assert _classification("r-sweep-live-under-record") == before


def test_an_early_exit_record_does_not_license_a_resume_while_the_worker_lives(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record written beside a worker that still runs does not end the attempt.

    The shape is the launcher that ended by signal while the worker it adopted
    went on: the record names the signal and sits beside a run whose own worker
    record still answers alive. The record is evidence of the end only where
    nothing still runs, so the sweep refuses, names the worker's pid, and
    launches nothing.
    """
    with _live_worker() as worker_pid:
        pointer = _pointer(
            tmp_path,
            "r-live-under-record",
            pid=_absent_pid(),
            worker_pid=worker_pid,
            exit_record=True,
            parked=True,
        )
        before = _classification("r-live-under-record")
        report, launches = _sweep_launching(monkeypatch, [pointer])

        assert launches == []
        assert report["resumed"] == []
        entry = next(
            row for row in report["skipped"] if row["run_id"] == "r-live-under-record"
        )
        assert entry["reason"] == "resume-refused", entry
        assert str(worker_pid) in entry["detail"], entry
        assert _classification("r-live-under-record") == before


# --- The window between a resume's check and its recorded attempt ----------
#
# The launch of a resumption is not atomic with the check that licenses it: the
# pointer is read, the plan composed, the process spawned, and only then is the
# attempt's pid folded onto the pointer. A mark on the pointer closes that
# window, so a second door reading inside it refuses against the launch rather
# than against the attempt the launch has not recorded yet.


def test_a_resume_while_a_launch_is_in_flight_starts_nothing_and_keeps_its_advice(
    home, tmp_path: Path
) -> None:
    """A launch already under way is refused, and the refused advice is kept.

    The refusal names the launch and its process, starts nothing, and leaves the
    run's recorded attempt and pids as they were. The advice the refused resume
    carried is written beside the run rather than dropped, because the caller
    that composed it is gone by the time the launch it lost to has recorded
    anything.
    """
    with _live_worker() as launcher_pid:
        record = _pointer(tmp_path, "r-launch-in-flight", pid=_absent_pid())
        record["resume_launch_in_flight"] = {
            "pid": launcher_pid,
            "at": "2026-09-29T10:00:05Z",
        }
        _write_json(pointer_path("r-launch-in-flight"), record)
        before = runs.read_pointer("r-launch-in-flight")
        classification_before = _classification("r-launch-in-flight")

        with pytest.raises(CrewError) as raised:
            resume_plan(
                "r-launch-in-flight", "finish the census then promote", config=CONFIG
            )

        refusal = str(raised.value)
        assert "already has a resume launching" in refusal, refusal
        assert str(launcher_pid) in refusal, refusal
        # Nothing started and nothing was reclassified: the live attempt's own
        # fields are the ones the pointer still carries.
        after = runs.read_pointer("r-launch-in-flight")
        assert after["attempt"] == before["attempt"]
        assert after["pid"] == before["pid"]
        assert after["phase"] == before["phase"]
        assert _classification("r-launch-in-flight") == classification_before
        # The advice survives the refusal, for the resume that comes next.
        kept = resumption.refused_advice_path("r-launch-in-flight")
        assert (
            kept.read_text(encoding="utf-8").strip() == "finish the census then promote"
        )


def test_the_next_resume_carries_the_advice_a_refusal_kept(
    home, tmp_path: Path
) -> None:
    """The advice a refused resume held reaches the prompt of the resume after it.

    The launch mark is gone by then — the launcher that set it has ended — so the
    next resume proceeds and the kept advice is composed into its prompt ahead of
    its own, which is the only place the lost instruction survives.
    """
    with _live_worker() as launcher_pid:
        record = _pointer(tmp_path, "r-launch-in-flight", pid=_absent_pid())
        record["resume_launch_in_flight"] = {
            "pid": launcher_pid,
            "at": "2026-09-29T10:00:05Z",
        }
        record["argv"] = ["claude", "-p"]
        _write_json(pointer_path("r-launch-in-flight"), record)
        with pytest.raises(CrewError):
            resume_plan(
                "r-launch-in-flight", "finish the census then promote", config=CONFIG
            )

    # The launcher is gone, so the run is liftable again.
    record = runs.read_pointer("r-launch-in-flight")
    record.pop("resume_launch_in_flight", None)
    _write_json(pointer_path("r-launch-in-flight"), record)

    # A backend whose command reckon can translate: this case composes a real
    # plan, so the only thing under test is what the prompt carries.
    config = {
        **CONFIG,
        "backends": {
            "alpha": {**CONFIG["backends"]["alpha"], "command": "claude"},
        },
    }
    plan = resume_plan("r-launch-in-flight", "carry on", config=config)

    assert "finish the census then promote" in plan.stdin_text, plan.stdin_text
    assert "carry on" in plan.stdin_text, plan.stdin_text
    # Delivered once: the stored copy is consumed by the resume that carried it.
    assert not resumption.refused_advice_path("r-launch-in-flight").exists()


def test_a_second_launch_over_a_live_attempt_leaves_its_records_untouched(
    home, tmp_path: Path
) -> None:
    """A launch that meets a live attempt is refused before it retires anything.

    Starting an attempt archives the canonical worker and exit records and clears
    their names, because those names are the classifier's surface for the attempt
    running now. Doing that over a live attempt reports a working run as halted
    and unlinks the only record of its worker. So the launch is refused before
    the attempt marker or either record is written, which is what leaves the
    pointer's phase and pids, the worker record, and the run's classification
    exactly as the second launch found them.
    """
    with _live_worker() as worker_pid:
        _pointer(
            tmp_path, "r-launch-over-live", pid=_absent_pid(), worker_pid=worker_pid
        )
        directory = runs.run_dir("r-launch-over-live")
        worker_record = directory / recovery.WORKER_RECORD_NAME
        worker_before = worker_record.read_text(encoding="utf-8")
        before = runs.read_pointer("r-launch-over-live")
        classification_before = _classification("r-launch-over-live")

        with pytest.raises(CrewError) as raised:
            _prepare_attempt_records(
                directory,
                run_id="r-launch-over-live",
                attempt=2,
                attempt_kind="resume",
                attempt_started_at="2026-09-29T10:05:00Z",
            )

        assert "is still running" in str(raised.value), str(raised.value)
        after = runs.read_pointer("r-launch-over-live")
        assert after["phase"] == before["phase"]
        assert after["pid"] == before["pid"]
        assert after["attempt"] == before["attempt"]
        assert worker_record.read_text(encoding="utf-8") == worker_before
        assert _classification("r-launch-over-live") == classification_before
        # Refused before the marker went down, so no later reader is told this
        # run has an attempt that the refusal never started.
        assert not (directory / "attempt.json").exists()
