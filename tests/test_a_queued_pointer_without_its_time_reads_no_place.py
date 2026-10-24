"""A queued pointer without a readable arrival time reads no place.

A queued request's place uses its arrival time even when its roadmap priority
cannot be resolved: the ordering needs that time and refuses a pointer with none. That
refusal belongs to the pointer, not to the read. One malformed pointer must not
cost every other queued request its place, and it must not take down a whole
live read, because the pointer can arrive without the field from anything that
writes one.

So a pointer whose arrival time cannot be read gets no place, while its run is
still listed as queued. The ordering itself is untouched — it still refuses
such a pointer — and one case here holds that too, so the narrowing stays in
the reader rather than moving into the ordering the admission pass ranks by.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

PROJECT = "queued-time"
SESSION = "coordinator-a"
LANE = "clive"

PLACED_OLDER = "r-queued-older"
PLACED_NEWER = "r-queued-newer"
UNREADABLE = "r-queued-unreadable"


def _pointer(tmp_path: Path, run_id: str, *, queued_at: datetime | None) -> dict:
    """One queued pointer, as a live record carries it, with a real stream file."""
    stream = tmp_path / "stream.jsonl"
    if not stream.exists():
        stream.write_text("")
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "backend": LANE,
        "launch": "cli",
        "local": True,
        "phase": "queued",
        "created_at": datetime.now(tz=UTC).isoformat(),
        "log_path": str(stream),
        "manifest_path": str(tmp_path / f"{run_id}-manifest.md"),
        "worktree": str(tmp_path / f"{run_id}-worktree"),
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "200s"},
    }
    if queued_at is not None:
        record["queued_at"] = queued_at.isoformat()
    return record


@pytest.fixture()
def queued_crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home holding two placeable queued pointers and one not."""
    from reckon import crew

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    now = datetime.now(tz=UTC)
    for run_id, queued_at in (
        (PLACED_OLDER, now - timedelta(minutes=3)),
        (PLACED_NEWER, now - timedelta(minutes=1)),
        (UNREADABLE, None),
    ):
        crew._write_json(
            crew.pointer_path(run_id),
            _pointer(tmp_path, run_id, queued_at=queued_at),
        )
    return tmp_path


def _snapshot() -> list[dict]:
    from reckon.crew import runs

    return list(runs._list_live_records(project=PROJECT))


def _live_rows(repository: Path) -> dict[str, dict]:
    from reckon.cli import main as cli_main

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "view",
            "live",
            "--project",
            PROJECT,
            "--checkout-path",
            str(repository),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout or result.output)
    return {row["run_id"]: row for row in payload["runs"]}


def test_queued_placements_places_only_pointers_with_a_readable_time(
    queued_crew_home: Path,
) -> None:
    from reckon.crew.queue_admission import queued_placements

    snapshot = _snapshot()
    # The instrument check: the fixture's own malformed pointer must be present
    # in the snapshot the placement reads, or an absent place would prove
    # nothing about a pointer that was never there.
    held = {str(pointer.get("run_id") or ""): pointer for pointer in snapshot}
    assert set(held) == {PLACED_OLDER, PLACED_NEWER, UNREADABLE}, sorted(held)
    assert held[UNREADABLE].get("queued_at") is None

    placements = queued_placements(snapshot)

    assert placements == {
        PLACED_OLDER: {
            "position": 1,
            "rank": 1,
            "effective_rank": 1,
            "reason": "queued at (roadmap priority unavailable)",
        },
        PLACED_NEWER: {
            "position": 2,
            "rank": 2,
            "effective_rank": 2,
            "reason": "queued at (roadmap priority unavailable)",
        },
    }
    assert UNREADABLE not in placements


def test_the_live_view_lists_the_unplaceable_run_as_queued(
    queued_crew_home: Path,
) -> None:
    rows = _live_rows(queued_crew_home)

    assert set(rows) == {PLACED_OLDER, PLACED_NEWER, UNREADABLE}, sorted(rows)
    assert {row["classification"] for row in rows.values()} == {"queued"}
    assert (rows[PLACED_OLDER]["position"], rows[PLACED_OLDER]["rank"]) == (1, 1)
    assert (rows[PLACED_NEWER]["position"], rows[PLACED_NEWER]["rank"]) == (2, 2)
    assert (
        rows[PLACED_OLDER]["effective_rank"],
        rows[PLACED_NEWER]["effective_rank"],
    ) == (1, 2)
    assert rows[PLACED_OLDER]["reason"] == "queued at (roadmap priority unavailable)"
    assert rows[UNREADABLE].get("position") is None
    assert rows[UNREADABLE].get("rank") is None
    assert rows[UNREADABLE].get("effective_rank") is None
    assert rows[UNREADABLE].get("reason") is None


def test_the_admission_ordering_still_refuses_an_unreadable_time() -> None:
    """The narrowing lives in the reader: the pass's own ordering is unchanged."""
    from reckon.crew.queue_order import admission_order

    now = datetime.now(tz=UTC)
    queued = [
        {
            "run_id": PLACED_OLDER,
            "session": SESSION,
            "backend": LANE,
            "queued_at": (now - timedelta(minutes=3)).isoformat(),
        },
        {"run_id": UNREADABLE, "session": SESSION, "backend": LANE},
    ]
    with pytest.raises(ValueError, match="no readable queued_at"):
        admission_order(queued, [], now=now)
