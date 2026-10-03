"""A lane event groups by the cause, not by the run that reported it."""

from __future__ import annotations

import pytest

from reckon.crew import query, recovery
from reckon.crew.runs import list_live
from tests.test_simultaneous_ends_are_one_lane_event import _terminal


@pytest.mark.parametrize(
    ("first_text", "second_text"),
    [
        (
            "connection refused: run r-20260914T065219000000-alpha stopped",
            "connection refused: run r-20260914T065223000000-bravo stopped",
        ),
        (
            "connection refused: worker pid 4242 stopped",
            "connection refused: worker pid 991 stopped",
        ),
        (
            "connection refused at 2026-09-14T06:52:19Z",
            "connection refused at 2026-09-14T06:52:23Z",
        ),
    ],
    ids=("run-id", "pid", "timestamp"),
)
def test_two_ends_differing_only_in_a_per_run_token_are_one_event(
    tmp_path, monkeypatch, first_text, second_text
):
    """A token the run's own record contributes never splits a shared cause.

    Each pair records one cause whose message names a run-specific token — the
    run's own id, a process id, a timestamp — and differs in nothing else. The
    two ends land four seconds apart on one backend, so they are the same lane
    event and must render as one row whose fleet verdict carries the grouped
    state.
    """
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    _terminal(
        tmp_path, "r-first", "shared", "2026-09-14T06:52:19Z", result_text=first_text
    )
    _terminal(
        tmp_path, "r-second", "shared", "2026-09-14T06:52:23Z", result_text=second_text
    )

    rows = query.project_live_rows(list_live(project="fixture-project"))
    assert [(row["run_id"], row["classification"]) for row in rows] == [
        ("r-first", "lane-event")
    ]
    event = rows[0]
    assert set(event["lane_event"]["run_ids"]) == {"r-first", "r-second"}
    assert event["lane_event"]["cause"] == "transport-outage"
    assert event["fleet_verdict"]["state"] == "lane-event"
    assert event["fleet_verdict"]["recovery_classification"] == "lane-event"

    recovered = recovery.recover(project="fixture-project")
    assert recovered["counts"]["lane-event"] == 1


def test_ends_with_different_causes_stay_apart(tmp_path, monkeypatch):
    """Two causes ending together are not one event, however close their ends.

    The first pair shares a kind and differs in what the message says, so a
    signature taken over anything looser than the cause itself — the kind
    alone, or a stripped message — would fuse them. The second pair differs in
    kind, which no text normalization may bridge. Each pair ends four seconds
    apart on one backend.
    """
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    _terminal(
        tmp_path,
        "r-refused",
        "shared",
        "2026-09-14T06:52:19Z",
        result_text="connection refused while contacting backend",
    )
    _terminal(
        tmp_path,
        "r-reset",
        "shared",
        "2026-09-14T06:52:23Z",
        result_text="transport reset while contacting backend",
    )
    rows = query.project_live_rows(list_live(project="fixture-project"))
    assert {row["run_id"] for row in rows} == {"r-refused", "r-reset"}
    assert all(row["classification"] != "lane-event" for row in rows)

    other = tmp_path / "second-lane"
    other.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(other))
    _terminal(
        other,
        "r-limited",
        "shared",
        "2026-09-14T06:52:19Z",
        result_text="rate limit reached for this account",
    )
    _terminal(
        other,
        "r-refused",
        "shared",
        "2026-09-14T06:52:23Z",
        result_text="connection refused while contacting backend",
    )
    rows = query.project_live_rows(list_live(project="fixture-project"))
    assert {row["run_id"] for row in rows} == {"r-limited", "r-refused"}
    assert all(row["classification"] != "lane-event" for row in rows)
