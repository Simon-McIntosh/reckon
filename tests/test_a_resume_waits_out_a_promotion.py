"""A resume refuses while its run is being promoted, and never deadlocks.

Promotion holds a lock named for the run for the whole of its promotion, and
takes the run's pointer lock inside it. The live-worker check that runs before
that lock cannot close the window after it: a resume starting once the check
has passed used to rewrite the pointer underneath a promotion that had already
decided what to do with it, which is how a landing observed a pointer change
mid-promotion.

The resume therefore takes the promotion lock first, without waiting, and takes
the run's own pointer lock inside it. A promotion in progress is a refusal
rather than a wait. The order is promotion's own, so the pair can never hold
what the other waits for: with the wait removed from the outer take, the
inverse order is the only way to deadlock, and the bound asserted below is what
would catch it.

The cases drive ``_resume`` with a launcher seam, so no worker is started and
there is nothing left running. The promotion arm holds the two locks exactly as
``promotion.py`` does, in its order, and the deadlock arm starts one of each
together against a five-second bound.

The declared mutation is the base behaviour: a resume that takes only the run's
pointer lock proceeds while the promotion lock is held.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from reckon.crew import promotion, resumption, runs
from reckon.crew.node import CrewError

DECLARED_MUTATION = (
    "At the base revision the resume proceeds while the promotion lock is held"
)

RUN_ID = "r-resume-beside-a-promotion"
# The name promotion gives its per-run lock (reckon/crew/promotion.py). Spelled
# here rather than read through the production helper, so the base arm holds the
# lock the base code actually races against instead of failing on a new name.
PROMOTION_LOCK = f"promotion-{RUN_ID}"
DEADLOCK_BUDGET_SECONDS = 5.0
ROUNDS = 25


def _launcher(pid: int) -> Callable[..., int]:
    """A launcher seam standing in for the supervisor spawn, returning ``pid``."""

    def launcher(plan: Any, **kwargs: Any) -> int:
        return pid

    return launcher


@pytest.fixture(autouse=True)
def _registry_is_clean() -> Any:
    """No case inherits a launch registry entry left by another."""
    resumption._LAUNCHES_IN_THIS_PROCESS.clear()
    yield
    resumption._LAUNCHES_IN_THIS_PROCESS.clear()


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A resumable run with a temporary crew home and nothing outside it."""
    config_home = tmp_path / "config"
    (config_home / "crew").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text("node: stub-node\nstatus: waiting\n", encoding="utf-8")
    runs._write_json(
        runs.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": "",
            "repo": str(worktree),
            "worktree": str(worktree),
            "backend": "alpha",
            "launch": "cli",
            "phase": "working",
            "pid": None,
            "manifest_path": str(manifest),
            "log_path": str(directory / "stream.jsonl"),
            "attempt": 1,
            "created_at": "2026-10-02T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        resumption, "resume_plan", lambda *a, **k: SimpleNamespace(stdin_text="")
    )
    monkeypatch.setattr(resumption, "_require_fleet_gate_open", lambda: None)

    def record_and_clear(run_id: str, **kwargs: Any) -> None:
        resumption.clear_resume_launching(run_id)

    monkeypatch.setattr(resumption, "record_resumption", record_and_clear)


def _resume(run_id: str, pid: int) -> dict[str, Any]:
    return resumption._resume(
        run_id,
        runs.read_pointer(run_id),
        config=None,
        launcher=_launcher(pid),
        advice="continue",
    )


def test_a_resume_refuses_while_a_promotion_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pointer is the promotion's while it holds the run.

    The promotion lock is taken exactly as a promotion takes it, and a resume
    attempted inside it must refuse — naming the promotion — and must leave the
    pointer byte-for-byte as the promotion left it.
    """
    _fixture(tmp_path, monkeypatch)
    pointer = runs.pointer_path(RUN_ID)
    before = pointer.read_bytes()
    launched: list[int] = []

    with (
        runs._pointer_lock(PROMOTION_LOCK),
        pytest.raises(CrewError, match="being promoted"),
    ):
        resumption._resume(
            RUN_ID,
            runs.read_pointer(RUN_ID),
            config=None,
            launcher=lambda plan, **kwargs: launched.append(1) or 424242,
            advice="continue",
        )

    assert launched == [], "a refused resume must not start a worker"
    assert pointer.read_bytes() == before, "the promotion's pointer was rewritten"


def test_a_resume_proceeds_with_no_promotion_in_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The added outer take refuses nothing when no promotion holds the run."""
    _fixture(tmp_path, monkeypatch)

    launched = _resume(RUN_ID, 424242)

    assert launched["pid"] == 424242, launched
    current = runs.read_pointer(RUN_ID)
    assert resumption.launch_in_flight(current) is False, current


def test_a_promotion_and_a_resume_started_together_never_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both orders of arrival terminate: the lock order keeps one wait only.

    Each round starts a promotion-shaped critical section and a resume at the
    same moment. Whichever arrives first, both threads must finish well inside
    the bound; a resume that took the pointer lock before the promotion lock
    would hang here against a promotion holding both.
    """
    _fixture(tmp_path, monkeypatch)
    stuck: list[str] = []
    outcomes: list[str] = []
    lock = threading.Lock()

    def promotion_arm(barrier: threading.Barrier) -> None:
        barrier.wait(timeout=DEADLOCK_BUDGET_SECONDS)
        try:
            with promotion._promotion_lock(RUN_ID), runs._pointer_lock(RUN_ID):
                time.sleep(0.02)
            with lock:
                outcomes.append("promoted")
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            with lock:
                stuck.append(f"promotion: {exc!r}")

    def resume_arm(barrier: threading.Barrier, round_number: int) -> None:
        barrier.wait(timeout=DEADLOCK_BUDGET_SECONDS)
        try:
            _resume(RUN_ID, 424242 + round_number)
            with lock:
                outcomes.append("resumed")
        except CrewError:
            with lock:
                outcomes.append("refused")
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            with lock:
                stuck.append(f"resume: {exc!r}")

    for round_number in range(ROUNDS):
        round_barrier = threading.Barrier(2)
        threads = [
            threading.Thread(target=promotion_arm, args=(round_barrier,), daemon=True),
            threading.Thread(
                target=resume_arm, args=(round_barrier, round_number), daemon=True
            ),
        ]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=DEADLOCK_BUDGET_SECONDS)
        elapsed = time.monotonic() - started
        alive = [thread for thread in threads if thread.is_alive()]
        assert not alive, f"round {round_number} deadlocked: {alive}"
        # The bound is per round: a pair that keeps both waits alive never
        # returns from the joins, while an ordinary scheduling delay between
        # rounds costs nothing that would look like a deadlock.
        assert elapsed < DEADLOCK_BUDGET_SECONDS, (round_number, elapsed)

    assert not stuck, stuck
    assert "resumed" in outcomes and "promoted" in outcomes, outcomes
