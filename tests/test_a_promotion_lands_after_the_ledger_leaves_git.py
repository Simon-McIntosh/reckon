"""Committed ledger retirement keeps promotion on the durable run store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, _store, ledger, run_store
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git

PROJECT = "sample"
PLAN = "delivery"
PRIOR = {"run_id": "r-prior", "project": PROJECT, "gate": "passed"}


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repository"
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(config / "mounts.json"))
    (config / "mounts.json").write_text(json.dumps({PROJECT: str(root / "docs")}))
    aggregate = ledger.ledger_path(PROJECT, root)
    aggregate.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "doc": "crew",
                "data": {"_version": 1, "runs": [PRIOR], "members": [], "holds": []},
            }
        )
    )
    target = ledger.run_path(PROJECT, PRIOR["run_id"], root)
    target.parent.mkdir()
    target.write_text(ledger.serialize_run(PRIOR))
    plan = root / "docs" / "plans" / f"{PLAN}.html"
    plan.parent.mkdir()
    plan.write_text(
        _plan_html.write_state(
            '<!doctype html><html><head></head><body><main class="plan-doc">'
            "</main></body></html>",
            {"type": "plan", "slug": PLAN, "title": "Delivery", "status": "active"},
        )
    )
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "add", str(aggregate), str(target), str(plan))
    git(root, "commit", "-qm", "test: record history\n\nSeed the temporary project.")
    run_store.append(PROJECT, PRIOR)
    assert _rows() == {PRIOR["run_id"]: PRIOR}
    assert git(root, "ls-files", str(aggregate), str(target)).splitlines() == [
        f"docs/state/{PROJECT}/crew.json",
        f"docs/state/{PROJECT}/runs/{PRIOR['run_id']}.json",
    ]
    return root


def _rows() -> dict:
    with run_store.RunStore() as store:
        return store.records()


def _remove_ledger(root: Path) -> None:
    git(
        root,
        "rm",
        "-r",
        f"docs/state/{PROJECT}/crew.json",
        f"docs/state/{PROJECT}/runs",
    )
    git(
        root,
        "commit",
        "-qm",
        "chore: retire ledger files\n\nKeep history in the store.",
    )
    assert not git(root, "ls-files", f"docs/state/{PROJECT}")
    assert _rows() == {PRIOR["run_id"]: PRIOR}


def _assert_no_ledger_files(root: Path) -> None:
    assert not ledger.ledger_path(PROJECT, root).exists()
    assert not ledger.run_path(PROJECT, "r-next", root).parent.exists()
    assert not git(root, "ls-files", f"docs/state/{PROJECT}")


def test_an_append_after_committed_removal_writes_only_the_store(
    repository: Path,
) -> None:
    _remove_ledger(repository)
    result = ledger.append_run(PROJECT, {"run_id": "r-next"}, root=repository)
    assert result["store"] == {"status": "written"}
    assert set(_rows()) == {PRIOR["run_id"], "r-next"}
    assert _rows()[PRIOR["run_id"]] == PRIOR
    _assert_no_ledger_files(repository)


def test_a_promotion_after_committed_removal_lands_its_comment(
    repository: Path,
) -> None:
    _remove_ledger(repository)
    _write_json(
        pointer_path("r-next"),
        {
            "run_id": "r-next",
            "project": PROJECT,
            "repo": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-01-01T00:00:00Z",
            "node": {"id": "delivery", "plan": PLAN, "section": "delivery"},
        },
    )
    result = promotion.complete(
        "r-next",
        gate="passed",
        outcome="The requested behaviour is delivered.",
        completed_at="2026-01-01T00:01:00Z",
        root=repository,
    )
    assert result["pointer_removed"]
    assert result["store"] == {"status": "written"}
    assert result["landing_check"]["committed"]
    assert set(_rows()) == {PRIOR["run_id"], "r-next"}
    assert _rows()[PRIOR["run_id"]] == PRIOR
    assert _rows()["r-next"]["gate"] == "passed"
    state, _ = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    assert (
        "The requested behaviour is delivered."
        in state["comments"]["delivery"][0]["body"]
    )
    assert git(repository, "show", "--format=", "--name-only", "HEAD") == (
        f"docs/plans/{PLAN}.html"
    )
    assert not git(repository, "status", "--porcelain")
    _assert_no_ledger_files(repository)


@pytest.mark.parametrize("staged", [False, True])
def test_an_uncommitted_deletion_is_refused(repository: Path, staged: bool) -> None:
    path = ledger.ledger_path(PROJECT, repository)
    path.unlink()
    if staged:
        git(repository, "add", str(path))
    before = _rows()
    with pytest.raises(ledger.LedgerError) as refusal:
        ledger.append_run(PROJECT, {"run_id": "r-next"}, root=repository)
    assert str(path) in str(refusal.value)
    assert "refusing to promote" in str(refusal.value)
    assert _rows() == before
    assert not ledger.run_path(PROJECT, "r-next", repository).exists()


@pytest.mark.parametrize("removed", [False, True])
def test_a_duplicate_is_refused_without_changing_the_store(
    repository: Path, removed: bool
) -> None:
    if removed:
        _remove_ledger(repository)
    before = _rows()
    with pytest.raises(ledger.LedgerError) as refusal:
        ledger.append_run(PROJECT, PRIOR, root=repository)
    assert PRIOR["run_id"] in str(refusal.value)
    assert _rows() == before
    if removed:
        _assert_no_ledger_files(repository)
