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


def _write_working_review(
    *, review_run_id: str, reviewed_run_id: str, head: str
) -> dict:
    """Record one launched review working on ``reviewed_run_id`` at ``head``."""
    pointer = {
        "run_id": review_run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": "running",
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
    runs._write_json(runs.pointer_path(review_run_id), pointer)
    return pointer


def _write_working_repair(
    *, repair_run_id: str, source_node_id: str, head: str
) -> dict:
    """Record one launched repair round working on ``source_node_id``'s run."""
    node_id = repair_module.round_node_id(
        repair_module.REPAIR_NODE_PREFIX, source_node_id, head
    )
    pointer = {
        "run_id": repair_run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": "running",
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
