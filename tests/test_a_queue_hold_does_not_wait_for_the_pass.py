"""A queue hold applies while an admission pass is running, and the pass sees it.

A pass holds the admission lock for its whole length. A steering verb that took
the same lock could not be used while a pass ran, which is exactly when a
coordinator needs to shrink the queue the pass is working through. So a hold and
a release write one request through that request's own pointer lock, and the
launch re-reads the request's phase before it starts the run.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import pytest

from reckon.crew import queue_admission, queue_edit, runs

#: How long the steering verbs are given to return while a pass holds the
#: admission lock. The defect they are measured against waited the lock's whole
#: lifetime, which is minutes on a live queue.
RETURN_BUDGET_SECONDS = 1.0


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(root))
    (root / "crew" / "live").mkdir(parents=True)
    (root / "crew" / "locks").mkdir(parents=True)
    return root


def _request(run_id: str, *, phase: str = "queued", plan: str = "alpha") -> dict:
    record = {
        "run_id": run_id,
        "project": "fixture",
        "session": "owner",
        "backend": "local",
        "phase": phase,
        "queued_at": datetime.now(UTC).isoformat(),
        "node": {"id": run_id, "plan": plan, "write_paths": []},
    }
    runs._write_json(runs.pointer_path(run_id), record)
    return record


class _PassHoldingTheAdmissionLock:
    """Hold the admission lock in another thread, as a running pass does."""

    def __enter__(self) -> Self:
        self._entered = threading.Event()
        self._release = threading.Event()
        self._thread = threading.Thread(target=self._hold, daemon=True)
        self._thread.start()
        assert self._entered.wait(5.0), "the admission lock was never taken"
        return self

    def _hold(self) -> None:
        with queue_admission._admit_lock():
            self._entered.set()
            self._release.wait(30.0)

    def __exit__(self, *_exc: object) -> None:
        self._release.set()
        self._thread.join(5.0)


def _call_within(
    seconds: float, call: Callable[[], Any]
) -> tuple[bool, dict[str, Any]]:
    """Run ``call`` in a thread and report whether it was still running."""
    outcome: dict[str, Any] = {}

    def target() -> None:
        started = time.monotonic()
        try:
            outcome["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - the test reports it
            outcome["error"] = exc
        outcome["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    return thread.is_alive(), outcome


def _open_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let one pass reach the launch stage with no gate, lane or back-off hold."""
    monkeypatch.setattr(
        queue_admission, "_dispatch_fleet_gate", lambda: {"state": "open"}
    )
    monkeypatch.setattr(
        queue_admission, "_session_allowance_read", lambda *a, **k: (1, "fixture")
    )
    monkeypatch.setattr(queue_admission, "_lane_gate_hold", lambda *a, **k: None)
    monkeypatch.setattr(queue_admission, "_retry_backoff_hold", lambda *a, **k: None)
    monkeypatch.setattr(
        queue_admission, "_require_launched_record", lambda value: value
    )


def test_hold_applies_while_a_pass_holds_the_admission_lock(home: Path) -> None:
    _request("r-held")

    with _PassHoldingTheAdmissionLock():
        blocked, outcome = _call_within(
            RETURN_BUDGET_SECONDS,
            lambda: queue_edit.hold("r-held", reason="shrink the pass"),
        )
        assert not blocked, (
            "queue_edit.hold waited on the admission lock a running pass holds "
            f"({RETURN_BUDGET_SECONDS}s budget)"
        )
        assert "error" not in outcome, outcome.get("error")
        assert runs.read_pointer("r-held")["phase"] == queue_admission.HELD_PHASE


@pytest.mark.parametrize("prior_phase", ["queued", "awaiting-upstream"])
def test_release_restores_the_prior_phase_under_the_same_held_lock(
    home: Path,
    prior_phase: str,
) -> None:
    _request("r-waiting", phase=prior_phase)

    with _PassHoldingTheAdmissionLock():
        blocked, outcome = _call_within(
            RETURN_BUDGET_SECONDS, lambda: queue_edit.hold("r-waiting")
        )
        assert not blocked, "queue_edit.hold waited on the admission lock"
        assert "error" not in outcome, outcome.get("error")
        assert runs.read_pointer("r-waiting")["phase"] == "held"
        blocked, outcome = _call_within(
            RETURN_BUDGET_SECONDS, lambda: queue_edit.release("r-waiting")
        )
        assert not blocked, "queue_edit.release waited on the admission lock"
        assert "error" not in outcome, outcome.get("error")
        assert runs.read_pointer("r-waiting")["phase"] == prior_phase


@pytest.mark.parametrize("held_before_launch", [False, True], ids=["unheld", "held"])
def test_pass_does_not_launch_a_request_held_after_it_read_the_queue(
    home: Path, monkeypatch: pytest.MonkeyPatch, held_before_launch: bool
) -> None:
    _request("r-raced")
    _open_pass(monkeypatch)
    launched: list[str] = []
    decided: list[str] = []

    def launch(pointer: Mapping[str, Any], **_kwargs: Any) -> dict[str, Any]:
        launched.append(str(pointer["run_id"]))
        runs._mutate_pointer(
            str(pointer["run_id"]), lambda current: {**current, "phase": "running"}
        )
        return {"run_id": pointer["run_id"], "phase": "running"}

    monkeypatch.setattr(queue_admission, "_launch_queued", launch)

    def coordinator_holds_while_the_pass_decides(
        pointer: Mapping[str, Any], *, config_for: Any = None
    ) -> None:
        # The pass has already read this request as queued. The hold lands now,
        # before the launch, exactly as a coordinator's hold does against a pass
        # that is busy with the rest of the queue.
        assert pointer["phase"] == "queued"
        decided.append(str(pointer["run_id"]))
        if held_before_launch:
            queue_edit.hold(str(pointer["run_id"]), reason="shrink the pass")

    monkeypatch.setattr(
        queue_admission, "_plan_review_owed", coordinator_holds_while_the_pass_decides
    )

    result = queue_admission.admit_once(lock=False)

    assert decided == ["r-raced"], "the request never reached the launch decisions"
    if not held_before_launch:
        assert launched == ["r-raced"]
        assert result["launched"] == ["r-raced"]
        assert runs.read_pointer("r-raced")["phase"] == "running"
        return
    assert launched == [], "the pass launched a request held after it read the queue"
    assert runs.read_pointer("r-raced")["phase"] == queue_admission.HELD_PHASE
    rows = [row for row in result["verdicts"] if row["run_id"] == "r-raced"]
    assert rows, result["verdicts"]
    assert rows[0]["verdict"] == "refused-before-launch"
    assert "held" in rows[0]["detail"]
    assert "r-raced" not in result["launched"]
