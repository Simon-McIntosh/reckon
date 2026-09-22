"""A live process outranks every manifest-derived unreadable classification.

The classifier's own comment states the invariant: the process is consulted
before the manifest reading, so a run whose process is alive is classified
from that life and never as unreadable. The hole was the incomplete-wait arm:
a manifest declaring ``status: waiting`` whose wait declaration is missing or
invalid read unreadable even for a positively live process, while its siblings
— the terminal-report deferral and the refuse-to-parse manifest arm — already
consulted liveness. What this locks in: every manifest state a live run can
reach classifies from liveness or from the declared wait, never as unreadable,
and the incomplete-wait reading stays unreadable only once the process is gone.

The other direction of the same precedence is locked here too: the deliverable
is read before the process. A run whose manifest is complete and whose stored
review parses is terminal-with-deliverable whatever its exit looked like — a
killed process does not turn it into an abandoned run — while a killed process
with no manifest and no review is still abandoned.
"""

from __future__ import annotations

import contextlib
import os
import socket
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, review

HOST = socket.gethostname()


def _pid_max() -> int:
    """The largest process id the kernel will allocate; beyond it, no pid."""
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip())


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    return _pid_max() + 4096


def _stat_fields(pid: int) -> list[str]:
    """The per-process stat fields as the kernel writes them."""
    stat = Path(f"/proc/{pid}/stat").read_text()
    return stat[stat.rfind(")") + 2 :].split()


def _spawn_holdable_child() -> tuple[int, int]:
    """Fork a child that stays alive and exits when its hold pipe closes."""
    ready_r, ready_w = os.pipe()
    hold_r, hold_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(ready_r)
        os.close(hold_w)
        try:
            os.write(ready_w, b"x")
            os.close(ready_w)
            os.read(hold_r, 1)
        finally:
            os._exit(0)
    os.close(ready_w)
    os.close(hold_r)
    os.read(ready_r, 1)
    os.close(ready_r)
    return pid, hold_w


@contextmanager
def _owned_child() -> int:
    """A genuinely running child, always reaped on the way out."""
    pid, hold_w = _spawn_holdable_child()
    hold_open = True
    try:
        assert _stat_fields(pid)[0] in ("R", "S"), _stat_fields(pid)
        yield pid
    finally:
        if hold_open:
            with contextlib.suppress(OSError):
                os.close(hold_w)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)


def _manifest_body(case: str) -> str | None:
    """The manifest text for one state a live run can carry, or None for absent."""
    if case == "absent":
        return None
    if case == "unwritten":
        return "node: run\nstatus: <complete|blocked|failed>\n"
    if case == "complete":
        return "node: run\nstatus: complete\ncommits: 0123456789abcdef\n"
    if case == "blocked":
        return "node: run\nstatus: blocked\nblockers: implementation pending\n"
    if case == "failed":
        return "node: run\nstatus: failed\nblockers: the work failed\n"
    if case == "valid-wait":
        now = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
        return (
            "node: run\n"
            "status: waiting\n"
            "wait_condition: scheduler job 42\n"
            'wait_probe: ["scheduler-status", "--job", "42"]\n'
            'wait_terminal: ["COMPLETED"]\n'
            f"wait_started_at: {now}\n"
            "resume_brief: collect the scheduler result\n"
        )
    if case == "invalid-wait":
        # Declares a wait but omits wait_condition and wait_probe, so the
        # declaration is incomplete and no reader can act on it at rest.
        return (
            "node: run\n"
            "status: waiting\n"
            'wait_terminal: ["COMPLETED"]\n'
            "resume_brief: collect the scheduler result\n"
        )
    raise AssertionError(f"unknown manifest case {case!r}")


def _pointer(tmp_path: Path, run_id: str, *, pid: int | None, case: str) -> dict:
    """One record shaped as a live pointer on the reading host."""
    stream = tmp_path / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n')
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    body = _manifest_body(case)
    if body is not None:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(body, encoding="utf-8")
    return {
        "run_id": run_id,
        "project": "fixture-project",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
        "phase": "working",
        "created_at": "2026-09-07T00:00:00Z",
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(tmp_path / f"{run_id}.stderr.log"),
        "process_alive": None,
        "pid": pid,
        "pid_start_time": recovery._process_start_time(pid) if pid else None,
        "launcher_host": HOST,
    }


# Every manifest state a live run can reach, and the classification that state
# must yield for a positively live process. ``absent`` and ``unwritten`` read
# running as no delivered word exists; the terminal words are deferred by the
# live process; a declared wait keeps its own waiting classification (the
# manifest is the authority for what a parked worker waits on); an incomplete
# wait declaration reads running rather than unreadable — the shape that read
# unreadable at base.
_LIVE_STATES = [
    ("absent", "running"),
    ("unwritten", "running"),
    ("complete", "running"),
    ("blocked", "running"),
    ("failed", "running"),
    ("valid-wait", "waiting"),
    ("invalid-wait", "running"),
]


@pytest.mark.parametrize(("case", "expected"), _LIVE_STATES)
def test_a_live_process_is_never_unreadable_across_manifest_states(
    tmp_path: Path, case: str, expected: str
) -> None:
    # The falsifier of the invariant, ranged over every manifest state a live
    # run can carry. The process is genuinely alive on this host, so liveness
    # is proven and no classification path may return unreadable.
    with _owned_child() as pid:
        row = recovery.classify_pointer(
            _pointer(tmp_path, f"r-live-{case}", pid=pid, case=case),
            now_seconds=time.time(),
        )
    assert row["process_alive"] is True
    assert row["liveness_proven"] is True
    assert row["classification"] == expected
    assert row["classification"] != "unreadable"


def test_a_live_incomplete_wait_reads_running_with_the_refusal_text(
    tmp_path: Path,
) -> None:
    # The refuse-to-read manifest keeps its failure text on the running row;
    # the incomplete wait declaration must behave the same way, so a reader of
    # the live run still sees what the declaration is missing.
    with _owned_child() as pid:
        row = recovery.classify_pointer(
            _pointer(
                tmp_path, "r-live-invalid-detail", pid=pid, case="invalid-wait"
            ),
            now_seconds=time.time(),
        )
    assert row["classification"] == "running"
    assert row["manifest_status"] == "waiting"
    assert "wait_condition" in (row["manifest_error"] or "")


def test_a_gone_process_keeps_the_invalid_wait_unreadable(tmp_path: Path) -> None:
    # The dead control: the same incomplete-wait body with a process the table
    # cannot name is still unreadable, so the fix did not disable the reading —
    # it reserved it for the manifest at rest.
    row = recovery.classify_pointer(
        _pointer(
            tmp_path, "r-gone-invalid-wait", pid=_absent_pid(), case="invalid-wait"
        ),
        now_seconds=time.time(),
    )
    assert row["process_alive"] is False
    assert row["liveness_proven"] is True
    assert row["classification"] == "unreadable"
    assert "wait_condition" in row["manifest_error"]
    assert "wait_condition" in row["detail"]


# ── The deliverable outranks the process ──────────────────────────────────
# The same precedence read from the other side. A killed worker that had
# already written its manifest and its review delivered the run before it died,
# so the classifier reads that delivery before it reads the process and never
# folds the run into abandoned: the stored phase is the last writer's label,
# not evidence of what the run left behind.


@pytest.fixture()
def review_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the durable review store from the operator's crew state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _parsed_review(project: str, run_id: str) -> dict:
    """A review that parses with every scored dimension present."""
    emitted = "\n".join(
        f"SCORE {dimension}: 18" for dimension in review.REVIEW_DIMENSIONS
    )
    record = review.parse_review(emitted)
    record.update(
        {
            "project": project,
            "reviewed_run_id": run_id,
            "review_run_id": f"{run_id}-review",
        }
    )
    return record


# The phases a killed pointer has frozen on: the last writer's label, not
# evidence of anything. A death mid-work freezes on a non-terminal phase and a
# death after the worker's final turn can freeze on a terminal one, so neither
# shape may decide the classification.
_KILLED_EXIT_SHAPES = ("working", "complete", "failed")


@pytest.mark.parametrize("phase", _KILLED_EXIT_SHAPES)
def test_a_killed_process_does_not_abandon_the_delivered_run(
    tmp_path: Path, review_home: Path, phase: str
) -> None:
    # The falsifier of this precedence: a complete manifest and a parsed review
    # behind a process the table has killed. Every exit shape yields the same
    # delivered reading, because the delivery is what the classifier reads and
    # the phase is only the last writer's label.
    run_id = f"r-killed-delivered-{phase}"
    pointer = _pointer(tmp_path, run_id, pid=_absent_pid(), case="complete")
    pointer["phase"] = phase
    review.store_review(_parsed_review(pointer["project"], run_id))

    row = recovery.classify_pointer(pointer, now_seconds=time.time())

    assert row["process_alive"] is False
    assert row["liveness_proven"] is True
    assert row["classification"] == "promotable"
    assert row["classification"] != "abandoned"


@pytest.mark.parametrize("phase", _KILLED_EXIT_SHAPES)
def test_a_killed_process_does_not_abandon_a_delivered_review_run(
    tmp_path: Path, review_home: Path, phase: str
) -> None:
    # A review-role run's deliverable is the review it wrote for another run,
    # so no stored review of this run exists or is required: its own completed
    # manifest is the delivery.
    run_id = f"r-killed-review-{phase}"
    pointer = _pointer(tmp_path, run_id, pid=_absent_pid(), case="complete")
    pointer["phase"] = phase
    pointer["role"] = "review"

    row = recovery.classify_pointer(pointer, now_seconds=time.time())

    assert row["process_alive"] is False
    assert row["classification"] == "promotable"
    assert row["classification"] != "abandoned"


def test_a_killed_run_with_a_complete_manifest_and_no_review_waits_in_scoring(
    tmp_path: Path, review_home: Path
) -> None:
    # The manifest is read before the process even when the review is missing:
    # the run is waiting for the review promotion requires, and the row names
    # that rather than the death.
    row = recovery.classify_pointer(
        _pointer(
            tmp_path, "r-killed-awaiting-review", pid=_absent_pid(), case="complete"
        ),
        now_seconds=time.time(),
    )

    assert row["process_alive"] is False
    assert row["classification"] == "scoring"
    assert row["classification"] != "abandoned"


def test_a_killed_run_whose_review_does_not_parse_is_not_abandoned(
    tmp_path: Path, review_home: Path
) -> None:
    # A review that is stored but unreadable is evidence the run delivered
    # something, so it cannot become an abandonment; the row keeps the run
    # waiting on a repair rather than inviting a redispatch over it.
    run_id = "r-killed-bad-review"
    pointer = _pointer(tmp_path, run_id, pid=_absent_pid(), case="complete")
    review.store_review(
        {
            "project": pointer["project"],
            "reviewed_run_id": run_id,
            "status": "unparsed",
        }
    )

    row = recovery.classify_pointer(pointer, now_seconds=time.time())

    assert row["process_alive"] is False
    assert row["classification"] == "scoring"
    assert row["classification"] != "abandoned"


@pytest.mark.parametrize("phase", _KILLED_EXIT_SHAPES)
def test_a_killed_process_with_no_manifest_and_no_review_is_still_abandoned(
    tmp_path: Path, review_home: Path, phase: str
) -> None:
    # The dead control: the same killed process with nothing delivered is still
    # abandoned, so reading the deliverable first did not disable the reading —
    # it reserved the delivered classification for a run that delivered.
    pointer = _pointer(
        tmp_path, f"r-killed-nothing-{phase}", pid=_absent_pid(), case="absent"
    )
    pointer["phase"] = phase

    row = recovery.classify_pointer(pointer, now_seconds=time.time())

    assert row["process_alive"] is False
    assert row["liveness_proven"] is True
    assert row["classification"] == "abandoned"
