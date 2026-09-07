"""Liveness is read at the moment it is classified, not carried from storage.

The measured case: a worker was mid-flight with its process genuinely running
on this host while its pointer's stored ``process_alive`` field read ``None``.
The classifier gating the deferral that guards a live worker's terminal report
consumed that stored field, so the condition ``alive is True`` was false and
the deferral — the arm that prevents a premature verdict — was bypassed in
exactly the case it exists for.

What this locks in: the classifier asks the process table at classification
time, but only when the pointer's recorded launching host is the reading host.
A pid is meaningful only on the machine that issued it and the crew home is
shared across login nodes, so a foreign-host read fabricates a verdict in both
directions; there the stored answer is carried and the row marks it unproven.
A pointer with no pid behaves as it always has. The classification for every
combination of stored field and real process state is stated as a table below,
so a later reader can see which of the two the code consults.
"""

from __future__ import annotations

import contextlib
import os
import socket
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from reckon.crew import recovery


def _pid_max() -> int:
    """The largest process id the kernel will allocate; beyond it, no pid."""
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip())


def _stat_fields(pid: int) -> list[str]:
    """The per-process stat fields as the kernel writes them."""
    stat = Path(f"/proc/{pid}/stat").read_text()
    return stat[stat.rfind(")") + 2 :].split()


def _wait_for_state(pid: int, state: str, within: float = 10.0) -> bool:
    """Whether the pid's kernel state becomes ``state`` within the window."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        fields = _stat_fields(pid)
        if fields and fields[0] == state:
            return True
        time.sleep(0.01)
    return False


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
def _owned_child(*, zombie: bool) -> int:
    """A real launched process kept alive (or let die into the Z state).

    ``zombie=False`` yields a genuinely running pid; ``zombie=True`` closes the
    hold so the child exits and stays as a not-yet-reaped entry in the table,
    the exact window a zero-signal probe alone would report alive. The child is
    always reaped on the way out.
    """
    pid, hold_w = _spawn_holdable_child()
    hold_open = True
    try:
        if zombie:
            os.close(hold_w)
            hold_open = False
            assert _wait_for_state(pid, "Z"), f"child {pid} did not reach zombie state"
        else:
            assert _stat_fields(pid)[0] in ("R", "S"), _stat_fields(pid)
        yield pid
    finally:
        if hold_open:
            with contextlib.suppress(OSError):
                os.close(hold_w)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    return _pid_max() + 4096


def _pointer(
    tmp_path: Path,
    run_id: str,
    *,
    stored_alive: bool | None,
    pid: int | None,
    launcher_host: str | None,
    status: str | None = None,
    phase: str = "working",
) -> dict:
    """One record shaped as a live pointer, with the fields under test set."""
    stream = tmp_path / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n')
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    if status:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        # The blockers phrase mirrors the measured orientation manifest, so a
        # terminal word has prose to explain itself.
        manifest.write_text(
            f"node: {run_id}\n"
            f"status: {status}\n"
            "commits: 0123456789abcdef\n"
            "blockers: implementation pending\n"
        )
    return {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
        "phase": phase,
        "created_at": "2026-09-07T00:00:00Z",
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "stderr_path": str(tmp_path / f"{run_id}.stderr.log"),
        "process_alive": stored_alive,
        "pid": pid,
        "pid_start_time": recovery._process_start_time(pid) if pid else None,
        "launcher_host": launcher_host,
    }


HOST = socket.gethostname()
OTHER_HOST = f"{HOST}.different-host.invalid"

# Every combination of stored field and real process state, and which of the
# two the classification consults. ``running`` and ``zombie`` are the kernel
# states of genuinely launched children; ``absent`` is a pid past the kernel
# ceiling. The consulted column is the one the falsifier is about: on a host
# match the process table wins whatever the stored field says, and anywhere
# else the stored answer is carried and marked unproven without a lookup.
_MATCHED_TABLE = [
    # stored, process_state, expected_alive, expected_proven
    (None, "running", True, True),
    (True, "running", True, True),
    (False, "running", True, True),
    (None, "absent", False, True),
    (True, "absent", False, True),
    (False, "absent", False, True),
    (None, "zombie", False, True),
    (True, "zombie", False, True),
    (False, "zombie", False, True),
]
_UNMATCHED_TABLE = [
    # stored, expected_alive, expected_proven ; process_state is deliberately
    # never consulted, so a running child row still expects the stored value
    (None, None, False),
    (True, True, False),
    (False, False, False),
]


@pytest.mark.parametrize(
    ("stored", "process_state", "expected_alive", "expected_proven"), _MATCHED_TABLE
)
def test_matched_host_consults_the_process_table(
    tmp_path, stored, process_state, expected_alive, expected_proven
) -> None:
    # A pointer whose recorded launching host is the reading host is judged by
    # the process table at classification time: the stored field is ignored in
    # every direction, so a stale stored value can neither keep a dead run
    # alive nor call a live run dead. The ``running`` and ``zombie`` states are
    # the kernel states of genuinely launched children, not fixtures.
    if process_state == "absent":
        pid = _absent_pid()
        row = recovery.classify_pointer(
            _pointer(
                tmp_path,
                f"r-matched-{stored}-{process_state}",
                stored_alive=stored,
                pid=pid,
                launcher_host=HOST,
            ),
            now_seconds=time.time(),
        )
    else:
        with _owned_child(zombie=(process_state == "zombie")) as pid:
            row = recovery.classify_pointer(
                _pointer(
                    tmp_path,
                    f"r-matched-{stored}-{process_state}",
                    stored_alive=stored,
                    pid=pid,
                    launcher_host=HOST,
                ),
                now_seconds=time.time(),
            )
    assert row["process_alive"] is expected_alive
    assert row["liveness_proven"] is expected_proven


@pytest.mark.parametrize("stored", [None, True, False])
def test_unmatched_host_carries_the_stored_answer_without_a_lookup(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    stored,
) -> None:
    # A pointer whose recorded launching host differs from the reading host is
    # classified from its stored field alone: no process lookup is performed,
    # and the row carries that the answer is unproven. Asserting the absence of
    # the lookup rather than only its result — the process table function is
    # forbidden to run for a foreign pid.
    def _forbidden_lookup(pid):
        raise AssertionError(f"process lookup performed for foreign pid {pid!r}")

    monkeypatch.setattr(recovery, "process_alive", _forbidden_lookup)
    row = recovery.classify_pointer(
        _pointer(
            tmp_path,
            f"r-unmatched-{stored}",
            stored_alive=stored,
            pid=_absent_pid(),
            launcher_host=OTHER_HOST,
        ),
        now_seconds=time.time(),
    )
    assert row["process_alive"] is stored
    assert row["liveness_proven"] is False


@pytest.mark.parametrize("stored", [None, True, False])
def test_unrecorded_host_is_not_proven_either(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    stored,
) -> None:
    # A pointer with no recorded launching host cannot be shown to belong to
    # this host (crew home is shared across login nodes), so it is treated like
    # a differing host: stored answer, unproven, no lookup.
    def _forbidden_lookup(pid):
        raise AssertionError(
            f"process lookup performed without a recorded host {pid!r}"
        )

    monkeypatch.setattr(recovery, "process_alive", _forbidden_lookup)
    row = recovery.classify_pointer(
        _pointer(
            tmp_path,
            f"r-nohost-{stored}",
            stored_alive=stored,
            pid=_absent_pid(),
            launcher_host=None,
        ),
        now_seconds=time.time(),
    )
    assert row["process_alive"] is stored
    assert row["liveness_proven"] is False


def test_the_measured_case_a_live_writer_defers_its_terminal_word(
    tmp_path,
) -> None:
    # The measured arbitration restored: stored field says nothing, the
    # launching host is this host and its pid is running, so the classification
    # is alive and a terminal manifest word (here ``failed``) is a report in
    # progress rather than the run's outcome.
    with _owned_child(zombie=False) as pid:
        row = recovery.classify_pointer(
            _pointer(
                tmp_path,
                "r-live-defers",
                stored_alive=None,
                pid=pid,
                launcher_host=HOST,
                status="failed",
            ),
            now_seconds=time.time(),
        )
    assert row["process_alive"] is True
    assert row["liveness_proven"] is True
    assert row["classification"] == "running"
    assert row["manifest_status"] is None, "a live writer's report is not an outcome"
    assert row["manifest_reported_status"] == "failed"


def test_an_absent_process_makes_the_terminal_word_the_outcome(tmp_path) -> None:
    # The same pointer with a pid naming no process classifies as though not
    # alive, so the terminal word regains its meaning.
    row = recovery.classify_pointer(
        _pointer(
            tmp_path,
            "r-absent-outcome",
            stored_alive=None,
            pid=_absent_pid(),
            launcher_host=HOST,
            status="failed",
        ),
        now_seconds=time.time(),
    )
    assert row["process_alive"] is False
    assert row["liveness_proven"] is True
    assert row["classification"] == "failed"
    assert row["manifest_status"] == "failed"


def test_a_defunct_entry_classifies_not_alive_and_composes_with_the_zombie_fix(
    tmp_path,
) -> None:
    # The same pointer with a pid naming a defunct entry classifies as though
    # not alive: the point-of-use probe answers no for a zombie, so the deferral
    # does not hold a finished run forever and the terminal word becomes the
    # outcome — the narrow zombie reading, not the old alive-for-a-zombie
    # answer this composes with.
    with _owned_child(zombie=True) as pid:
        row = recovery.classify_pointer(
            _pointer(
                tmp_path,
                "r-zombie-outcome",
                stored_alive=None,
                pid=pid,
                launcher_host=HOST,
                status="failed",
            ),
            now_seconds=time.time(),
        )
    assert row["process_alive"] is False
    assert row["liveness_proven"] is True
    assert row["classification"] == "failed"
    assert row["manifest_status"] == "failed"


def test_no_pid_at_all_behaves_as_it_does_today(tmp_path) -> None:
    # A pointer with no pid is not something a process table can be asked
    # about, so the stored answer is used on a matched host exactly as it was
    # before this change.
    for stored in (None, True, False):
        row = recovery.classify_pointer(
            _pointer(
                tmp_path,
                f"r-nopid-{stored}",
                stored_alive=stored,
                pid=None,
                launcher_host=HOST,
            ),
            now_seconds=time.time(),
        )
        assert row["process_alive"] is stored
        assert row["liveness_proven"] is False
