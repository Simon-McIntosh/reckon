"""A live run never reads dead, unpromoted or unreadable.

The producer reads a run from two places that disagree: the pointer names the
launcher's pid, while the work is a child the supervisor records in the run
directory's own worker record. Every misread here comes from trusting the
pointer's pid for a question the worker record owns — a live worker behind a
gone supervisor read as "process gone, worktree carries N commits", a launch
flicker read as abandoned, a resumed run behind an old complete manifest read
as unpromoted, and a wrapper launcher's exit read as the worker's. The same
shape reaches the manifest reader: a run one minute into its turn has written
its orientation and nothing else, and the reader refused the body as if it were
a delivery that failed to declare a verdict.

Each case is a stub run driven through the producer, and each has an executed
negative control: the guard is dropped and the misread must come back. The
controls are selected by ``RECKON_LIVENESS_NEGATIVE_CONTROL``, one guard per
case. The declared mutation those controls restore is logged verbatim by the
gate that runs them.

A dead control sits beside them: a genuinely dead worker whose worktree carries
commits still reads interrupted-with-retained-work, so the reading this module
reserves for work in flight was not granted to work that stopped.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import time

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, runs

HOST = socket.gethostname()

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "per case, restore the misread (drop the worker.json wait, the worker-pid "
    "probe, the resume-process probe, the wrapper-child probe, the "
    "orientation-only rule, the phase advance, the ledger-row check, or one "
    "bucket mapping) and that case must fail"
)

NEGATIVE_CONTROL = os.environ.get("RECKON_LIVENESS_NEGATIVE_CONTROL", "").strip()


@pytest.fixture(autouse=True)
def _isolated_crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))

# One mutation per guard this module relies on. A control drops exactly the
# guard its case is about, so a failure names the guard and not a neighbour.
_MUTATIONS = {
    "worker-json-wait": lambda mp: mp.setattr(
        recovery, "_PRE_SPAWN_PHASES", frozenset()
    ),
    "worker-pid-probe": lambda mp: mp.setattr(
        recovery, "_worker_record_liveness", lambda record: None
    ),
    "resume-process-probe": lambda mp: mp.setattr(
        recovery, "_worker_record_liveness", lambda record: None
    ),
    "wrapper-child-probe": lambda mp: mp.setattr(
        recovery, "_worker_record_liveness", lambda record: None
    ),
    "orientation-only-rule": lambda mp: mp.setattr(
        recovery, "_carries_orientation_write", lambda text, data: False
    ),
    "phase-advance": lambda mp: mp.setattr(
        recovery, "_observed_phase", lambda phase, **kwargs: phase
    ),
    "ledger-row-check": lambda mp: mp.setattr(
        recovery, "_ledger_run_id_reader", lambda project: (lambda: ())
    ),
    "bucket-mapping": lambda mp: mp.setattr(
        recovery,
        "FLEET_BLOCKED_STATES",
        tuple(
            sorted(
                recovery.NEEDS_ACTION - recovery.WAITING_STATES - {"blocked"}
            )
        ),
    ),
}


@contextmanager
def _control(monkeypatch: pytest.MonkeyPatch, guard: str):
    """Drop one guard, for the run whose only purpose is to fail without it."""
    if NEGATIVE_CONTROL in {guard, "all"}:
        _MUTATIONS[guard](monkeypatch)
    yield


def _pid_max() -> int:
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip())


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    return _pid_max() + 4096


def _spawn_holdable_child() -> tuple[int, int]:
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
def _live_child():
    """A genuinely running child, always reaped on the way out."""
    pid, hold_w = _spawn_holdable_child()
    try:
        yield pid
    finally:
        with contextlib.suppress(OSError):
            os.close(hold_w)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=False)


def _git_commit(repo: Path, message: str) -> None:
    ident = ("-c", "user.name=gate", "-c", "user.email=gate@example.invalid")
    _git(*ident, "add", "-A", cwd=repo)
    done = _git(*ident, "commit", "-q", "-m", message, cwd=repo)
    assert done.returncode == 0, done.stderr


def _worktree_with_commit(tmp_path: Path, name: str) -> tuple[Path, str]:
    """A real worktree carrying one commit past a recorded base."""
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    assert _git("init", "-q", "-b", "main", cwd=repo).returncode == 0
    (repo / "delivered.txt").write_text("base\n", encoding="utf-8")
    _git_commit(repo, "base")
    base = _git("rev-parse", "HEAD", cwd=repo).stdout.decode().strip()
    (repo / "delivered.txt").write_text("work\n", encoding="utf-8")
    _git_commit(repo, "work")
    return repo, base


ORIENTATION_BODY = (
    "orientation_worktree: /tmp/fixture-tree\n"
    "orientation_base_sha: 00000000000000000000000000000000000000ac\n"
    'orientation_write_paths: ["reckon/crew/recovery.py"]\n'
    "node: a-stub-node\n"
)

# The unfilled dispatch template as the prompt prints it, status line and all.
TEMPLATE_BODY = (
    "orientation_worktree: <output of pwd>\n"
    "orientation_base_sha: <output of git rev-parse HEAD>\n"
    'orientation_write_paths: ["<paths>"]\n'
    "node: a-stub-node\n"
    "status: in-progress | waiting | complete | blocked | failed\n"
    "checkpoint: <one line>\n"
)

COMPLETE_BODY = "node: a-stub-node\nstatus: complete\ncommits: []\n"


def _write_worker_record(run_id: str, pid: int, *, backend: str = "claude") -> None:
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / recovery.WORKER_RECORD_NAME).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "pid": pid,
                "pid_start_time": recovery._process_start_time(pid),
                "backend": backend,
                "argv": ["--stub"],
            }
        ),
        encoding="utf-8",
    )


def _write_exit_record(run_id: str) -> None:
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / recovery.EXIT_RECORD_NAME).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "worker_pid": None,
                "launched_at": "2026-09-25T09:00:00Z",
                "exited_at": "2026-09-25T09:30:00Z",
                "stream_records_seen": 4,
            }
        ),
        encoding="utf-8",
    )


def _pointer(
    tmp_path: Path,
    run_id: str,
    *,
    pid: int | None,
    phase: str = "working",
    manifest_body: str | None = None,
    worker_pid: int | None = None,
    worker_backend: str = "claude",
    worktree: Path | None = None,
    base_sha: str | None = None,
    stream_records: list[str] | None = None,
    write_stream: bool = True,
) -> dict:
    """One stub run shaped as a live pointer on the reading host."""
    stream = tmp_path / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    if write_stream:
        records = stream_records or ['{"type":"turn.started"}']
        stream.write_text("\n".join(records) + "\n", encoding="utf-8")
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    if manifest_body is not None:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(manifest_body, encoding="utf-8")
    if worker_pid is not None:
        _write_worker_record(run_id, worker_pid, backend=worker_backend)
    return {
        "run_id": run_id,
        "project": "liveness-fixture",
        "session": "s21-coord",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
        "phase": phase,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(tmp_path / f"{run_id}.stderr.log"),
        "process_alive": None,
        "pid": pid,
        "pid_start_time": recovery._process_start_time(pid) if pid else None,
        "launcher_host": HOST,
        "worktree": str(worktree) if worktree is not None else str(tmp_path / "tree"),
        "base_sha": base_sha or "",
    }


MOMENT = time.time()


def _classify(pointer: dict) -> dict:
    return recovery.classify_pointer(pointer, now_seconds=MOMENT)


def _snapshot(pointer: dict) -> dict:
    return recovery._watch_snapshot(pointer, moment=MOMENT, stall_seconds=3600)


# ── Case 1: a live worker behind a gone supervisor ────────────────────────
# The supervisor spawns the worker and may exit before it, so the pointer pid
# answers for a process that is gone while the work continues. The worker
# record is the pointer's equal for the question "is the work still running".


def test_a_live_worker_behind_a_gone_supervisor_is_not_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _worktree_with_commit(tmp_path, "tree-first-commit")
    with _control(monkeypatch, "worker-pid-probe"), _live_child() as worker_pid:
        row = _classify(
            _pointer(
                tmp_path,
                "r-live-worker-first-commit",
                pid=_absent_pid(),
                phase="starting",
                manifest_body=ORIENTATION_BODY,
                worker_pid=worker_pid,
                worktree=repo,
                base_sha=base,
            )
        )

    assert row["process_alive"] is True
    assert row["interruption"] is None
    assert row["classification"] == "running"
    assert row["classification"] not in {"abandoned", recovery.INTERRUPTED_RUN_PHASE}


def test_a_genuinely_dead_worker_with_commits_still_reads_interrupted(
    tmp_path: Path,
) -> None:
    # The dead control the whole module is measured against: no worker record,
    # a dead pid, and one commit past base. Retained work still reads as an
    # interruption, so the probe did not disable that reading — it reserved it
    # for a worker whose own record agrees that nothing is running.
    repo, base = _worktree_with_commit(tmp_path, "tree-dead-commits")
    row = _classify(
        _pointer(
            tmp_path,
            "r-dead-with-commits",
            pid=_absent_pid(),
            phase="working",
            manifest_body=ORIENTATION_BODY,
            worktree=repo,
            base_sha=base,
        )
    )

    assert row["process_alive"] is False
    assert row["classification"] == recovery.INTERRUPTED_RUN_PHASE
    assert "dead-pid-with-retained-work" in json.dumps(row)


# ── Case 2: a launch flicker ──────────────────────────────────────────────
# The supervisor is up and has not spawned yet: no worker record, no exit, no
# stream, no manifest. A pid the table cannot name cannot prove that work
# stopped, because none has started.


def test_a_pre_spawn_launch_is_not_abandoned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _control(monkeypatch, "worker-json-wait"), _live_child() as supervisor_pid:
        row = _classify(
            _pointer(
                tmp_path,
                "r-launch-flicker",
                pid=supervisor_pid,
                phase="starting",
                write_stream=False,
            )
        )

    assert row["classification"] == "running"
    assert row["classification"] != "abandoned"


# ── Case 3: a resumed run inheriting an old complete manifest ─────────────


def test_a_resumed_run_with_an_old_complete_manifest_is_not_unpromoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _control(monkeypatch, "resume-process-probe"), _live_child() as worker_pid:
        row = _classify(
            _pointer(
                tmp_path,
                "r-resumed-old-complete",
                pid=_absent_pid(),
                phase="starting",
                manifest_body=COMPLETE_BODY,
                worker_pid=worker_pid,
            )
        )

    assert row["classification"] == "running"
    assert row["classification"] not in {"promotable", "scoring", "abandoned"}


# ── Case 4: a codex wrapper launcher ──────────────────────────────────────
# The launcher is a wrapper that exits; the child it spawned is the run. The
# supervisor writes the worker record for every backend, so the wrapper adds
# nothing: the child's pid is asked, and it is alive.


def test_a_codex_run_whose_pid_is_a_wrapper_is_not_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _control(monkeypatch, "wrapper-child-probe"), _live_child() as worker_pid:
        row = _classify(
            _pointer(
                tmp_path,
                "r-codex-wrapper",
                pid=_absent_pid(),
                phase="starting",
                worker_pid=worker_pid,
                worker_backend="codex",
                stream_records=[
                    '{"type":"thread.started"}',
                    '{"type":"turn.started"}',
                ],
            )
        )

    assert row["process_alive"] is True
    assert row["classification"] == "running"
    assert row["classification"] not in {"abandoned", recovery.INTERRUPTED_RUN_PHASE}


# ── Cases 5 and 9: the orientation write and the unfilled template ────────


def test_an_orientation_only_manifest_is_a_run_in_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _control(monkeypatch, "orientation-only-rule"):
        row = _classify(
            _pointer(
                tmp_path,
                "r-orientation-only",
                pid=_absent_pid(),
                phase="starting",
                manifest_body=ORIENTATION_BODY,
                write_stream=False,
            )
        )

    assert row["classification"] == "running"
    assert row["classification"] != "unreadable"


def test_a_live_run_holding_the_unfilled_template_is_not_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _control(monkeypatch, "worker-pid-probe"), _live_child() as worker_pid:
        row = _classify(
            _pointer(
                tmp_path,
                "r-template-only-live",
                pid=_absent_pid(),
                phase="starting",
                manifest_body=TEMPLATE_BODY,
                worker_pid=worker_pid,
            )
        )

    assert row["process_alive"] is True
    assert row["classification"] == "running"
    assert row["classification"] != "unreadable"


# ── Case 8: the pointer phase advances with the evidence ──────────────────


def test_a_pointer_phase_advances_from_starting_to_working_to_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-phase-advance"
    with _control(monkeypatch, "phase-advance"), _live_child() as supervisor_pid:
        started = _pointer(
            tmp_path, run_id, pid=supervisor_pid, phase="starting", write_stream=False
        )
        started_row = _classify(started)
        started_snapshot = _snapshot(started)
        _write_worker_record(run_id, supervisor_pid)
        working = _pointer(tmp_path, run_id, pid=supervisor_pid, phase="starting")
        working_row = _classify(working)
        working_snapshot = _snapshot(working)
        os.kill(supervisor_pid, signal.SIGTERM)
        os.waitpid(supervisor_pid, 0)
        _write_exit_record(run_id)
        complete = _pointer(
            tmp_path,
            run_id,
            pid=supervisor_pid,
            phase="starting",
            manifest_body=COMPLETE_BODY,
        )
        complete_row = _classify(complete)
        complete_snapshot = _snapshot(complete)
        phases = (
            started_row["effective_phase"],
            working_row["effective_phase"],
            complete_row["effective_phase"],
        )
        states = (
            started_snapshot["state"],
            working_snapshot["state"],
            complete_snapshot["state"],
        )

    assert phases == ("starting", "working", "complete")
    assert states[0] == "dispatched"
    assert states[1] == "working"
    assert states[2] == "completed_unpromoted"


# ── A concluded turn with no terminal manifest ────────────────────────────


def test_a_worker_that_ended_after_a_result_record_is_resumable(
    tmp_path: Path,
) -> None:
    repo, base = _worktree_with_commit(tmp_path, "tree-ended-result")
    run_id = "r-ended-without-manifest"
    _write_exit_record(run_id)
    snapshot = _snapshot(
        _pointer(
            tmp_path,
            run_id,
            pid=_absent_pid(),
            phase="working",
            worktree=repo,
            base_sha=base,
            stream_records=[
                '{"type":"turn.started"}',
                '{"type":"result","subtype":"success"}',
            ],
        )
    )

    assert snapshot["state"] == "blocked"
    assert snapshot["recovery_classification"] == "ended-without-manifest"
    assert snapshot["recovery"] == "resume"
    assert snapshot["state"] != "stalled"


# ── Case 6: a departure is promoted only with a ledger row ────────────────


def _departure_word(*, use_ledger: bool) -> str:
    known = {
        "r-departure": {
            "run_id": "r-departure",
            "node": "n",
            "session": "s",
            "state": "complete",
            "detail": "",
            "needs_help_complete": None,
        }
    }
    reader = (
        recovery._ledger_run_id_reader("liveness-fixture")
        if use_ledger
        else None
    )
    events, _ = recovery.fleet_transitions(
        known,
        {},
        ledger_run_ids=reader,
    )
    return events[0][2]


def test_a_vanished_run_without_a_ledger_row_is_not_called_promoted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_ids = {"r-departure"}
    monkeypatch.setattr(
        recovery,
        "_ledger_run_id_reader",
        lambda _project: lambda: ledger_ids,
    )
    with _control(monkeypatch, "ledger-row-check"):
        assert _departure_word(use_ledger=True) == "promoted"
        ledger_ids.clear()
        assert _departure_word(use_ledger=True) != "promoted"


def test_a_departure_with_no_ledger_evidence_keeps_the_promoted_word() -> None:
    # A caller that supplies no ledger cannot tell a promotion from a vanish,
    # so the reading is qualified rather than asserted: the word stays
    # promoted, and no reader is told a run was withdrawn on no evidence.
    assert _departure_word(use_ledger=False) == "promoted"


# ── Case 7: every emitted word falls in exactly one bucket ────────────────


def _bucket(word: str, classification: str | None = None) -> str | None:
    from reckon.crew.ticker import _bucket_of

    return _bucket_of(word, classification)


def test_every_emitted_word_maps_to_exactly_one_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if NEGATIVE_CONTROL in {"bucket-mapping", "all"}:
        _MUTATIONS["bucket-mapping"](monkeypatch)
    emitted = set()
    emitted_pairs = set()
    repo, base = _worktree_with_commit(tmp_path, "tree-buckets")
    with _live_child() as worker_pid:
        stubs = [
            _pointer(tmp_path, "b-live", pid=worker_pid),
            _pointer(
                tmp_path,
                "b-gone-commits",
                pid=_absent_pid(),
                worktree=repo,
                base_sha=base,
                manifest_body=ORIENTATION_BODY,
            ),
            _pointer(
                tmp_path,
                "b-flicker",
                pid=_absent_pid(),
                phase="starting",
                write_stream=False,
            ),
            _pointer(
                tmp_path,
                "b-orientation",
                pid=_absent_pid(),
                manifest_body=ORIENTATION_BODY,
                write_stream=False,
            ),
        ]
        for stub in stubs:
            snapshot = _snapshot(stub)
            emitted.add(snapshot["state"])
            emitted_pairs.add(
                (snapshot["state"], snapshot["recovery_classification"])
            )
    # These are the producer's row states, not the separate recovery verbs
    # carried beside them. A ticker bucket accepts the row state and consults
    # the recovery classification only for the special held reading.
    emitted |= set(recovery.FLEET_WORKING_STATES)
    emitted |= set(recovery.FLEET_UNPROMOTED_STATES)
    emitted |= set(recovery.FLEET_WAITING_STATES)
    emitted |= set(recovery.FLEET_BLOCKED_STATES)
    emitted_pairs |= {
        ("blocked", "ended-without-manifest"),
        ("blocked", "refused-at-admission"),
    }

    missing = sorted(
        pair for pair in emitted_pairs if _bucket(pair[0], pair[1]) is None
    )
    assert missing == [], f"classifications with no bucket: {missing}"
    by_classification: dict[str, set[str | None]] = {}
    for state, classification in emitted_pairs:
        by_classification.setdefault(classification, set()).add(
            _bucket(state, classification)
        )
    assert all(
        len(buckets) == 1 and None not in buckets
        for buckets in by_classification.values()
    )

    partition = (
        set(recovery.FLEET_WORKING_STATES),
        set(recovery.FLEET_UNPROMOTED_STATES),
        set(recovery.FLEET_WAITING_STATES),
        set(recovery.FLEET_BLOCKED_STATES),
    )
    overlaps = sorted(
        word
        for word in set().union(*partition)
        if sum(word in cell for cell in partition) > 1
    )
    assert overlaps == [], f"words in more than one bucket: {overlaps}"
