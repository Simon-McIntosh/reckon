"""One station refresh lists each review store once, whatever it derives.

The station's per-project refresh derives every live session's duties, and each
duty reads the review store for its run. Without the per-tick store installed,
every one of those reads enumerates the store's directory again, so the cost of
a refresh grows with the number of duties a project holds rather than with the
number of files that changed.

The case below drives one refresh over more duty items than a store's listing
should ever be paid per item, counting every enumeration of the store's
directory at its two entry points: the directory's own ``glob`` and
``os.scandir``. A review store is listed at most once per refresh.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import obligations, review, station_obligations, station_store
from reckon.crew.run_paths import crew_home

PROJECT = "fixture"
SESSION = "session-one"
HEAD = "0123456789abcdef0123456789abcdef01234567"
DUTY_ITEMS = 24
NOW = datetime(2026, 11, 13, 6, 0, tzinfo=UTC)


def _record_payload(run_id: str) -> dict:
    """One complete stored review of the run's head, as a reviewer files it."""
    return {
        "project": PROJECT,
        "reviewed_run_id": run_id,
        "review_run_id": f"review-of-{run_id}",
        "reviewed_head_sha": HEAD,
        "status": "parsed",
        "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 18),
        "absent": [],
        "total": 18 * len(review.REVIEW_DIMENSIONS),
    }


def _pointer(home: Path, index: int) -> dict:
    """One live pointer whose delivered head owes a review."""
    run_id = f"r-duty-{index:02}"
    run_directory = home / "runs" / run_id
    run_directory.mkdir(parents=True, exist_ok=True)
    stream = run_directory / "stream.jsonl"
    stream.write_text(
        json.dumps({"type": "system", "subtype": "init"}) + "\n", encoding="utf-8"
    )
    manifest = run_directory / "manifest.md"
    manifest.write_text("status: complete\ncommits: []\n", encoding="utf-8")
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": "working",
        "created_at": "2026-11-13T05:00:00Z",
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "worktree": str(home / "trees" / run_id),
        "launcher_host": socket.gethostname(),
        "node": {"id": run_id, "plan": "fixture-plan"},
    }
    path = home / "live" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


def _classified_row(index: int) -> dict:
    """One classified row whose duty is read from the review store."""
    run_id = f"r-duty-{index:02}"
    return {
        "run_id": run_id,
        "session": SESSION,
        "project": PROJECT,
        "classification": "scoring",
        "age_seconds": 30,
        "node": run_id,
        "plan": "fixture-plan",
        "manifest_commits": [HEAD],
        "next_action": "",
    }


def _record_listings(monkeypatch: pytest.MonkeyPatch, directory: Path) -> list[str]:
    """Count every enumeration of ``directory`` through its two entry points.

    A ``glob`` reaches the filesystem through ``os.scandir`` itself, so the
    stand-in records the call before opening the directory and suppresses the
    enumeration it makes underneath, leaving one count per listing rather than
    one per layer the listing happens to use.
    """
    listings: list[str] = []
    real_glob = Path.glob
    real_scandir = os.scandir
    inside_glob = False

    def counted_glob(self: Path, pattern: str, **kwargs):
        nonlocal inside_glob
        if self == directory:
            listings.append(f"glob:{pattern}")
        inside_glob = True
        try:
            return real_glob(self, pattern, **kwargs)
        finally:
            inside_glob = False

    def counted_scandir(path="."):
        if (
            not inside_glob
            and not isinstance(path, int)
            and Path(os.fsdecode(path)) == directory
        ):
            listings.append("scandir")
        return real_scandir(path)

    monkeypatch.setattr(Path, "glob", counted_glob)
    monkeypatch.setattr(os, "scandir", counted_scandir)
    return listings


@pytest.fixture()
def derived(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A crew home holding DUTY_ITEMS duties whose reviews are all stored."""
    home = crew_home()
    directory = review.review_store_root() / PROJECT
    directory.mkdir(parents=True, exist_ok=True)
    pointers = []
    rows = []
    for index in range(DUTY_ITEMS):
        run_id = f"r-duty-{index:02}"
        (directory / f"{run_id}.json").write_text(
            json.dumps(_record_payload(run_id)), encoding="utf-8"
        )
        pointers.append(_pointer(home, index))
        rows.append(_classified_row(index))

    monkeypatch.setattr(obligations, "_classified_rows", lambda project: list(rows))
    monkeypatch.setattr(
        station_obligations,
        "follower_rows",
        lambda project: [{"session": SESSION, "live": True}],
    )
    # The head a duty is keyed on and the review tier are read through their own
    # resolvers, which open the run's worktree and read its plan. Neither is the
    # store's listing, so both are answered here and the store stays under test.
    monkeypatch.setattr(
        obligations.recovery, "_review_head_and_tree", lambda pointer: (HEAD, None)
    )
    monkeypatch.setattr(
        obligations.recovery, "_sweep_review_tier", lambda pointer, commits: "full"
    )
    derived_items: list[str] = []
    real_duty = obligations._review_duty_item

    def counted_duty(project, row, pointer, **kwargs):
        derived_items.append(str(row.get("run_id") or ""))
        return real_duty(project, row, pointer, **kwargs)

    monkeypatch.setattr(obligations, "_review_duty_item", counted_duty)
    store = station_store.StationStore(tmp_path / "station")
    yield store, directory, derived_items
    store.close()


def test_a_refresh_lists_the_review_store_once(derived, monkeypatch) -> None:
    store, directory, derived_items = derived
    listings = _record_listings(monkeypatch, directory)

    station_obligations.refresh(store, project=PROJECT, changed=True, now=NOW)

    assert len(derived_items) >= 20, (
        "the refresh derived fewer review duties than the store it was given: "
        f"{len(derived_items)}"
    )
    assert len(listings) == 1, (
        "the refresh listed the review store once per duty item instead of "
        f"once per tick: {len(listings)} listings {listings[:8]}"
    )
