"""A run that owes its coordinator an action keeps its session's pane.

A follower settles when the work its session carries has drained. The duties
that work raises are read and acted on by the follower itself, so a run that has
ended while a promotion, a refused landing or an owed repair is still
outstanding is not drained at all: settling over it leaves the duty with no
reader. The rule that decided this read only the review a delivered run owes,
which a completed review run, a promotable run and a refused landing do not owe,
so those sessions' panes left exactly where their coordinator's next move lived.

These cases drive ``live_work_populations`` -- the one statement of the
has-live-work rule -- over temporary live pointers with the crew home pointed at
a temporary tree. The keep-live readings are then checked against the
obligations view itself, so the pane and the coordinator's inbox cannot disagree
about which run is still owed. A final case drives ``SessionHost.tick`` with a
stubbed spawn and shows a settled child brought back for the unpromoted review.

The control restores the review-only rule: every keep-live case except the
delivered run owing its review then reads as no work.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import (
    obligations,
    recovery,
    review,
    runs,
    session_host,
    station_subscriber,
)

PROJECT = "keep-the-pane"
SESSION = "s-keep-the-pane"
HEAD = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
SETTLED_STATUS = session_host.SETTLED_EXIT_CODE << 8


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every crew path at a temporary home, so no case reads a live fleet."""
    config_home = tmp_path / "crew-home"
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setattr(station_subscriber, "_WARNED_DUTY_READ_FAILURES", set())
    yield config_home
    live = Path(os.path.expanduser("~")) / ".config" / "reckon" / "crew" / "live"
    assert not (live / "r-unpromoted.json").exists(), (
        "this case's pointer reached the real live directory"
    )


def _pointer(
    home: Path,
    run_id: str,
    *,
    phase: str = "complete",
    manifest_status: str = "complete",
    extra: dict | None = None,
    body: str = "",
) -> dict:
    """One live pointer for a run that has delivered and whose worker is gone."""
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: {manifest_status}\ncommits: [{HEAD}]\n{body}",
        encoding="utf-8",
    )
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "role": "implement",
        "node": {"id": f"node-{run_id}", "plan": "a-plan", "time_budget": "20m"},
        "phase": phase,
        "created_at": runs._utc_now(),
        "manifest_path": str(manifest),
        "log_path": str(log),
        "worktree": str(home / "reclaimed" / run_id),
        "process_alive": False,
    }
    if extra:
        pointer.update(extra)
    return pointer


def _populations(pointers: list[dict]) -> tuple[bool, bool, list[str]]:
    return station_subscriber.live_work_populations(pointers)


def test_a_completed_plan_review_run_keeps_its_pane_until_promoted(home: Path) -> None:
    """A delivered run whose review is not stored keeps its session owed."""
    pointer = _pointer(home, "r-unpromoted")
    pointer["role"] = "plan-review"
    assert _populations([pointer]) == (True, False, [SESSION])

    # A plan review is its own deliverable: once it has been promoted its live
    # pointer is released, so the population the pane is judged by no longer
    # carries the run at all.
    assert _populations([]) == (False, False, [])


def test_a_promotable_run_with_its_review_stored_keeps_its_pane(home: Path) -> None:
    """A promotable run owes its coordinator a promotion, so the pane stays."""
    run_id = "r-promotable"
    review.store_review(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "status": "parsed",
            "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 18),
            "absent": [],
        }
    )
    pointer = _pointer(home, run_id)
    assert _populations([pointer]) == (True, False, [SESSION])
    assert _populations([]) == (False, False, [])


def test_a_run_whose_landing_was_refused_keeps_its_pane(home: Path) -> None:
    """The refused landing is the coordinator's next move, so the pane stays."""
    run_id = "r-refused"
    pointer = _pointer(
        home,
        run_id,
        extra={
            "admission_landing": {
                "status": "refused",
                "head": HEAD,
                "guard": "evidence-missing",
                "remedy": "record the gate log",
            }
        },
    )
    items = station_subscriber.terminal_run_duty_items(
        pointer,
        now=obligations._utc_now(),
        grace=900,
        live_pointers=[pointer],
    )
    assert [item["kind"] for item in items] == ["landing-refused"]
    assert _populations([pointer]) == (True, False, [SESSION])


def test_a_run_owing_a_repair_keeps_its_pane(home: Path) -> None:
    """A stored review with an undisposed blocking finding owes a repair."""
    run_id = "r-repair"
    review.store_review(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "status": "parsed",
            "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 18),
            "absent": [],
            "findings": [
                {
                    "dimension": "correctness",
                    "blocking": True,
                    "summary": "the guard never fires",
                }
            ],
        }
    )
    pointer = _pointer(home, run_id)
    items = station_subscriber.terminal_run_duty_items(
        pointer,
        now=obligations._utc_now(),
        grace=900,
        live_pointers=[pointer],
    )
    assert [item["kind"] for item in items] == ["repair-owed"]
    assert _populations([pointer]) == (True, False, [SESSION])


def test_a_run_that_ended_needs_help_keeps_its_pane(home: Path) -> None:
    """A worker that asked for help is the coordinator's to answer."""
    pointer = _pointer(
        home,
        "r-needs-help",
        phase="complete",
        manifest_status="blocked",
        body=(
            "NEEDS-HELP: the guard has no test\n"
            "tried: drove the guard over a temporary tree\n"
            "options: add the test, or drop the guard\n"
            "leaning: add the test\n"
            "cost-if-wrong: one dispatch is spent on a decision the plan settles\n"
        ),
    )
    row = recovery.classify_pointer(pointer)
    assert row.get("recovery_classification") == "needs-help"
    assert _populations([pointer]) == (True, False, [SESSION])


def test_a_completed_run_owing_nothing_lets_the_pane_settle(home: Path) -> None:
    """A run with no duty of its own is drained, and the session is not owed."""
    pointer = _pointer(home, "r-drained", phase="failed", manifest_status="failed")
    assert _populations([pointer]) == (False, False, [])


def _view_item_run_ids(pointers: list[dict]) -> set[str]:
    """The runs the obligations view itself lists as owing this session an action."""
    token = obligations._VIEW_LIVE_POINTERS.set({PROJECT: tuple(pointers)})
    try:
        view = obligations._obligations_view(PROJECT, SESSION)
    finally:
        obligations._VIEW_LIVE_POINTERS.reset(token)
    rows = view.get("obligations") if isinstance(view, dict) else None
    assert rows is not None, f"the view answered no duty list: {view!r}"
    return {str(item.get("run_id") or "") for item in rows if item.get("run_id")}


def _delivered_with_a_review_that_ended(home: Path) -> dict:
    """A delivered scoring run whose dispatched review ended without a record."""
    review_run = "r-review-ended"
    crew._write_json(
        crew.pointer_path(review_run),
        _pointer(home, review_run, phase="complete", extra={"role": "review"}),
    )
    return _pointer(
        home,
        "r-review-in-flight",
        extra={"review_dispatch": {"run_id": review_run, "at": runs._utc_now()}},
    )


def test_the_pane_and_the_obligations_view_name_the_same_owing_runs(
    home: Path,
) -> None:
    """One derivation: the runs that keep the pane owe exactly what the view lists."""
    pointers = [
        _pointer(home, "r-unpromoted"),
        _delivered_with_a_review_that_ended(home),
    ]
    assert _populations(pointers) == (True, False, [SESSION])
    # The second run's review ended without storing a record, so the view lists
    # no duty for it and the pane must not hold its session for it either.
    assert _view_item_run_ids(pointers) == {"r-unpromoted"}
    assert _populations([pointers[1]]) == (False, False, [])
    assert _view_item_run_ids([pointers[1]]) == set()


class StubHost(session_host.SessionHost):
    """A host whose children are pids this case hands out and ends on demand."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.spawned: list[int] = []
        self.exits: dict[int, int | None] = {}
        self._next_pid = 9_000

    def _spawn(self, child: session_host._Child) -> int | None:
        self._next_pid += 1
        self.spawned.append(self._next_pid)
        return self._next_pid

    def _poll_exit(self, child: session_host._Child) -> int | None:
        return self.exits.pop(child.pid, None)

    def settle(self, pid: int) -> None:
        self.exits[pid] = SETTLED_STATUS

    def child(self) -> session_host._Child:
        return self._children[(PROJECT, SESSION)]


def test_a_settled_child_is_rearmed_for_the_unpromoted_review(
    home: Path, tmp_path: Path
) -> None:
    """A run that delivered while the child was settling brings the pane back."""
    view = session_host.live_work_run
    host = StubHost(
        owner={"pid": os.getpid(), "start_time": "a-state-time-for-the-stub"},
        follower_argv=["a-stub-follower"],
        environ={
            session_host.STATE_DIR_ENV: str(tmp_path / "state"),
            session_host.LOG_DIR_ENV: str(tmp_path / "log"),
            session_host.POLL_ENV: "0.05",
            session_host.BACKOFF_ENV: "0.0",
        },
        live_view=view,
    )
    host.handle(
        json.dumps({"project": PROJECT, "session": SESSION, "owner_checked": True})
    )
    assert len(host.spawned) == 1, "the request must start one child"
    host.tick()
    first = host.child().pid
    assert first is not None, "the host armed no child for the requested pair"

    host.settle(first)
    host.tick()
    assert host.child().settled, "the child did not read as settled"
    assert len(host.spawned) == 1, "a settled child was restarted"

    # The run delivered while the child was settling: its review is composed by
    # the follower that just left, so the host must bring the pane back.
    pointer = _pointer(home, "r-unpromoted")
    crew._write_json(crew.pointer_path(pointer["run_id"]), pointer)
    host._live_view = session_host.live_work_run
    host.tick()
    assert not host.child().settled, "the host stayed settled over an owed review"
    assert host.child().rearmed_for == "r-unpromoted"
