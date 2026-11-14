"""Only the runs a coordinator can act on now are counted as unreconciled.

A delivered run whose own review is working, and one whose repair round is
working, both stood at the head the reflex dispatched for them with no closure
disposition recorded — because nothing had come back yet. The closure count
read them as work the coordinator owed, so the hook's instruction to work the
list to empty could not be met while the work it was waiting on was already in
flight. The count is the runs needing a coordinator action now: the delivered
run with no review stands, while the run with a review working and the run with
a repair working are left out, and so are the working review and repair runs
themselves.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from reckon.crew import promotion_release as promotion_release_module
from reckon.crew import repair as repair_module
from reckon.crew import review as review_module
from reckon.crew import runs
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "unreconciled-in-flight-fixture"
SESSION = "coordinator-fixture"


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed unreconciled fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(json.dumps({PROJECT: str(root / "docs")}))
    return root


def _write_delivered_run(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: str,
    node_id: str,
    head: str,
) -> dict:
    """Record one complete run that delivered at ``head`` and awaits a verdict."""
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {node_id}\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": head,
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": node_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _write_round_manifest(run_id: str, *, status: str, body: str = "line") -> Path:
    """Write one round's manifest at ``status`` in either manifest body form."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / runs.DEFAULT_MANIFEST_NAME
    if body == "json":
        path.write_text(json.dumps({"status": status}), encoding="utf-8")
    else:
        path.write_text(f"status: {status}\n", encoding="utf-8")
    return path


def _write_working_review(
    *,
    review_run_id: str,
    reviewed_run_id: str,
    head: str,
    phase: str = "running",
    manifest_status: str | None = None,
    manifest_body: str = "line",
) -> dict:
    """Record one launched review working on ``reviewed_run_id`` at ``head``."""
    pointer = {
        "run_id": review_run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "backend": "local",
        "role": "review",
        "node": {
            "id": f"review-{reviewed_run_id}",
            "plan": "fixture-plan",
            "section": "fixture-section",
            "role": "review",
            "time_budget": "20m",
            "write_paths": [
                str(
                    review_module.review_path(
                        PROJECT, reviewed_run_id, reviewed_head_sha=head
                    )
                )
            ],
        },
    }
    if manifest_status is not None:
        pointer["manifest_path"] = str(
            _write_round_manifest(
                review_run_id, status=manifest_status, body=manifest_body
            )
        )
    runs._write_json(runs.pointer_path(review_run_id), pointer)
    return pointer


def _write_working_repair(
    *,
    repair_run_id: str,
    source_node_id: str,
    head: str,
    phase: str = "running",
    manifest_status: str | None = None,
    manifest_body: str = "line",
) -> dict:
    """Record one launched repair round working on ``source_node_id``'s run."""
    node_id = repair_module.round_node_id(
        repair_module.REPAIR_NODE_PREFIX, source_node_id, head
    )
    pointer = {
        "run_id": repair_run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "backend": "local",
        "role": "implement",
        "node": {
            "id": node_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    if manifest_status is not None:
        pointer["manifest_path"] = str(
            _write_round_manifest(
                repair_run_id, status=manifest_status, body=manifest_body
            )
        )
    runs._write_json(runs.pointer_path(repair_run_id), pointer)
    return pointer


def _unreconciled_run_ids(payload: dict) -> list[str]:
    return [
        str(row.get("run_id") or "")
        for row in payload["runs"]
        if row.get("unreconciled")
    ]


def test_a_review_and_a_repair_in_flight_are_not_counted(
    repository: Path, tmp_path: Path
) -> None:
    head = _git(repository, "rev-parse", "HEAD")
    _write_delivered_run(
        repository, tmp_path, run_id="run-no-review", node_id="src-noreview", head=head
    )
    _write_delivered_run(
        repository, tmp_path, run_id="run-reviewing", node_id="src-reviewing", head=head
    )
    _write_delivered_run(
        repository, tmp_path, run_id="run-repairing", node_id="src-repairing", head=head
    )
    _write_working_review(
        review_run_id="r-review-working",
        reviewed_run_id="run-reviewing",
        head=head,
    )
    _write_working_repair(
        repair_run_id="r-repair-working",
        source_node_id="src-repairing",
        head=head,
    )

    payload = runs.drain(PROJECT, session=SESSION)

    assert payload["unreconciled_runs"] == 1, payload["runs"]
    assert _unreconciled_run_ids(payload) == ["run-no-review"]
    assert runs.drain_unreconciled_by_session(PROJECT)[SESSION] == 1

    summary = obligations_module.obligations(PROJECT, SESSION)["summary"]
    assert summary["unreconciled_runs"] == 1


def test_the_count_follows_the_live_pointer_not_the_run(
    repository: Path, tmp_path: Path
) -> None:
    """The exclusion is the working run's life, not the subject's history.

    Once the review pointer is gone the run it reviewed is owed a verdict
    again, so it is counted — the repair on the other subject still holds that
    one out. This is the check that the count reads the working run rather than
    excluding every run that was ever reviewed.
    """
    head = _git(repository, "rev-parse", "HEAD")
    _write_delivered_run(
        repository, tmp_path, run_id="run-reviewing", node_id="src-reviewing", head=head
    )
    _write_delivered_run(
        repository, tmp_path, run_id="run-repairing", node_id="src-repairing", head=head
    )
    _write_working_review(
        review_run_id="r-review-working",
        reviewed_run_id="run-reviewing",
        head=head,
    )
    _write_working_repair(
        repair_run_id="r-repair-working",
        source_node_id="src-repairing",
        head=head,
    )

    assert runs.drain(PROJECT, session=SESSION)["unreconciled_runs"] == 0

    runs.pointer_path("r-review-working").unlink()

    payload = runs.drain(PROJECT, session=SESSION)
    assert payload["unreconciled_runs"] == 1
    assert _unreconciled_run_ids(payload) == ["run-reviewing"]


def test_a_queued_round_is_not_working_and_keeps_its_count(
    repository: Path, tmp_path: Path
) -> None:
    """A request the lane has only queued has not started, so it excuses nothing.

    The exclusion names work that is under way. A queued review is still a
    request waiting on the lane, and the run it will review still awaits the
    coordinator's verdict, so both keep the count a parked request always has.
    """
    head = _git(repository, "rev-parse", "HEAD")
    _write_delivered_run(
        repository,
        tmp_path,
        run_id="run-queued-review",
        node_id="src-queued",
        head=head,
    )
    _write_working_review(
        review_run_id="r-review-queued",
        reviewed_run_id="run-queued-review",
        head=head,
        phase="queued",
    )

    payload = runs.drain(PROJECT, session=SESSION)

    assert payload["unreconciled_runs"] == 2, payload["runs"]
    assert set(_unreconciled_run_ids(payload)) == {
        "run-queued-review",
        "r-review-queued",
    }
    assert all(not row.get("in_flight") for row in payload["runs"])


def test_a_promotion_reading_counts_the_same_runs_as_the_drain(
    repository: Path, tmp_path: Path
) -> None:
    """The fleet reading and the drain share the row builder, in-flight runs too.

    A promotion stamps its own count on its row rather than consuming the
    drain's, so the two readings are two callers of one composition. They must
    agree on the shape this change introduced — a subject run whose review or
    repair is working — not only on the shapes that predate it.
    """
    head = _git(repository, "rev-parse", "HEAD")
    _write_delivered_run(
        repository, tmp_path, run_id="run-no-review", node_id="src-noreview", head=head
    )
    _write_delivered_run(
        repository, tmp_path, run_id="run-reviewing", node_id="src-reviewing", head=head
    )
    _write_delivered_run(
        repository, tmp_path, run_id="run-repairing", node_id="src-repairing", head=head
    )
    _write_working_review(
        review_run_id="r-review-working",
        reviewed_run_id="run-reviewing",
        head=head,
    )
    _write_working_repair(
        repair_run_id="r-repair-working",
        source_node_id="src-repairing",
        head=head,
    )

    drain_count = runs.drain(PROJECT, session=SESSION)["unreconciled_runs"]
    promotion_count = promotion_release_module._unreconciled_live_runs(
        runs.list_live(project=PROJECT)
    )

    assert drain_count == 1
    assert promotion_count == drain_count


@pytest.mark.parametrize("body", ["line", "json"])
def test_a_delivered_review_round_is_counted_and_its_subject_again(
    repository: Path, tmp_path: Path, body: str
) -> None:
    """A round that has delivered owes its coordinator an action, so it counts.

    While the review works, neither the subject nor the review run is a duty.
    Once the review writes a terminal manifest, completing it is the duty, and
    the subject's verdict is the next one — so both are candidates again. The
    manifest is read through the shared parser, which accepts a JSON body and
    the tolerant ``key: value`` form alike.
    """
    head = _git(repository, "rev-parse", "HEAD")
    _write_delivered_run(
        repository, tmp_path, run_id="run-reviewing", node_id="src-reviewing", head=head
    )
    _write_working_review(
        review_run_id="r-review-working",
        reviewed_run_id="run-reviewing",
        head=head,
        manifest_status="complete",
        manifest_body=body,
    )

    payload = runs.drain(PROJECT, session=SESSION)

    assert payload["unreconciled_runs"] == 2, payload["runs"]
    assert set(_unreconciled_run_ids(payload)) == {
        "run-reviewing",
        "r-review-working",
    }
    assert all(not row.get("in_flight") for row in payload["runs"])


def test_a_delivered_repair_round_is_counted_and_its_node_again(
    repository: Path, tmp_path: Path
) -> None:
    """The same rule holds for a repair round that has written a terminal manifest."""
    head = _git(repository, "rev-parse", "HEAD")
    _write_delivered_run(
        repository, tmp_path, run_id="run-repairing", node_id="src-repairing", head=head
    )
    _write_working_repair(
        repair_run_id="r-repair-working",
        source_node_id="src-repairing",
        head=head,
        manifest_status="failed",
    )

    payload = runs.drain(PROJECT, session=SESSION)

    assert payload["unreconciled_runs"] == 2, payload["runs"]
    assert set(_unreconciled_run_ids(payload)) == {
        "run-repairing",
        "r-repair-working",
    }


def test_the_promotion_reading_lists_the_pointer_directory_once(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The promoted reading derives its rows from one snapshot, as the drain does.

    A promotion counts pointer by pointer, so a per-row fallback reads the
    pointer directory and every round's manifest once per pointer. Over N
    pointers that is N directory listings for one fleet reading, which is the
    work this test bounds: the caller enters the row scope and the count reads
    the snapshot once.
    """
    head = _git(repository, "rev-parse", "HEAD")
    for index in range(4):
        _write_delivered_run(
            repository,
            tmp_path,
            run_id=f"run-delivered-{index}",
            node_id=f"src-delivered-{index}",
            head=head,
        )
    _write_delivered_run(
        repository, tmp_path, run_id="run-reviewing", node_id="src-reviewing", head=head
    )
    _write_working_review(
        review_run_id="r-review-working",
        reviewed_run_id="run-reviewing",
        head=head,
    )

    pointers = runs.list_live(project=PROJECT)

    listings: list[int] = []
    original_listing = runs._list_live_records

    def counting_listing(*args: object, **kwargs: object) -> object:
        listings.append(1)
        return original_listing(*args, **kwargs)

    monkeypatch.setattr(runs, "_list_live_records", counting_listing)

    count = promotion_release_module._unreconciled_live_runs(pointers)

    assert count == 4
    assert len(listings) <= 1, listings
