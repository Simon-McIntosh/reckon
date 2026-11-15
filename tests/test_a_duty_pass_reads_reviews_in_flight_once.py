"""A duty pass asks which runs have a review in flight once, not once per row.

The station evaluates every finished run's duty on every tick, and each duty
asks the reflex's own recognition rule which live reviewer stands for the run's
head. That rule sweeps the live pointers, so a pass over P pointers swept P
pointers P times: once while the pass built the session's in-flight set, and
again for every row that then asked the same question.

The fixture is a synthesised crew home of 30 delivered runs, 10 of them with a
live review working on the delivered head and the other 20 carrying a complete
stored review. The live directory therefore holds 40 pointers. One pass is run
while recording stand-ins count the recognition searches and the walks the
in-flight set is built from. The populations and the duty rows the pass raises
are compared against the base read path, so the sharing can only change how
often the question is asked, never the answer.
"""

from __future__ import annotations

import importlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import queue_admission, runs, station_subscriber
from tests.helpers.git import git as _git

ledger_module = importlib.import_module("reckon.ledger")
obligations_module = importlib.import_module("reckon.crew.obligations")
recovery_module = importlib.import_module("reckon.crew.recovery")
review_module = importlib.import_module("reckon.crew.review")

PROJECT = "review-in-flight-pass"
SESSION = "coordinator-review-in-flight-pass"
DELIVERED_RUNS = 20
IN_FLIGHT_RUNS = 10
# 30 delivered runs, and one live reviewer for each of the 10 that are in flight.
LIVE_POINTERS = DELIVERED_RUNS + IN_FLIGHT_RUNS + IN_FLIGHT_RUNS

# One clock for the whole case, so an aged duty reads the same text in both the
# shared and the base read path.
OBSERVED_AT = datetime(2026, 11, 15, 8, 0, tzinfo=UTC)
DELIVERED_AGE = timedelta(days=3)


def _observed_seconds() -> float:
    return OBSERVED_AT.timestamp()


def _commit(repository: Path, name: str, body: str) -> str:
    (repository / name).parent.mkdir(parents=True, exist_ok=True)
    (repository / name).write_text(body, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", f"test: {body.strip()}")
    return _git(repository, "rev-parse", "HEAD")


def _delivered_run(
    root: Path,
    home: Path,
    index: int,
    *,
    in_flight: bool,
    pointers: list[dict[str, Any]],
) -> None:
    """Write one delivered run, and the live reviewer when it has one."""
    run_id = f"run-{index:02d}"
    reviewed_head = _git(root, "rev-parse", "HEAD")
    worktree = root.parent / f"worktree-{index:02d}"
    _git(root, "worktree", "add", "-q", "--detach", str(worktree), reviewed_head)
    head = _commit(worktree, f"reckon/mod_{index:02d}.py", f"VALUE = {index}\n")
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n"
        f"changed_paths: [reckon/mod_{index:02d}.py]\n",
        encoding="utf-8",
    )
    aged = (OBSERVED_AT - DELIVERED_AGE).timestamp()
    os.utime(manifest, (aged, aged))
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(root),
        "worktree": str(worktree),
        "base_sha": reviewed_head,
        "process_alive": False,
        "phase": "complete",
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "terminal_at": OBSERVED_AT.isoformat(),
        "node": {
            "id": run_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "write_paths": [f"reckon/mod_{index:02d}.py"],
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    pointers.append(pointer)
    if in_flight:
        # A hand-dispatched reviewer carries no reflex record, so it is
        # recognised by the record paths it was granted: the head-keyed name the
        # composer writes for this run's head.
        reviewer = {
            "run_id": f"review-of-{run_id}",
            "project": PROJECT,
            "session": SESSION,
            "phase": "working",
            "role": "review",
            "process_alive": True,
            "node": {
                "id": f"review-of-{run_id}",
                "plan": "fixture-plan",
                "section": "fixture-section",
                "write_paths": review_module.run_review_record_paths(
                    runs.run_dir(run_id), run_id, reviewed_head_sha=head
                ),
            },
        }
        runs._write_json(runs.pointer_path(reviewer["run_id"]), reviewer)
        pointers.append(reviewer)
        return
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"stored-review-of-{run_id}",
            "reviewed_base_sha": reviewed_head,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(record)


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """30 delivered runs, 10 of them with a live review on the delivered head."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the in-flight-passes fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    ledger_dir = ledger_module.ledger_path(PROJECT).parent
    (ledger_dir / "runs").mkdir(parents=True, exist_ok=True)
    ledger_module.ledger_path(PROJECT).write_text(
        json.dumps({"data": {"_version": 1, "members": [], "runs": [], "holds": []}}),
        encoding="utf-8",
    )
    pointers: list[dict[str, Any]] = []
    for index in range(IN_FLIGHT_RUNS):
        _delivered_run(root, config_home, index, in_flight=True, pointers=pointers)
    for index in range(IN_FLIGHT_RUNS, IN_FLIGHT_RUNS + DELIVERED_RUNS):
        _delivered_run(root, config_home, index, in_flight=False, pointers=pointers)
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    recovery_classification = importlib.import_module(
        "reckon.crew.recovery_classification"
    )
    monkeypatch.setattr(recovery_classification, "_utc_seconds", _observed_seconds)
    return {"home": config_home, "repo": root, "pointers": pointers}


def _count_searches(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record every call the pass makes to the in-flight recognition."""
    calls: list[Any] = []
    real = obligations_module._in_flight_review

    def search(pointer: Any, pointers: Any) -> Any:
        calls.append(pointer)
        return real(pointer, pointers)

    monkeypatch.setattr(obligations_module, "_in_flight_review", search)
    return calls


def _count_in_flight_walks(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Record the `_live_pointers` reads the in-flight set is built from."""
    walks: list[str] = []
    depth = {"building": 0}
    real_pointers = obligations_module._live_pointers
    real_build = getattr(obligations_module, "_reviews_in_flight", None)

    def pointer_read(project: str) -> Any:
        if depth["building"]:
            walks.append(project)
        return real_pointers(project)

    def build(project: str) -> Any:
        depth["building"] += 1
        try:
            return real_build(project)
        finally:
            depth["building"] -= 1

    monkeypatch.setattr(obligations_module, "_live_pointers", pointer_read)
    if real_build is not None:
        monkeypatch.setattr(obligations_module, "_reviews_in_flight", build)
    return walks


def _collect(monkeypatch: pytest.MonkeyPatch, into: list[tuple[str, str, str]]) -> None:
    """Record, into ``into``, the duty rows the pass raises."""

    real_duty = obligations_module._review_duty_item

    def duty(*args: Any, **kwargs: Any) -> Any:
        item = real_duty(*args, **kwargs)
        if item is not None:
            into.append(
                (
                    str(item.get("run_id") or ""),
                    str(item.get("kind") or ""),
                    str(item.get("next_command") or ""),
                )
            )
        return item

    monkeypatch.setattr(obligations_module, "_review_duty_item", duty)


def _base_standing_review(pointer: Any, read_pointers: Any) -> str:
    """The base read path: one live sweep for each pointer that asks."""
    launched = [
        candidate
        for candidate in read_pointers(str(pointer.get("project") or ""))
        if str(candidate.get("phase") or "") != queue_admission.QUEUED_PHASE
    ]
    return obligations_module._in_flight_review(pointer, launched)


def test_a_duty_pass_reads_reviews_in_flight_once(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One pass pays one search per live pointer and one in-flight walk."""
    assert len(fleet["pointers"]) == LIVE_POINTERS, "the fixture's fleet moved"
    read_pointers = obligations_module._live_pointers

    searches = _count_searches(monkeypatch)
    walks = _count_in_flight_walks(monkeypatch)
    rows: list[tuple[str, str, str]] = []
    _collect(monkeypatch, rows)
    populations = station_subscriber.live_work_populations(fleet["pointers"])
    print(
        f"head: searches={len(searches)} walks={len(walks)} "
        f"live_pointers={LIVE_POINTERS} duties={len(rows)}"
    )

    assert len(searches) <= LIVE_POINTERS, (
        f"a pass made {len(searches)} in-flight searches for {LIVE_POINTERS} live "
        "pointers: it asks more than once per live pointer"
    )
    assert walks == [PROJECT], (
        f"the in-flight set was built from {len(walks)} walks, not one"
    )
    assert rows, "the fixture must raise duties, or the pass proves nothing"

    # The shared answer changes only how often the question is asked. The base
    # read path is re-run over the same pass, and its populations and duty rows
    # must be the ones the shared answer produced.
    head_rows = list(rows)
    head_searches = len(searches)
    rows.clear()
    searches.clear()
    monkeypatch.setattr(
        obligations_module,
        "_standing_review",
        lambda pointer: _base_standing_review(pointer, read_pointers),
    )
    base_populations = station_subscriber.live_work_populations(fleet["pointers"])
    print(f"base read path: searches={len(searches)} duties={len(rows)}")
    assert base_populations == populations, "the population reading moved"
    assert rows == head_rows, "the duty rows moved with the read path"
    assert len(searches) > head_searches, (
        "the control path did not read the base way, so the comparison is vacuous"
    )


def test_a_later_pass_seen_the_review_started_between_passes(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared answer is scoped to its pass, never carried across passes."""
    run_id = "run-00"
    pointer = next(p for p in fleet["pointers"] if p.get("run_id") == run_id)
    reviewer_id = f"review-of-{run_id}"
    reviewer_path = runs.pointer_path(reviewer_id)
    pending = json.loads(reviewer_path.read_text(encoding="utf-8"))
    # The first pass runs before the review is dispatched: its pointer is not on
    # disk and not in the fleet the pass is handed.
    reviewer_path.unlink()
    until_started = [p for p in fleet["pointers"] if p.get("run_id") != reviewer_id]

    rows: list[tuple[str, str, str]] = []
    _collect(monkeypatch, rows)
    station_subscriber.live_work_populations(until_started)
    before = [kind for owner, kind, _cmd in rows if owner == run_id]

    # The dispatch lands between the passes, and the second pass is handed the
    # fleet again: only a read that is scoped to its pass can see the new review.
    runs._write_json(reviewer_path, pending)
    rows.clear()
    station_subscriber.live_work_populations(fleet["pointers"])
    after = [kind for owner, kind, _cmd in rows if owner == run_id]

    assert pointer["run_id"] == run_id
    assert before != after, (
        f"the second pass did not see the review started between passes: "
        f"run ids read {before!r} then {after!r}"
    )
