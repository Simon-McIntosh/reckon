"""One fleet-state derivation lists the live directory once and decodes each pointer
file once, and derives the same obligations a per-reader listing derives.

A derivation's own rows and every duty kind it reaches read live pointers, so
outside a published snapshot each reader lists and decodes the directory for
itself. The fleet here is synthesised under a temporary crew home — three
sessions, 300 live pointers, delivered implement runs still owing their
reviews, stored reviews of some of them, held requests and unreconciled runs —
so no reader reaches the operator's own crew home.
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
    open_run_index,
    recovery_classification,
    recovery_watch,
    review,
    runs,
)
from tests.helpers.git import git as _git

PROJECT = "fleet-state-fixture"
SESSIONS = ("session-alpha", "session-beta", "session-gamma")
POINTERS = 300
OBSERVED_AT = datetime(2026, 10, 2, 12, 10, tzinfo=UTC)


def _seed_repository(root: Path) -> str:
    """A checkout with one commit, which every fixture run's head names."""
    docs = root / "docs"
    (docs / "state" / PROJECT).mkdir(parents=True)
    (docs / "plans").mkdir()
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the fleet-state fixture"),
    ):
        _git(root, *arguments)
    return _git(root, "rev-parse", "HEAD")


def _fleet_entry(
    index: int, *, repo: Path, head: str, manifest: Path
) -> tuple[dict[str, Any], str]:
    """One pointer of the fixture fleet, with the status its manifest records.

    The population's positions name the kinds the derivation must answer: a
    delivered implement run, a review of one such run, a held request and an
    unreconciled run, repeating across the previous three sessions.
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
    kind = index % 10
    if kind < 5:
        # Delivered and reconciled, owing its review until one is stored.
        pointer["commits"] = [head]
        pointer["closure_disposition"] = {"kind": "handed-off"}
        return pointer, "complete"
    if kind < 7:
        # A review of the delivered run five positions back, still working.
        pointer["role"] = "review"
        pointer["phase"] = "working"
        pointer["reviewed_run_id"] = f"run-{index - 5:05d}"
        pointer["reviewed_head_sha"] = head
        return pointer, "in-progress"
    if kind < 9:
        # A request its owner holds, past the age the hold is reported at.
        pointer["phase"] = "held"
        pointer["queue_hold"] = {
            "started_at": (OBSERVED_AT - timedelta(hours=30 + kind)).isoformat(),
            "reason": "fixture hold",
        }
        return pointer, "in-progress"
    # Delivered but never reconciled: no disposition excuses the pointer.
    pointer["commits"] = [head]
    return pointer, "complete"


def _store_review(store: Path, run_id: str, head: str) -> None:
    """Store one parsed review of a delivered run at its own head."""
    document = {
        "project": PROJECT,
        "reviewed_run_id": run_id,
        "review_run_id": f"review-of-{run_id}",
        "reviewed_head_sha": head,
        "status": "parsed",
        "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 18),
        "absent": [],
        "total": 18 * len(review.REVIEW_DIMENSIONS),
    }
    (store / f"{run_id}.json").write_text(json.dumps(document), encoding="utf-8")


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


def _build_fleet(home: Path, repo: Path) -> dict[str, Path]:
    """Write the fixture fleet under one synthesised crew home."""
    head = _seed_repository(repo)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    (repo / "docs" / "state" / PROJECT / "runs").mkdir(exist_ok=True)
    manifests = home / "manifests"
    store = review.review_store_root() / PROJECT
    store.mkdir(parents=True)
    stamp = (OBSERVED_AT - timedelta(hours=2)).timestamp()
    for index in range(POINTERS):
        run_id = f"run-{index:05d}"
        manifest = manifests / f"{run_id}.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        pointer, status = _fleet_entry(index, repo=repo, head=head, manifest=manifest)
        manifest.write_text(f"status: {status}\ncommits: [{head}]\n", encoding="utf-8")
        os.utime(manifest, (stamp, stamp))
        if index % 10 in (2, 3):
            _store_review(store, run_id, head)
        runs._write_json(runs.pointer_path(run_id), pointer)
    return {"home": home, "repo": repo, "store": store}


class _CallCounter:
    """Count calls to one function, without changing what it returns."""

    def __init__(self, module: Any, name: str) -> None:
        self._module = module
        self._name = name
        self.calls = 0

    def __enter__(self) -> Self:
        self._real = getattr(self._module, self._name)
        counter = self

        def counting(*args: Any, **kwargs: Any) -> Any:
            counter.calls += 1
            return counter._real(*args, **kwargs)

        setattr(self._module, self._name, counting)
        return self

    def __exit__(self, *exc: object) -> None:
        setattr(self._module, self._name, self._real)


class _DecodeCounter:
    """Count the pointer documents decoded through ``runs.json.loads``."""

    def __init__(self) -> None:
        self.count = 0

    def __enter__(self) -> Self:
        self._real = runs.json
        counter = self

        class _CountingJSON:
            def __getattr__(self, name: str) -> Any:
                return getattr(counter._real, name)

            def loads(self, *args: Any, **kwargs: Any) -> Any:
                value = counter._real.loads(*args, **kwargs)
                if isinstance(value, dict) and str(value.get("run_id") or ""):
                    counter.count += 1
                return value

        runs.json = _CountingJSON()
        return self

    def __exit__(self, *exc: object) -> None:
        runs.json = self._real


@contextmanager
def _base_reads(
    project: str | None = None, *, pointers: Any = None
) -> Iterator[list[dict[str, Any]]]:
    """The base revision's read pattern: every reader lists for itself.

    A slice now offers its own population to this publication; the base
    revision published nothing, so the offer is dropped and each reader lists
    the directory for itself, exactly as before.
    """
    yield runs.list_live(project=project)


def test_one_derivation_lists_the_live_directory_once(fleet: dict[str, Path]) -> None:
    """One derivation lists the project's live directory once."""
    # The derivation runs inside the open-run read scope its production caller
    # already holds, so the window measures the derivation's own reads rather
    # than a caller's one population read.
    with (
        open_run_index.read_scope(),
        _CallCounter(runs, "_list_live_records") as listings,
        _DecodeCounter() as decodes,
    ):
        state = obligation_snapshot.fleet_state(PROJECT, now=OBSERVED_AT)
    # The counter must have seen the whole population: a listing that returned
    # nothing would satisfy a count of one without reading what it measures.
    assert len(state.pointers) == POINTERS
    assert listings.calls == 1, (
        f"one derivation listed the directory {listings.calls} times and"
        f" decoded {decodes.count} pointers"
    )
    assert decodes.count <= POINTERS, (
        f"one derivation decoded {decodes.count} pointers for a fleet of {POINTERS}"
    )
    payloads = {
        session: obligation_snapshot.payload_for(state, session) for session in SESSIONS
    }
    # The fleet holds duties for every kind the snapshot has to answer for, so
    # the single listing is not the listing of an empty derivation.
    kinds = {
        item["kind"] for payload in payloads.values() for item in payload["obligations"]
    }
    assert kinds, "the derivation answered with no duties, so its count proves nothing"
    assert any(payload["summary"]["unreconciled_runs"] for payload in payloads.values())
    assert len(state.rows) == POINTERS


def test_the_snapshot_derives_the_per_reader_obligations(
    fleet: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The derivation's obligations equal those a per-reader read derives."""
    with open_run_index.read_scope():
        shared = obligation_snapshot.fleet_state(PROJECT, now=OBSERVED_AT)
    with (
        open_run_index.read_scope(),
        _CallCounter(runs, "_list_live_records") as listings,
    ):
        monkeypatch.setattr(obligations, "live_pointer_snapshot", _base_reads)
        monkeypatch.setattr(
            obligations,
            "_live_pointers",
            lambda project: runs.list_live(project=project),
        )
        per_reader = obligation_snapshot.fleet_state(PROJECT, now=OBSERVED_AT)
    # The two arms must differ in the read pattern they take, or the equality
    # below compares one path with itself.
    assert listings.calls > 1, (
        "the per-reader arm listed the directory once, so it did not reproduce"
        " the base revision's read pattern"
    )
    assert shared.pointers == per_reader.pointers
    assert shared.unreconciled == per_reader.unreconciled
    for session in SESSIONS:
        assert obligation_snapshot.payload_for(
            shared, session
        ) == obligation_snapshot.payload_for(per_reader, session)
