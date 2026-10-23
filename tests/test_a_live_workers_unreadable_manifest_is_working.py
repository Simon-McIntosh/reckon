"""A live worker's unreadable in-progress manifest is not drawn halted.

A worker writes its manifest by hand, in whatever shape its turn needs, and a
strict reader refuses some of those shapes: a list field joined with a separator
the parser does not accept, or a status word outside the vocabulary. Each
refusal is real and worth keeping on the row, but on a worker that is still
running it is an in-progress body the reader caught mid-turn, not a delivery
that reached its end with no verdict. Reporting that gap as ``unwritten`` draws
a working run halted and sends a coordinator to resume work in flight.

The run's own motion decides instead, from the same evidence the orientation
write is read with: a proven-live worker, or — for a run whose pid this host
cannot look up — an assistant record in its newest stream inside the stall
window the stall clock already applies. A worker whose process is gone left
that body behind as the missing verdict it may well be, and the word stands for
it.

Everything the classifier resolves through the environment is pointed at
``tmp_path`` so the real crew directories are untouched, and the live process
each fixture names is a child of this test, so liveness is proven rather than
stored.
"""

from __future__ import annotations

import importlib
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, reports, runs, ticker

HOST = socket.gethostname()

# Every fixture is placed decisively outside the two transient windows the
# classifier holds an absent verdict back for — the launch window after
# dispatch, and the window a manifest may be caught mid-rewrite. The margin is
# added to the window constants rather than written as an absolute age, so a
# window that moves takes the fixtures with it.
OUTSIDE_WINDOW_MARGIN_SECONDS = 120
DISPATCH_AGE_SECONDS = recovery.LAUNCH_WINDOW_SECONDS + OUTSIDE_WINDOW_MARGIN_SECONDS
MANIFEST_AGE_SECONDS = (
    recovery.MANIFEST_REWRITE_WINDOW_SECONDS + OUTSIDE_WINDOW_MARGIN_SECONDS
)

ASSISTANT_RECORD = '{"type":"assistant","message":{"content":[]}}\n'
ORIENTATION_KEYS = (
    "orientation_worktree: /tmp/nowhere\n"
    "orientation_base_sha: " + "0" * 40 + "\n"
    'orientation_write_paths: ["reckon/crew/recovery.py"]\n'
)

# The three in-progress bodies a live worker may hold. The first two are the
# shapes the strict reader refuses — a list field joined with ``;`` and a status
# word outside the vocabulary — and the third is a well-formed body the reader
# accepts, carried as the control that already read working before this change.
LOOSE_LIST_BODY = (
    "node: a-live-worker\nstatus: in-progress\nchanged_paths: a.py; b.py\n"
)
NON_VOCABULARY_BODY = ORIENTATION_KEYS + "status: in-progress-placeholder\n"
CHECKPOINT_BODY = "node: a-live-worker\nstatus: in-progress\ncheckpoint: mid-turn\n"

REFUSED_BODIES = {
    "loose-list-field": LOOSE_LIST_BODY,
    "non-vocabulary-status": NON_VOCABULARY_BODY,
}
ACCEPTED_BODY = {"in-progress-with-checkpoint": CHECKPOINT_BODY}

PROJECT = "unreadable-manifest-fixture"


def _pid_max() -> int:
    """The largest process id the kernel will allocate; beyond it, no pid."""
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip())


@pytest.fixture()
def live_pid():
    """A process this test owns, so the run's pid answers the process table."""
    child = subprocess.Popen(["sleep", "300"])
    try:
        yield child.pid
    finally:
        child.terminate()
        child.wait()


def _run_dir(run_id: str) -> Path:
    return Path(runs.run_dir(run_id))


def _write(path: Path, text: str, *, age_seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))


def _pointer(run_id: str, *, pid: int, body: str) -> dict:
    """One stub run as the pointer is written, aged past the transient windows."""
    directory = _run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"
    stream.write_text(ASSISTANT_RECORD, encoding="utf-8")
    manifest = directory / "manifest.md"
    _write(manifest, body, age_seconds=MANIFEST_AGE_SECONDS)
    now = time.time()
    created = recovery.datetime.fromtimestamp(
        now - DISPATCH_AGE_SECONDS, tz=recovery.timezone.utc
    ).isoformat()
    return {
        "run_id": run_id,
        "project": PROJECT,
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "40m"},
        "phase": "working",
        "attempt": 1,
        "created_at": created,
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(directory / "stderr.log"),
        "process_alive": None,
        "pid": pid,
        "pid_start_time": recovery._process_start_time(pid),
        "launcher_host": HOST,
    }


def _recover_row(pointer: dict, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Write the pointer to the fixture home and read it back through recover."""
    crew._write_json(crew.pointer_path(pointer["run_id"]), pointer)
    monkeypatch.setattr(
        importlib.import_module("reckon.crew.dispatch"),
        "observe",
        lambda _run_id, config=None: pointer,
    )
    result = recovery.recover(project=PROJECT)
    rows = [row for row in result["runs"] if row["run_id"] == pointer["run_id"]]
    assert len(rows) == 1, result["runs"]
    return rows[0]


def _pane_word(row: dict) -> str:
    return ticker._pane_word(
        {
            "recovery_classification": row["recovery_classification"],
            "to_state": row["classification"],
        }
    )


def _refusal_text(body: str) -> str:
    """The strict reader's own refusal of this body, derived rather than quoted."""
    with pytest.raises(reports.ManifestParseError) as refusal:
        reports.parse_manifest(body)
    return str(refusal.value)


@pytest.mark.parametrize("name", sorted(REFUSED_BODIES))
def test_a_live_worker_with_an_unreadable_in_progress_manifest_is_working(
    name: str,
    tmp_path: Path,
    home: Path,
    live_pid: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The defect's own shape: a worker is alive and writing, its body is one the
    # strict reader refuses, and the row must keep the classification its
    # liveness gives it while the refusal stays visible on the detail.
    body = REFUSED_BODIES[name]
    pointer = _pointer(f"r-live-{name}", pid=live_pid, body=body)

    row = _recover_row(pointer, monkeypatch)

    assert row["process_alive"] is True, row["process_alive"]
    assert row["classification"] == "running", row["classification"]
    assert _pane_word(row) == "working", row["recovery_classification"]
    # The refusal is not swallowed: the detail names what the reader refused, so
    # a coordinator still sees the body that needs rewriting.
    assert _refusal_text(body) in row["detail"], row["detail"]


def test_a_live_worker_with_an_accepted_in_progress_manifest_is_working(
    tmp_path: Path, home: Path, live_pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The control: the same live worker holding a body the reader accepts
    # already read working, and still does.
    pointer = _pointer("r-live-checkpoint", pid=live_pid, body=CHECKPOINT_BODY)

    row = _recover_row(pointer, monkeypatch)

    assert row["process_alive"] is True
    assert row["classification"] == "running"
    assert _pane_word(row) == "working", row["recovery_classification"]


@pytest.mark.parametrize("name", sorted(REFUSED_BODIES))
def test_a_gone_worker_with_an_unreadable_manifest_still_reads_halted(
    name: str, tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dead control: the same body with a process the table cannot name is
    # the delivery it may be, so the halted word stands and the resume is
    # offered. The pid is beyond the kernel's ceiling, so it names no process.
    body = REFUSED_BODIES[name]
    pointer = _pointer(f"r-gone-{name}", pid=_pid_max() + 4096, body=body)

    row = _recover_row(pointer, monkeypatch)

    assert row["process_alive"] is False, row["process_alive"]
    assert row["recovery_classification"] in {"unreadable", "unwritten"}, row[
        "recovery_classification"
    ]
    assert _pane_word(row) == "halted", row["recovery_classification"]
