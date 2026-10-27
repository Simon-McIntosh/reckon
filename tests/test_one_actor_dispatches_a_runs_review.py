"""One actor dispatches a run's review.

The review reflex and the review-missing obligation both dispatch the same
review. While the reflex is armed for the session, the obligation names the
reflex as the owner and the window its sweep cadence gives, rather than printing
a dispatch command that collides with the reflex's own claim on the review JSON
path. With no armed follower the command is printed, because nothing else will
launch the review.

Both cases are driven on a synthesised temporary repository and a temporary
configuration home, so no reader reaches a real checkout or the operator's own
crew home.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import review as review_module
from reckon.crew import runs
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "review-owner-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _commit(repository: Path, name: str, body: str) -> str:
    (repository / name).parent.mkdir(parents=True, exist_ok=True)
    (repository / name).write_text(body, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", f"test: {body.strip()}")
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    _commit(root, "seed.txt", "seed\n")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return root


def _write_complete_run(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: str,
    node_id: str,
    head: str,
) -> dict:
    """Record one complete, unpromoted run whose tree carries ``head``."""
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


def _store_review(*, run_id: str, base: str, head: str) -> Path:
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    return review_module.store_review(record)


def _review_items(result: dict) -> list[dict]:
    return [
        item
        for item in result["obligations"]
        if item["kind"] in {"review-missing", "review-ready", "promotable-stale"}
    ]


def _unreviewed_run(repository: Path, tmp_path: Path, *, run_id: str) -> dict:
    """A delivered run with no stored review of any head."""
    head = _commit(repository, "reckon/repair_module.py", "REPAIR = 1\n")
    return _write_complete_run(
        repository, tmp_path, run_id=run_id, node_id="review-owner", head=head
    )


def test_an_armed_reflex_owns_the_dispatch_with_its_window(
    repository: Path, tmp_path: Path
) -> None:
    """With a follower armed, no command is printed and the reflex is named."""
    run_id = "run-reflex-armed"
    _unreviewed_run(repository, tmp_path, run_id=run_id)

    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        result = obligations_module.obligations(PROJECT, SESSION)

    items = _review_items(result)
    assert [item["kind"] for item in items] == ["review-missing"], items
    duty = items[0]
    command = str(duty["next_command"])
    assert "reckon crew dispatch" not in command, duty
    assert "review reflex" in command, duty
    assert SESSION in command, duty
    # The window is the follower's own sweep cadence, quoted as a figure.
    window = obligations_module._reflex_review_window_seconds()
    assert f"within about {window} s" in command, duty
    assert duty["review_owner"] == "reflex", duty


def test_a_superseded_review_of_an_earlier_head_names_the_reflex_too(
    repository: Path, tmp_path: Path
) -> None:
    """A stored record at an earlier head reads missing, and the reflex owns it."""
    run_id = "run-head-moved"
    reviewed_head = _git(repository, "rev-parse", "HEAD")
    head = _commit(repository, "reckon/repair_module.py", "REPAIR = 2\n")
    _write_complete_run(
        repository, tmp_path, run_id=run_id, node_id="head-moved", head=head
    )
    _store_review(run_id=run_id, base=reviewed_head, head=reviewed_head)

    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        result = obligations_module.obligations(PROJECT, SESSION)

    items = _review_items(result)
    assert [item["kind"] for item in items] == ["review-missing"], items
    duty = items[0]
    assert duty["reviewed_head"] == reviewed_head, duty
    assert "reckon crew dispatch" not in str(duty["next_command"]), duty
    assert "review reflex" in str(duty["next_command"]), duty


def test_no_armed_follower_prints_the_dispatch_command(
    repository: Path, tmp_path: Path
) -> None:
    """With no armed follower nothing would launch the review, so it prints."""
    run_id = "run-no-follower"
    _unreviewed_run(repository, tmp_path, run_id=run_id)

    result = obligations_module.obligations(PROJECT, SESSION)

    items = _review_items(result)
    assert [item["kind"] for item in items] == ["review-missing"], items
    duty = items[0]
    command = str(duty["next_command"])
    assert command.startswith("reckon crew dispatch"), duty
    assert "--role review" in command, duty
    assert run_id in command, duty
    assert "review_owner" not in duty, duty
