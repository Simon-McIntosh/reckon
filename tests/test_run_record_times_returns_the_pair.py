"""The times resolver returns exactly the two stamps its callers read.

``review.run_record_times`` answers one question — what dispatch and completion
stamps does a run's own record carry? — and its callers read the pair and
nothing else. These cases hold it to that pair from each source it resolves:
the committed per-run record beside the ledger, the live pointer that stands in
before promotion, and neither, which is committed as ``unknown``. The pair is
asserted whole, so a resolver that hands back a third value alongside the two
fails here rather than at a caller that never reads it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import review as review_module

PROJECT = "times-pair"
REVIEW_RUN = "r-review-run"

DISPATCH_TS = "2026-10-06T09:00:00+00:00"
COMPLETION_TS = "2026-10-06T09:07:30+00:00"


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised project checkout with the state tree the ledger uses."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    return root


def _run_record(checkout: Path, run_id: str, payload: dict) -> Path:
    """Write one run's committed per-run record beside the ledger."""
    path = checkout / "docs" / "state" / PROJECT / "runs" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _pointer(config: Path, run_id: str, payload: dict) -> Path:
    """Write one run's live pointer under the synthesised config home."""
    path = config / "crew" / "live" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_the_pair_comes_from_the_committed_run_record(checkout: Path) -> None:
    # A promoted run's own committed record carries both stamps, so the pair
    # is the run's, not the store clock's.
    _run_record(
        checkout,
        REVIEW_RUN,
        {
            "run_id": REVIEW_RUN,
            "project": PROJECT,
            "dispatched_at": DISPATCH_TS,
            "completed_at": COMPLETION_TS,
        },
    )
    result = review_module.run_record_times(PROJECT, REVIEW_RUN, root=checkout)
    assert result == (DISPATCH_TS, COMPLETION_TS)


def test_the_pair_comes_from_a_live_pointer_before_promotion(
    checkout: Path, tmp_path: Path
) -> None:
    # A run not yet promoted has no committed per-run record, so its live
    # pointer supplies the dispatch stamp it carries as ``created_at`` and its
    # completion stamp; a missing committed record is not a missing time.
    _pointer(
        tmp_path / "config",
        REVIEW_RUN,
        {
            "run_id": REVIEW_RUN,
            "created_at": DISPATCH_TS,
            "completed_at": COMPLETION_TS,
        },
    )
    result = review_module.run_record_times(PROJECT, REVIEW_RUN, root=checkout)
    assert result == (DISPATCH_TS, COMPLETION_TS)


def test_the_pair_is_empty_when_no_source_carries_a_stamp(
    checkout: Path, tmp_path: Path
) -> None:
    # A live pointer that carries neither stamp yields empty strings rather
    # than a defaulted clock; the committed store then records ``unknown``.
    _pointer(tmp_path / "config", REVIEW_RUN, {"run_id": REVIEW_RUN})
    assert review_module.run_record_times(PROJECT, REVIEW_RUN, root=checkout) == (
        "",
        "",
    )

    # No record and no pointer at all is the same answer, not a crash.
    assert review_module.run_record_times(PROJECT, "r-absent", root=checkout) == (
        "",
        "",
    )


def test_the_resolver_returns_only_the_pair(checkout: Path) -> None:
    # The return shape is the pair itself: a resolver that appends a found
    # flag as a third value no longer unpacks to two stamps and fails here.
    _run_record(
        checkout,
        REVIEW_RUN,
        {
            "run_id": REVIEW_RUN,
            "project": PROJECT,
            "dispatched_at": DISPATCH_TS,
            "completed_at": COMPLETION_TS,
        },
    )
    result = review_module.run_record_times(PROJECT, REVIEW_RUN, root=checkout)
    assert len(result) == 2
    dispatched, completed = result
    assert (dispatched, completed) == (DISPATCH_TS, COMPLETION_TS)
