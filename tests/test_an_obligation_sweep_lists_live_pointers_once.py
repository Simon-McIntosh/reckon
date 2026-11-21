"""One obligation-snapshot sweep lists the live directory a bounded number of times.

Every review duty in a session's payload reads the live pointers: the duty's own
read, and the review queue it consults, so outside a published snapshot a sweep
pays one listing per review duty — a directory glob, a stat of every pointer and
a copy of every record. On the fleet here, 200 live pointers with 60 delivered
runs owing a review across three sessions, the base read pattern takes 122
listings and copies 24,400 records for one sweep.

The sweep now publishes the listing its derivation already holds for the length
of each slice, so the same sweep lists the directory a bounded number of times
and the obligations it publishes are the ones the base read pattern publishes.
The fleet is synthesised under a temporary crew home, so no reader reaches the
operator's own crew home.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

import pytest

from reckon.crew import (
    obligation_snapshot,
    obligations,
    recovery_classification,
    recovery_watch,
    runs,
)
from tests.helpers.git import git as _git

PROJECT = "sweep-listing-fixture"
SESSIONS = ("session-alpha", "session-beta", "session-gamma")
POINTERS = 200
OWING_REVIEW = 60
OBSERVED_AT = datetime(2026, 10, 2, 12, 10, tzinfo=UTC)
#: One sweep's reads are bounded by the derivation, not by the duty count: the
#: derivation lists once, and a slice answers from the population it holds.
MAX_LISTINGS = 3
MAX_RECORDS = 600
PRODUCER = {
    "pid": 1,
    "pid_start_time": "1",
    "started_at": None,
    "code_stamp": "sweep-listing-fixture",
}


def _seed_repository(root: Path) -> str:
    """A checkout with one commit, which every fixture run's head names."""
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "plans").mkdir()
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the sweep-listing fixture"),
    ):
        _git(root, *arguments)
    return _git(root, "rev-parse", "HEAD")


def _fleet_entry(
    index: int, *, repo: Path, head: str, manifest: Path
) -> tuple[dict[str, Any], str]:
    """One live pointer of the fixture fleet, with its manifest's status.

    Three of every ten positions are a delivered implement run that owes a
    review of its head; the rest are working review runs, which the sweep lists
    too but which owe a duty of their own kind. The delivered runs spread evenly
    across the three sessions, so each session's payload carries twenty.
    """
    run_id = f"run-{index:05d}"
    pointer: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSIONS[index % len(SESSIONS)],
        "repo": str(repo),
        "worktree": str(repo),
        "base_sha": head,
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": run_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "write_paths": ["seed.txt"],
        },
    }
    if index % 10 < 3:
        # Delivered and reconciled, owing its review until one is stored.
        pointer["commits"] = [head]
        pointer["closure_disposition"] = {"kind": "handed-off"}
        return pointer, "complete"
    # A review of the delivered run at the start of this decade, still working.
    pointer["role"] = "review"
    pointer["phase"] = "working"
    pointer["reviewed_run_id"] = f"run-{index - index % 10:05d}"
    pointer["reviewed_head_sha"] = head
    return pointer, "in-progress"


def _build_fleet(home: Path, repo: Path) -> dict[str, Path]:
    """Write the fixture fleet under one synthesised crew home."""
    head = _seed_repository(repo)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    (repo / "docs" / "state" / PROJECT / "runs").mkdir(exist_ok=True)
    manifests = home / "manifests"
    stamp = (OBSERVED_AT - timedelta(hours=2)).timestamp()
    for index in range(POINTERS):
        run_id = f"run-{index:05d}"
        manifest = manifests / f"{run_id}.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        pointer, status = _fleet_entry(index, repo=repo, head=head, manifest=manifest)
        manifest.write_text(f"status: {status}\ncommits: [{head}]\n", encoding="utf-8")
        os.utime(manifest, (stamp, stamp))
        runs._write_json(runs.pointer_path(run_id), pointer)
    return {"home": home, "repo": repo}


@pytest.fixture()
def fleet(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """A synthesised crew home holding the fixture fleet's live pointers."""
    built = _build_fleet(home, tmp_path / "repo")
    frozen = OBSERVED_AT.timestamp()
    monkeypatch.setattr(obligations, "_utc_now", lambda: OBSERVED_AT)
    monkeypatch.setattr(recovery_watch, "_utc_seconds", lambda: frozen)
    monkeypatch.setattr(recovery_classification, "_utc_seconds", lambda: frozen)
    return built


class _ListingCounter:
    """Count the live-directory listings and the records copied from them.

    The stand-in wraps ``runs.list_live`` and keeps what it returns, so a count
    of zero or a count that saw an empty directory is visible as such rather
    than reading as a bounded sweep.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.records = 0
        self.seen = 0

    def __enter__(self) -> Self:
        self._real = runs.list_live
        counter = self

        def counting(*args: Any, **kwargs: Any) -> Any:
            records = counter._real(*args, **kwargs)
            counter.calls += 1
            counter.records += len(records)
            counter.seen = max(counter.seen, len(records))
            return records

        runs.list_live = counting
        return self

    def __exit__(self, *exc: object) -> None:
        runs.list_live = self._real


@contextmanager
def _base_reads(
    project: str | None = None, *, pointers: Any = None
) -> Iterator[list[dict[str, Any]]]:
    """The base revision's read pattern: no published snapshot, every reader lists."""
    yield runs.list_live(project=project)


def _sweep() -> list[Path]:
    """One obligation-snapshot sweep over the fixture fleet."""
    return obligation_snapshot.sweep(
        PROJECT,
        sessions=list(SESSIONS),
        producer=PRODUCER,
        stream_offset=0,
        transition_fired=True,
        state_dirs=[],
        now=OBSERVED_AT,
    )


def _published() -> dict[str, Any]:
    """Every session's published snapshot, read back from the store."""
    documents: dict[str, Any] = {}
    for session in SESSIONS:
        document = obligation_snapshot.read_snapshot(PROJECT, session)
        assert document is not None, f"{session} published no snapshot"
        documents[session] = document
    return documents


def _review_duties(documents: dict[str, Any]) -> int:
    """How many review duties the sweep published across every session."""
    return sum(
        1
        for document in documents.values()
        for item in document["obligations"]
        if "review" in str(item.get("kind") or "")
    )


def test_one_sweep_lists_the_live_directory_a_bounded_number_of_times(
    fleet: dict[str, Path],
) -> None:
    """One sweep lists the live directory a bounded number of times."""
    with _ListingCounter() as listings:
        written = _sweep()
    assert len(written) == len(SESSIONS), "the sweep did not publish every session"
    documents = _published()
    # The instrument must have seen the population: a listing that returned
    # nothing satisfies a count of three without reading what it measures.
    assert listings.seen == POINTERS, (
        f"the widest listing read {listings.seen} of {POINTERS} live pointers, so"
        " the count below proves nothing"
    )
    # And the sweep must have had the duties that used to cost a listing each.
    assert _review_duties(documents) >= OWING_REVIEW, (
        f"the sweep published {_review_duties(documents)} review duties over"
        f" {OWING_REVIEW} delivered runs, so the bound is not exercised"
    )
    assert listings.calls <= MAX_LISTINGS, (
        f"one sweep listed the live directory {listings.calls} times"
    )
    assert listings.records <= MAX_RECORDS, (
        f"one sweep copied {listings.records} live records for {POINTERS} pointers"
    )


def test_the_published_obligations_equal_the_base_read_pattern(
    fleet: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every session publishes the obligations the base read pattern publishes."""
    shared = _published_after_sweep()
    with (
        monkeypatch.context() as base_arm,
        _ListingCounter() as listings,
    ):
        base_arm.setattr(obligations, "live_pointer_snapshot", _base_reads)
        per_reader = _published_after_sweep()
    # The two arms must take different read patterns, or the equality below
    # compares one path with itself.
    assert listings.calls >= OWING_REVIEW, (
        f"the base-pattern arm listed the live directory {listings.calls} times"
        f" over {OWING_REVIEW} review duties, so it did not reproduce the base"
        " revision's read pattern"
    )
    assert per_reader == shared, (
        "the published obligations differ from the base read pattern's"
    )
    assert _review_duties(shared) >= OWING_REVIEW


def _published_after_sweep() -> dict[str, Any]:
    """Run one sweep and read every session's published snapshot back."""
    _sweep()
    return _published()
