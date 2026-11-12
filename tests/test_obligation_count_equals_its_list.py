"""The unreconciled count and the duty list come from one derivation.

A session is told two figures about the same live pointers: how many runs are
unreconciled, and the list of duties it owes. Read from separate derivations
they drift, and the drift is invisible — a coordinator reading "unreconciled
runs: 3" beside "0 outstanding" has been told that something is owed and that
nothing is, with no run named and no command to run.

Every fixture here builds live pointers in a synthesised repository and a
temporary configuration home, so no reader reaches a real checkout or the
operator's own crew home.
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

PROJECT = "count-equals-list-fixture"
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


def _write_pointer(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: str,
    node_id: str,
    head: str,
    role: str = "implement",
    phase: str = "",
    manifest_status: str = "complete",
    ownerless: bool = False,
) -> dict:
    """Record one live pointer whose manifest carries ``manifest_status``."""
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {node_id}\nstatus: {manifest_status}\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": "" if ownerless else SESSION,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": head,
        "process_alive": False,
        "launch": "in-harness",
        "role": role,
        "manifest_path": str(manifest),
        "node": {
            "id": node_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    if phase:
        pointer["phase"] = phase
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
            "reviewed_base_plan": "fixture-plan",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    return review_module.store_review(record)


def _figures(result: dict) -> tuple[int, list[dict]]:
    """The two figures a coordinator is shown, read from the one payload."""
    items = list(result["obligations"])
    return int(result["summary"]["unreconciled_runs"]), items


def test_a_complete_plan_review_run_is_counted_and_listed_with_its_promotion(
    repository: Path, tmp_path: Path
) -> None:
    """The reported shape: a complete plan-review run holding a live pointer.

    A plan-review run is dispatched by the reflex rather than by the session, so
    its pointer carries no owner. The closure counts such a pointer toward every
    session, so this view has to list it toward every session too.
    """
    run_id = "r-20261021T142127162278-plan-review-of-coil-supply-report-rebuild"
    head = _git(repository, "rev-parse", "HEAD")
    _write_pointer(
        repository,
        tmp_path,
        run_id=run_id,
        node_id="plan-review-of-coil-supply-report-rebuild",
        head=head,
        role="review",
        ownerless=True,
    )

    result = obligations_module.obligations(PROJECT, SESSION)

    count, items = _figures(result)
    assert count == 1, (count, items)
    assert len(items) == 1, items
    assert items[0]["run_id"] == run_id
    assert items[0]["next_command"].startswith(
        f"reckon crew complete --run {run_id}"
    ), items[0]


def test_a_complete_implement_run_is_counted_and_listed_equally(
    repository: Path, tmp_path: Path
) -> None:
    """A complete run with no review stored is one duty, counted once."""
    run_id = "run-complete-implement"
    head = _git(repository, "rev-parse", "HEAD")
    _write_pointer(
        repository, tmp_path, run_id=run_id, node_id="complete-implement", head=head
    )

    result = obligations_module.obligations(PROJECT, SESSION)

    count, items = _figures(result)
    assert count == len(items), (count, items)
    assert count == 1, items


def test_a_reviewed_run_is_counted_and_listed_equally(
    repository: Path, tmp_path: Path
) -> None:
    """A run holding a passing review at its head owes the promotion."""
    run_id = "run-reviewed"
    base = _git(repository, "rev-parse", "HEAD")
    head = _commit(repository, "work.txt", "work\n")
    _write_pointer(repository, tmp_path, run_id=run_id, node_id="reviewed", head=head)
    _store_review(run_id=run_id, base=base, head=head)

    result = obligations_module.obligations(PROJECT, SESSION)

    count, items = _figures(result)
    assert count == len(items), (count, items)
    assert count == 1, items
    assert items[0]["kind"] == "review-ready", items


def test_an_empty_project_reports_both_figures_as_zero(
    repository: Path, tmp_path: Path
) -> None:
    """No live pointer is no count and no list, never one without the other."""
    result = obligations_module.obligations(PROJECT, SESSION)

    count, items = _figures(result)
    assert count == 0, result
    assert items == [], result


def test_a_parked_request_is_counted_and_listed_equally(
    repository: Path, tmp_path: Path
) -> None:
    """A request the admission pass parks holds a fence, so it is a duty."""
    run_id = "run-held"
    head = _git(repository, "rev-parse", "HEAD")
    _write_pointer(
        repository,
        tmp_path,
        run_id=run_id,
        node_id="held-request",
        head=head,
        phase="held",
        manifest_status="in-progress",
    )

    result = obligations_module.obligations(PROJECT, SESSION)

    count, items = _figures(result)
    assert count == len(items), (count, items)
    assert count == 1, items
    assert items[0]["run_id"] == run_id
    assert items[0]["next_command"], items[0]


def test_a_request_waiting_on_an_upstream_section_is_counted_and_listed(
    repository: Path, tmp_path: Path
) -> None:
    """A request parked on its section's refs is a wait, and it is listed."""
    run_id = "run-upstream"
    head = _git(repository, "rev-parse", "HEAD")
    _write_pointer(
        repository,
        tmp_path,
        run_id=run_id,
        node_id="upstream-request",
        head=head,
        phase="awaiting-upstream",
        manifest_status="in-progress",
    )

    result = obligations_module.obligations(PROJECT, SESSION)

    count, items = _figures(result)
    assert count == len(items), (count, items)
    assert count == 1, items
    assert items[0]["run_id"] == run_id
