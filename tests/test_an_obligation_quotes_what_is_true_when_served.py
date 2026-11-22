"""A served duty quotes the phase read when it is served, not when composed.

A duty's sentence is composed once, when the fleet is classified, and a
snapshot carries that sentence for as long as the snapshot is read. The value
a review's phase had at composition is therefore not the value a reader should
see: a review that completed while a slow tick ran kept being listed as
``(phase: working)`` for the length of the tick, and a row naming a run that
had gone kept naming it.

The reader settles a quoted fact against the run's own pointer as it reads it.
The cases here build the duty through the real builder, then serve it twice:
once against a review that has since completed, and once against a review
whose pointer has gone. A third case holds the negative: a pointer that
cannot be parsed is not evidence that the cause has gone, so the row is kept
exactly as the snapshot carried it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import obligations as obligations_module
from reckon.hooks import coordinator_obligations as hook

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"

PROJECT = "quoted-phase-fixture"
SESSION = "quoted-phase-session"
RUN_ID = "run-awaiting-review"
REVIEW_RUN_ID = "run-review-quoted"
NODE_ID = "awaiting-review-node"

_ABSENT_HARNESS_PID = 2_147_483_647


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    return home


def _write_pointer(run_id: str, document: object) -> None:
    path = hook.snapshot_module().live_pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def _review_in_flight_item() -> dict[str, object]:
    """The duty the derivation builds for a run whose review reads as working."""
    row = {
        "run_id": RUN_ID,
        "node": NODE_ID,
        "plan": "fixture-plan",
        "age_seconds": 600,
        "classification": "scoring",
        "review_run_id": REVIEW_RUN_ID,
        "review_run_phase": "working",
        "detail": (
            "the worker manifest reports completion and its review is in flight "
            f"as {REVIEW_RUN_ID} (phase: working); the run is not promotable "
            "until that review files its record"
        ),
        "next_action": f"read it with `crew observe --run {REVIEW_RUN_ID}`",
    }
    item = obligations_module._review_duty_item(
        PROJECT,
        row,
        {"run_id": RUN_ID, "role": "implement"},
        now=datetime.now(tz=UTC),
        grace=600,
    )
    assert item is not None
    return item


def _payload(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "project": PROJECT,
        "session": SESSION,
        "obligations": items,
        "summary": {
            "count": len(items),
            "oldest_age_seconds": max(
                (int(item.get("age_seconds") or 0) for item in items), default=0
            ),
        },
    }


def test_the_duty_carries_the_run_whose_fact_it_quotes() -> None:
    item = _review_in_flight_item()

    assert item["kind"] == obligations_module.REVIEW_IN_FLIGHT_DUTY_KIND
    quoted = item[obligations_module.QUOTED_RUN_FIELD]
    assert quoted["run_id"] == REVIEW_RUN_ID
    assert quoted["phase"] == "working"


def test_a_served_row_quotes_the_phase_read_when_it_is_served(
    config_home: Path,
) -> None:
    """A review that completed is not served as the phase it held at compose."""
    item = _review_in_flight_item()
    _write_pointer(REVIEW_RUN_ID, {"run_id": REVIEW_RUN_ID, "phase": "complete"})
    _write_pointer(RUN_ID, {"run_id": RUN_ID, "phase": "working"})

    served = hook.recheck_worktree_held_rows(_payload([item]), project=PROJECT)
    rows = served["obligations"]
    assert len(rows) == 1
    reason = str(rows[0]["reason"])
    assert "(phase: complete)" in reason
    assert "(phase: working)" not in reason


def test_a_served_row_whose_run_has_gone_is_dropped(config_home: Path) -> None:
    item = _review_in_flight_item()
    _write_pointer(RUN_ID, {"run_id": RUN_ID, "phase": "working"})

    served = hook.recheck_worktree_held_rows(_payload([item]), project=PROJECT)

    assert served["obligations"] == []
    assert served["summary"]["count"] == 0


def test_an_unreadable_pointer_keeps_the_row_as_it_was(config_home: Path) -> None:
    item = _review_in_flight_item()
    path = hook.snapshot_module().live_pointer_path(REVIEW_RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    _write_pointer(RUN_ID, {"run_id": RUN_ID, "phase": "working"})

    served = hook.recheck_worktree_held_rows(_payload([item]), project=PROJECT)

    rows = served["obligations"]
    assert len(rows) == 1
    assert "(phase: working)" in str(rows[0]["reason"])
